"""Per-deployment and per-pool resources: ``aegc deploy`` and ``aegc destroy``.

Deployment: worker IAM role + instance profile (path /aegc/), a security group with no ingress, the
log group ``/aegc/<d>``, and the manifest ``/aegc/<d>/manifest`` that the engine reads.

Pool: a task queue with a dead-letter queue, a launch template (stock ECS-optimized AL2023 arm64 AMI
via ``resolve:ssm:``, IMDSv2 with hop limit 2, gp3 root), and a Spot Auto Scaling group with
attribute-based Graviton selection, protected from scale-in and with AZRebalance suspended. Workers
scale the group in themselves.

Every create carries the ownership tags; ``destroy`` deletes only what carries them (see
:func:`aegc.naming.is_owned`) and is a dry run unless asked otherwise.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from aegc import __version__, naming
from aegc.awsutil import error_code, is_not_found
from aegc.infra import Outcome, OwnershipError, Report, iam, userdata

AMI_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
DEFAULT_INSTANCE_TYPES = ("c8g.*", "m8g.*", "r8g.*", "c9g.*", "m9g.*", "r9g.*", "c7g.*", "m7g.*")
TASK_VISIBILITY_S = 300
RETENTION_S = 14 * 24 * 3600
MAX_RECEIVE_COUNT = 6  # the worker gives up at 4 deliveries; the DLQ is a backstop
LOG_RETENTION_DAYS = 14


@dataclass(frozen=True)
class PoolSpec:
    name: str  # e.g. "sci-py314"
    env: str  # e.g. "geospatial"
    python_minor: str  # e.g. "3.14", read from the image at build time
    image: str  # <registry>/<repo>@sha256:<digest>
    min_workers: int = 0
    max_workers: int = 4
    vcpu_min: int = 2
    vcpu_max: int = 16
    memory_mib_min: int = 4096
    spot: bool = True
    slots: str = "auto"
    allowed_instance_types: tuple[str, ...] = DEFAULT_INSTANCE_TYPES
    root_volume_gib: int = 50
    idle_timeout_s: int = 300

    def __post_init__(self) -> None:
        naming.validate_name("pool", self.name)
        if not 0 <= self.min_workers <= self.max_workers:
            raise ValueError("need 0 <= min_workers <= max_workers")


@dataclass(frozen=True)
class DeploymentSpec:
    deployment: str
    account: str
    region: str
    bucket: str
    pools: tuple[PoolSpec, ...]
    subnet_ids: tuple[str, ...] = ()  # default: the default VPC's default subnets
    extra_tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        naming.validate_name("deployment", self.deployment)
        if len({p.name for p in self.pools}) != len(self.pools):
            raise ValueError("pool names must be unique")


@dataclass
class Clients:
    iam: Any
    ec2: Any
    autoscaling: Any
    sqs: Any
    logs: Any
    ssm: Any


def _retry(fn: Callable[[], Any], *, codes: set[str], attempts: int, delay_s: float) -> Any:
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if error_code(e) not in codes or attempt == attempts - 1:
                raise
            time.sleep(delay_s)
    return None


# --- deployment-level --------------------------------------------------------------------------


def ensure_worker_role(c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool) -> str:
    d, name = spec.deployment, naming.worker_role(spec.deployment, spec.region)
    tags = naming.tag_list(naming.tags(d, "worker-role", extra=spec.extra_tags))
    policy = json.dumps(
        iam.worker_policy(
            deployment=d, region=spec.region, account=spec.account, bucket=spec.bucket
        )
    )
    profile_arn = f"arn:aws:iam::{spec.account}:instance-profile{naming.IAM_PATH}{name}"
    try:
        found = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
        role_outcome: Outcome = "exists"
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        role_outcome = "would-create" if dry_run else "created"
        if not dry_run:
            c.iam.create_role(
                Path=naming.IAM_PATH,
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(iam.ec2_trust_policy()),
                Description=f"aegc worker instances for deployment {d}",
                Tags=tags,
            )
    report.add("iam role", name, role_outcome)
    if dry_run:
        return profile_arn
    c.iam.put_role_policy(RoleName=name, PolicyName=iam.WORKER_POLICY, PolicyDocument=policy)

    try:
        prof = c.iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
        found = naming.tag_dict(c.iam.list_instance_profile_tags(InstanceProfileName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"instance profile {name} exists but isn't tagged as {d}'s")
        report.add("iam instance profile", name, "exists")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        prof = c.iam.create_instance_profile(
            Path=naming.IAM_PATH, InstanceProfileName=name, Tags=tags
        )["InstanceProfile"]
        report.add("iam instance profile", name, "created")
    if name not in [r["RoleName"] for r in prof.get("Roles", [])]:
        c.iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    return str(prof.get("Arn", profile_arn))


def default_subnets(ec2: Any) -> tuple[str, list[str]]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("no default VPC in this region; pass subnet ids explicitly")
    vpc = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc]},
            {"Name": "default-for-az", "Values": ["true"]},
        ]
    )["Subnets"]
    return vpc, sorted(s["SubnetId"] for s in subnets)


def resolve_network(ec2: Any, spec: DeploymentSpec) -> tuple[str, list[str]]:
    if not spec.subnet_ids:
        return default_subnets(ec2)
    subnets = ec2.describe_subnets(SubnetIds=list(spec.subnet_ids))["Subnets"]
    vpcs = {s["VpcId"] for s in subnets}
    if len(vpcs) != 1:
        raise ValueError("all subnets must be in one VPC")
    return vpcs.pop(), sorted(s["SubnetId"] for s in subnets)


def _owned_ec2_filters(d: str) -> list[dict[str, Any]]:
    # Ownership via tag filters: equally valid on AWS, and works where tags aren't echoed back.
    return [
        {"Name": f"tag:{naming.TAG_MANAGED}", "Values": ["true"]},
        {"Name": f"tag:{naming.TAG_DEPLOYMENT}", "Values": [d]},
    ]


def ensure_security_group(
    c: Clients, spec: DeploymentSpec, vpc: str, report: Report, *, dry_run: bool
) -> str:
    d, name = spec.deployment, naming.security_group(spec.deployment)
    by_name = c.ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc]}]
    )["SecurityGroups"]
    if by_name:
        owned = c.ec2.describe_security_groups(
            Filters=[
                {"Name": "group-id", "Values": [by_name[0]["GroupId"]]},
                *_owned_ec2_filters(d),
            ]
        )["SecurityGroups"]
        if not owned:
            raise OwnershipError(f"security group {name} exists but isn't tagged as {d}'s")
        report.add("security group", name, "exists", by_name[0]["GroupId"])
        return str(by_name[0]["GroupId"])
    if dry_run:
        report.add("security group", name, "would-create")
        return "sg-dry-run"
    gid = c.ec2.create_security_group(
        GroupName=name,
        Description=f"aegc workers for {d}: no ingress",
        VpcId=vpc,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": naming.tag_list(naming.tags(d, "sg", extra=spec.extra_tags)),
            }
        ],
    )["GroupId"]
    report.add("security group", name, "created", gid)
    return str(gid)


def ensure_log_group(c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool) -> None:
    d, name = spec.deployment, naming.log_group(spec.deployment)
    groups = c.logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"]
    match = [g for g in groups if g["logGroupName"] == name]
    if match:
        arn = match[0]["arn"].removesuffix(":*")
        if not naming.is_owned(
            f"{naming.PREFIX}-{d}", c.logs.list_tags_for_resource(resourceArn=arn)["tags"], d
        ):
            raise OwnershipError(f"log group {name} exists but isn't tagged as {d}'s")
        report.add("log group", name, "exists")
    elif dry_run:
        report.add("log group", name, "would-create")
        return
    else:
        c.logs.create_log_group(
            logGroupName=name, tags=naming.tags(d, "logs", extra=spec.extra_tags)
        )
        report.add("log group", name, "created")
    c.logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)


# --- pool-level --------------------------------------------------------------------------------


def _queue_tags(c: Clients, url: str) -> dict[str, str]:
    return dict(c.sqs.list_queue_tags(QueueUrl=url).get("Tags", {}))


def ensure_queue(
    c: Clients,
    name: str,
    tags: dict[str, str],
    attributes: dict[str, str],
    deployment: str,
    report: Report,
    *,
    dry_run: bool,
) -> str:
    try:
        url = c.sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except Exception as e:
        if not is_not_found(e):
            raise
        if dry_run:
            report.add("sqs queue", name, "would-create")
            return f"https://sqs.dry-run/{name}"
        url = c.sqs.create_queue(QueueName=name, Attributes=attributes, tags=tags)["QueueUrl"]
        report.add("sqs queue", name, "created")
        return str(url)
    if not naming.is_owned(name, _queue_tags(c, url), deployment):
        raise OwnershipError(f"queue {name} exists but isn't tagged as {deployment}'s")
    if not dry_run:
        c.sqs.set_queue_attributes(QueueUrl=url, Attributes=attributes)
    report.add("sqs queue", name, "exists")
    return str(url)


def ensure_pool_queues(
    c: Clients, spec: DeploymentSpec, pool: PoolSpec, report: Report, *, dry_run: bool
) -> tuple[str, str]:
    d = spec.deployment
    base = {
        "MessageRetentionPeriod": str(RETENTION_S),
        "SqsManagedSseEnabled": "true",
    }
    dlq_url = ensure_queue(
        c,
        naming.dead_letter_queue(d, pool.name),
        naming.tags(d, "dlq", pool=pool.name, extra=spec.extra_tags),
        base,
        d,
        report,
        dry_run=dry_run,
    )
    if dry_run and "dry-run" in dlq_url:
        dlq_arn = (
            f"arn:aws:sqs:{spec.region}:{spec.account}:{naming.dead_letter_queue(d, pool.name)}"
        )
    else:
        dlq_arn = c.sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
    task_url = ensure_queue(
        c,
        naming.task_queue(d, pool.name),
        naming.tags(d, "task-queue", pool=pool.name, extra=spec.extra_tags),
        {
            **base,
            "VisibilityTimeout": str(TASK_VISIBILITY_S),
            "ReceiveMessageWaitTimeSeconds": "20",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(MAX_RECEIVE_COUNT)}
            ),
        },
        d,
        report,
        dry_run=dry_run,
    )
    return task_url, dlq_url


def worker_config(spec: DeploymentSpec, pool: PoolSpec, task_queue_url: str) -> dict[str, Any]:
    return {
        "deployment": spec.deployment,
        "pool": pool.name,
        "region": spec.region,
        "task_queue_url": task_queue_url,
        "bucket": spec.bucket,
        "slots": pool.slots,
        "idle_timeout_s": pool.idle_timeout_s,
        "visibility_s": TASK_VISIBILITY_S,
        "max_deliveries": 4,
        "aegc_version": __version__,
    }


def resolve_ami(ssm: Any) -> str:
    """The current stock ECS-optimized AL2023 arm64 AMI.

    Resolved at deploy time: Auto Scaling rejects ``resolve:ssm:`` image ids in launch templates
    used with attribute-based instance selection ("expecting ami-..."). Redeploying picks up a
    newer AMI as a new launch template version.
    """
    return str(ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"])


def launch_template_data(
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    ami: str,
    sg: str,
    profile_arn: str,
    task_queue_url: str,
) -> dict[str, Any]:
    d = spec.deployment
    boot = userdata.WorkerBoot(
        deployment=d,
        pool=pool.name,
        image=pool.image,
        log_group=naming.log_group(d),
        worker_config=worker_config(spec, pool, task_queue_url),
    )
    instance_tags = naming.tag_list(naming.tags(d, "worker", pool=pool.name, extra=spec.extra_tags))
    return {
        "ImageId": ami,
        "IamInstanceProfile": {"Arn": profile_arn},
        "SecurityGroupIds": [sg],
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 2,
            "HttpEndpoint": "enabled",
            "InstanceMetadataTags": "enabled",
        },
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": pool.root_volume_gib,
                    "VolumeType": "gp3",
                    "Throughput": 500,
                    "Iops": 3000,
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "UserData": userdata.encode(userdata.render(boot)),
        "TagSpecifications": [
            {"ResourceType": rt, "Tags": instance_tags}
            for rt in ("instance", "volume", "network-interface")
        ],
    }


def template_digest(data: dict[str, Any]) -> str:
    """A short content hash of the launch template data aegc asked for.

    Recorded in each version's description. Comparing read-back data is unreliable: AWS normalizes
    and adds defaults, and emulators may not round-trip every field (substrate#1471).
    """
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def _lt_description(digest: str) -> str:
    return f"aegc {__version__} sha256:{digest}"


def ensure_launch_template(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    data: dict[str, Any],
    report: Report,
    *,
    dry_run: bool,
) -> str:
    d, name = spec.deployment, naming.launch_template(spec.deployment, pool.name)
    digest = template_digest(data)
    try:
        found = c.ec2.describe_launch_templates(LaunchTemplateNames=[name])["LaunchTemplates"]
    except Exception as e:
        if not is_not_found(e):
            raise
        found = []
    if not found:
        if dry_run:
            report.add("launch template", name, "would-create")
            return name
        c.ec2.create_launch_template(
            LaunchTemplateName=name,
            VersionDescription=_lt_description(digest),
            LaunchTemplateData=data,
            TagSpecifications=[
                {
                    "ResourceType": "launch-template",
                    "Tags": naming.tag_list(
                        naming.tags(d, "lt", pool=pool.name, extra=spec.extra_tags)
                    ),
                }
            ],
        )
        report.add("launch template", name, "created")
        return name
    owned = c.ec2.describe_launch_templates(
        Filters=[{"Name": "launch-template-name", "Values": [name]}, *_owned_ec2_filters(d)]
    )["LaunchTemplates"]
    if not owned:
        raise OwnershipError(f"launch template {name} exists but isn't tagged as {d}'s")
    current = c.ec2.describe_launch_template_versions(
        LaunchTemplateName=name, Versions=["$Default"]
    )["LaunchTemplateVersions"][0]
    if current.get("VersionDescription", "").endswith(f"sha256:{digest}"):
        report.add("launch template", name, "exists", f"version {current.get('VersionNumber')}")
        return name
    if dry_run:
        report.add("launch template", name, "updated", "would add a version")
        return name
    v = c.ec2.create_launch_template_version(
        LaunchTemplateName=name,
        VersionDescription=_lt_description(digest),
        LaunchTemplateData=data,
    )["LaunchTemplateVersion"]["VersionNumber"]
    c.ec2.modify_launch_template(LaunchTemplateName=name, DefaultVersion=str(v))
    report.add("launch template", name, "updated", f"version {v}")
    return name


def _asg_request(
    spec: DeploymentSpec, pool: PoolSpec, *, lt_name: str, subnets: list[str]
) -> dict[str, Any]:
    d = spec.deployment
    return {
        "MinSize": pool.min_workers,
        "MaxSize": pool.max_workers,
        "VPCZoneIdentifier": ",".join(subnets),
        "CapacityRebalance": pool.spot,
        "NewInstancesProtectedFromScaleIn": True,
        "HealthCheckType": "EC2",
        "MixedInstancesPolicy": {
            "LaunchTemplate": {
                "LaunchTemplateSpecification": {
                    "LaunchTemplateName": lt_name,
                    "Version": "$Default",
                },
                "Overrides": [
                    {
                        "InstanceRequirements": {
                            "VCpuCount": {"Min": pool.vcpu_min, "Max": pool.vcpu_max},
                            "MemoryMiB": {"Min": pool.memory_mib_min},
                            "CpuManufacturers": ["amazon-web-services"],
                            "BurstablePerformance": "excluded",
                            "AllowedInstanceTypes": list(pool.allowed_instance_types),
                        }
                    }
                ],
            },
            "InstancesDistribution": {
                "OnDemandBaseCapacity": 0,
                "OnDemandPercentageAboveBaseCapacity": 0 if pool.spot else 100,
                "SpotAllocationStrategy": "price-capacity-optimized",
            },
        },
        "_tags": naming.tags(d, "asg", pool=pool.name, extra=spec.extra_tags),
    }


def ensure_auto_scaling_group(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    lt_name: str,
    subnets: list[str],
    report: Report,
    dry_run: bool,
) -> str:
    d, name = spec.deployment, naming.auto_scaling_group(spec.deployment, pool.name)
    req = _asg_request(spec, pool, lt_name=lt_name, subnets=subnets)
    tags = req.pop("_tags")
    groups = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
        "AutoScalingGroups"
    ]
    asg_tags = [
        {
            "Key": k,
            "Value": v,
            # Instances keep the launch template's aegc:role=worker.
            "PropagateAtLaunch": k != naming.TAG_ROLE,
            "ResourceId": name,
            "ResourceType": "auto-scaling-group",
        }
        for k, v in tags.items()
    ]
    if not groups:
        if dry_run:
            report.add("auto scaling group", name, "would-create")
            return name
        c.autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=name,
            DesiredCapacity=pool.min_workers,
            Tags=asg_tags,
            **req,
        )
        outcome: Outcome = "created"
    else:
        if not naming.is_owned(name, naming.tag_dict(groups[0].get("Tags", [])), d):
            raise OwnershipError(f"auto scaling group {name} exists but isn't tagged as {d}'s")
        if dry_run:
            report.add("auto scaling group", name, "exists")
            return name
        # Desired capacity is the engine's and the workers' to manage; don't reset it.
        c.autoscaling.update_auto_scaling_group(AutoScalingGroupName=name, **req)
        c.autoscaling.create_or_update_tags(Tags=asg_tags)
        outcome = "exists"
    # Rebalancing would terminate instances that are running tasks.
    c.autoscaling.suspend_processes(AutoScalingGroupName=name, ScalingProcesses=["AZRebalance"])
    report.add("auto scaling group", name, outcome)
    return name


# --- manifest ----------------------------------------------------------------------------------


def manifest(
    spec: DeploymentSpec,
    pools: dict[str, dict[str, str]],
    *,
    profile_arn: str,
    sg: str,
    subnets: list[str],
) -> dict[str, Any]:
    return {
        "version": 1,
        "aegc_version": __version__,
        "deployment": spec.deployment,
        "region": spec.region,
        "account": spec.account,
        "bucket": spec.bucket,
        "instance_profile_arn": profile_arn,
        "security_group": sg,
        "subnets": subnets,
        "pools": {
            p.name: {
                **{k: v for k, v in asdict(p).items() if k != "allowed_instance_types"},
                "allowed_instance_types": list(p.allowed_instance_types),
                **pools[p.name],
            }
            for p in spec.pools
        },
    }


def write_manifest(
    c: Clients, spec: DeploymentSpec, body: dict[str, Any], report: Report, *, dry_run: bool
) -> None:
    d, name = spec.deployment, naming.deployment_manifest_parameter(spec.deployment)
    value = json.dumps(body, sort_keys=True)
    try:
        c.ssm.get_parameter(Name=name)
        found = naming.tag_dict(
            c.ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
        )
        if not naming.is_owned(f"{naming.PREFIX}-{d}", found, d):
            raise OwnershipError(f"SSM parameter {name} exists but isn't tagged as {d}'s")
        if not dry_run:
            c.ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        report.add("ssm parameter", name, "updated")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if dry_run:
            report.add("ssm parameter", name, "would-create")
            return
        c.ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Tags=naming.tag_list(naming.tags(d, "manifest", extra=spec.extra_tags)),
        )
        report.add("ssm parameter", name, "created")


def read_manifest(ssm: Any, deployment: str) -> dict[str, Any]:
    value = ssm.get_parameter(Name=naming.deployment_manifest_parameter(deployment))["Parameter"][
        "Value"
    ]
    return dict(json.loads(value))


# --- deploy ------------------------------------------------------------------------------------


def deploy(c: Clients, spec: DeploymentSpec, *, dry_run: bool = False) -> Report:
    """Create or reconcile a deployment and its pools. Safe to run repeatedly."""
    report = Report()
    profile_arn = ensure_worker_role(c, spec, report, dry_run=dry_run)
    vpc, subnets = resolve_network(c.ec2, spec)
    if not subnets:
        raise RuntimeError(f"no subnets found in {vpc}")
    sg = ensure_security_group(c, spec, vpc, report, dry_run=dry_run)
    ensure_log_group(c, spec, report, dry_run=dry_run)
    ami = resolve_ami(c.ssm)
    pools: dict[str, dict[str, str]] = {}
    for pool in spec.pools:
        task_url, dlq_url = ensure_pool_queues(c, spec, pool, report, dry_run=dry_run)
        data = launch_template_data(
            spec, pool, ami=ami, sg=sg, profile_arn=profile_arn, task_queue_url=task_url
        )
        lt = ensure_launch_template(c, spec, pool, data, report, dry_run=dry_run)

        # A new instance profile takes a few seconds to become usable by Auto Scaling.
        def make_group(pool: PoolSpec = pool, lt: str = lt) -> str:
            return ensure_auto_scaling_group(
                c, spec, pool, lt_name=lt, subnets=subnets, report=report, dry_run=dry_run
            )

        asg = _retry(
            make_group,
            codes={"ValidationError"},
            attempts=6,
            delay_s=5.0,
        )
        pools[pool.name] = {
            "task_queue_url": task_url,
            "dlq_url": dlq_url,
            "launch_template": lt,
            "auto_scaling_group": asg,
        }
    write_manifest(
        c,
        spec,
        manifest(spec, pools, profile_arn=profile_arn, sg=sg, subnets=subnets),
        report,
        dry_run=dry_run,
    )
    return report


# --- destroy -----------------------------------------------------------------------------------


def destroy(
    c: Clients,
    *,
    deployment: str,
    region: str,
    dry_run: bool = True,
    force: bool = False,
    wait_s: float = 600,
    poll_s: float = 10,
) -> Report:
    """Delete everything aegc created for ``deployment``. Dry run unless ``dry_run=False``.

    Refuses while tasks are queued or in flight unless ``force``. Order: Auto Scaling groups
    (forced, and waited for), launch templates, security group, queues, instance profile and role,
    log group, manifest. Payload objects expire through the bucket's lifecycle rule.
    """
    d = naming.validate_name("deployment", deployment)
    report = Report()
    verb: Outcome = "would-delete" if dry_run else "deleted"
    prefix = f"{naming.PREFIX}-{d}-"

    # Queues first, to check for in-flight work before deleting anything.
    queues = [
        u
        for u in c.sqs.list_queues(QueueNamePrefix=prefix).get("QueueUrls", [])
        if naming.is_owned(u.rsplit("/", 1)[-1], _queue_tags(c, u), d)
    ]
    if not force:
        busy = []
        for u in queues:
            if u.endswith("-tasks"):
                a = c.sqs.get_queue_attributes(
                    QueueUrl=u,
                    AttributeNames=[
                        "ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible",
                    ],
                )["Attributes"]
                n = int(a.get("ApproximateNumberOfMessages", 0)) + int(
                    a.get("ApproximateNumberOfMessagesNotVisible", 0)
                )
                if n:
                    busy.append(f"{u.rsplit('/', 1)[-1]} ({n})")
        if busy:
            raise RuntimeError(f"tasks are queued or running: {', '.join(busy)}; use force")

    groups = [
        g
        for g in c.autoscaling.describe_auto_scaling_groups()["AutoScalingGroups"]
        if g["AutoScalingGroupName"].startswith(prefix)
    ]
    for g in groups:
        name = g["AutoScalingGroupName"]
        if not naming.is_owned(name, naming.tag_dict(g.get("Tags", [])), d):
            report.add("auto scaling group", name, "kept", "not tagged as aegc's")
            continue
        if not dry_run:
            c.autoscaling.delete_auto_scaling_group(AutoScalingGroupName=name, ForceDelete=True)
            _wait_gone(c, name, wait_s=wait_s, poll_s=poll_s)
        report.add("auto scaling group", name, verb)

    for lt in c.ec2.describe_launch_templates(Filters=_owned_ec2_filters(d))["LaunchTemplates"]:
        name = lt["LaunchTemplateName"]
        if not name.startswith(prefix):
            continue
        if not dry_run:
            c.ec2.delete_launch_template(LaunchTemplateName=name)
        report.add("launch template", name, verb)

    sg_name = naming.security_group(d)
    for g in c.ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [sg_name]}, *_owned_ec2_filters(d)]
    )["SecurityGroups"]:
        if not dry_run:
            # Network interfaces of terminated instances can take a minute to go away.
            def delete_group(gid: str = g["GroupId"]) -> Any:
                return c.ec2.delete_security_group(GroupId=gid)

            _retry(
                delete_group,
                codes={"DependencyViolation"},
                attempts=max(1, int(wait_s // max(poll_s, 1))),
                delay_s=poll_s,
            )
        report.add("security group", sg_name, verb, g["GroupId"])

    for u in queues:
        if not dry_run:
            c.sqs.delete_queue(QueueUrl=u)
        report.add("sqs queue", u.rsplit("/", 1)[-1], verb)

    _destroy_worker_role(c, d, region, report, dry_run=dry_run)

    log_name = naming.log_group(d)
    for g in c.logs.describe_log_groups(logGroupNamePrefix=log_name)["logGroups"]:
        if g["logGroupName"] != log_name:
            continue
        tags = c.logs.list_tags_for_resource(resourceArn=g["arn"].removesuffix(":*"))["tags"]
        if not naming.is_owned(f"{naming.PREFIX}-{d}", tags, d):
            report.add("log group", log_name, "kept", "not tagged as aegc's")
            continue
        if not dry_run:
            c.logs.delete_log_group(logGroupName=log_name)
        report.add("log group", log_name, verb)

    param = naming.deployment_manifest_parameter(d)
    try:
        tags = naming.tag_dict(
            c.ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=param)["TagList"]
        )
        if naming.is_owned(f"{naming.PREFIX}-{d}", tags, d):
            if not dry_run:
                c.ssm.delete_parameter(Name=param)
            report.add("ssm parameter", param, verb)
        else:
            report.add("ssm parameter", param, "kept", "not tagged as aegc's")
    except Exception as e:
        # SSM reports a missing parameter to ListTagsForResource as InvalidResourceId.
        if not (is_not_found(e) or error_code(e) == "InvalidResourceId"):
            raise
    return report


def _wait_gone(c: Clients, name: str, *, wait_s: float, poll_s: float) -> None:
    deadline = time.monotonic() + wait_s
    while c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
        "AutoScalingGroups"
    ]:
        if time.monotonic() > deadline:
            raise TimeoutError(f"auto scaling group {name} still exists after {wait_s:.0f} s")
        time.sleep(poll_s)


def _destroy_worker_role(c: Clients, d: str, region: str, report: Report, *, dry_run: bool) -> None:
    name = naming.worker_role(d, region)
    verb: Outcome = "would-delete" if dry_run else "deleted"
    try:
        prof_tags = naming.tag_dict(
            c.iam.list_instance_profile_tags(InstanceProfileName=name)["Tags"]
        )
        if naming.is_owned(name, prof_tags, d):
            if not dry_run:
                prof = c.iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
                for r in prof.get("Roles", []):
                    c.iam.remove_role_from_instance_profile(
                        InstanceProfileName=name, RoleName=r["RoleName"]
                    )
                c.iam.delete_instance_profile(InstanceProfileName=name)
            report.add("iam instance profile", name, verb)
        else:
            report.add("iam instance profile", name, "kept", "not tagged as aegc's")
    except Exception as e:
        if not is_not_found(e):
            raise
    try:
        role_tags = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if naming.is_owned(name, role_tags, d):
            if not dry_run:
                for p in c.iam.list_role_policies(RoleName=name)["PolicyNames"]:
                    c.iam.delete_role_policy(RoleName=name, PolicyName=p)
                c.iam.delete_role(RoleName=name)
            report.add("iam role", name, verb)
        else:
            report.add("iam role", name, "kept", "not tagged as aegc's")
    except Exception as e:
        if not is_not_found(e):
            raise
