import json
from collections.abc import Callable
from typing import Any

import pytest

from aegc import naming
from aegc.awsutil import paginate
from aegc.infra import OwnershipError, Report, deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import launch_on_substrate

IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/aegc/geospatial@sha256:" + "b" * 64
POOL = PoolSpec(name="sci-py314", env="geospatial", python_minor="3.14", image=IMAGE, max_workers=3)
ASG = "aegc-dev-sci-py314"


@pytest.fixture
def env(aws: Callable[[str], Any]) -> tuple[Clients, DeploymentSpec]:
    c = Clients(
        iam=aws("iam"),
        ec2=aws("ec2"),
        autoscaling=aws("autoscaling"),
        sqs=aws("sqs"),
        logs=aws("logs"),
        ssm=aws("ssm"),
    )
    # No subnet ids: the deployment lands in the region's default VPC.
    spec = DeploymentSpec(
        deployment="dev",
        account=ACCOUNT,
        region=REGION,
        bucket=naming.bucket_name(ACCOUNT, REGION),
        pools=(POOL,),
    )
    return c, spec


def outcomes(r: Report) -> dict[tuple[str, str], str]:
    return {(a.resource, a.name): a.outcome for a in r.actions}


def group(c: Clients, name: str = ASG) -> dict[str, Any] | None:
    found = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
        "AutoScalingGroups"
    ]
    return dict(found[0]) if found else None


def default_vpc(ec2: Any) -> tuple[str, list[str]]:
    vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])[
        "Subnets"
    ]
    return vpc["VpcId"], sorted(s["SubnetId"] for s in subnets if s["DefaultForAz"])


def test_resolve_network_finds_the_default_subnets(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    c, spec = env
    vpc, subnets = deployment.resolve_network(c.ec2, spec)
    assert (vpc, subnets) == default_vpc(c.ec2)
    assert len(subnets) >= 2
    found = c.ec2.describe_subnets(SubnetIds=subnets)["Subnets"]
    assert all(s["CidrBlock"].endswith("/20") for s in found)
    assert len({s["AvailabilityZone"] for s in found}) == len(subnets)  # one per AZ


def test_deploy_into_explicit_subnets(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    vpc = c.ec2.create_vpc(CidrBlock="10.20.0.0/16")["Vpc"]["VpcId"]
    subnets = sorted(
        c.ec2.create_subnet(
            VpcId=vpc, CidrBlock=f"10.20.{i}.0/24", AvailabilityZone=f"{REGION}{az}"
        )["Subnet"]["SubnetId"]
        for i, az in enumerate("ab")
    )
    spec = DeploymentSpec(**{**spec.__dict__, "subnet_ids": tuple(subnets)})
    assert deployment.resolve_network(c.ec2, spec) == (vpc, subnets)
    deployment.deploy(c, spec)
    g = group(c)
    assert g is not None and sorted(g["VPCZoneIdentifier"].split(",")) == subnets
    sg = c.ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["aegc-dev-workers"]}]
    )["SecurityGroups"][0]
    assert sg["VpcId"] == vpc
    assert deployment.read_manifest(c.ssm, "dev")["subnets"] == subnets


def test_subnets_must_share_a_vpc(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    vpc = c.ec2.create_vpc(CidrBlock="10.20.0.0/16")["Vpc"]["VpcId"]
    mine = c.ec2.create_subnet(VpcId=vpc, CidrBlock="10.20.0.0/24")["Subnet"]["SubnetId"]
    _, defaults = default_vpc(c.ec2)
    spec = DeploymentSpec(**{**spec.__dict__, "subnet_ids": (mine, defaults[0])})
    with pytest.raises(ValueError, match="one VPC"):
        deployment.resolve_network(c.ec2, spec)


def test_deploy_creates_everything(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    r = outcomes(deployment.deploy(c, spec))
    role = naming.worker_role("dev", REGION)
    assert r[("iam role", role)] == "created"
    assert r[("iam instance profile", role)] == "created"
    assert r[("security group", "aegc-dev-workers")] == "created"
    assert r[("log group", "/aegc/dev")] == "created"
    assert r[("sqs queue", "aegc-dev-sci-py314-tasks")] == "created"
    assert r[("sqs queue", "aegc-dev-sci-py314-dlq")] == "created"
    assert r[("launch template", "aegc-dev-sci-py314")] == "created"
    assert r[("auto scaling group", "aegc-dev-sci-py314")] == "created"
    assert r[("ssm parameter", "/aegc/dev/manifest")] == "created"

    # Worker policy is scoped to this deployment.
    doc = c.iam.get_role_policy(RoleName=role, PolicyName="aegc-worker")["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    assert by_sid["ConsumeTasks"]["Resource"].endswith(":aegc-dev-*-tasks")
    assert by_sid["SelfDrain"]["Condition"] == {
        "StringEquals": {"autoscaling:ResourceTag/aegc:deployment": "dev"}
    }

    # Security group: no ingress.
    sg = c.ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["aegc-dev-workers"]}]
    )["SecurityGroups"][0]
    assert sg["IpPermissions"] == []

    # Task queue redrives to the DLQ.
    tq = c.sqs.get_queue_url(QueueName="aegc-dev-sci-py314-tasks")["QueueUrl"]
    attrs = c.sqs.get_queue_attributes(QueueUrl=tq, AttributeNames=["All"])["Attributes"]
    assert attrs["VisibilityTimeout"] == "300"
    assert json.loads(attrs["RedrivePolicy"])["deadLetterTargetArn"].endswith(
        ":aegc-dev-sci-py314-dlq"
    )

    # Launch template: stock AMI via SSM, gp3 at 500 MB/s, and the content hash we compare.
    version = c.ec2.describe_launch_template_versions(
        LaunchTemplateName="aegc-dev-sci-py314", Versions=["$Default"]
    )["LaunchTemplateVersions"][0]
    data = version["LaunchTemplateData"]
    ami = c.ssm.get_parameter(Name=deployment.AMI_PARAMETER)["Parameter"]["Value"]
    assert data["ImageId"] == ami and ami.startswith("ami-")
    assert data["BlockDeviceMappings"][0]["Ebs"]["Throughput"] == 500
    assert version["VersionDescription"].startswith("aegc ")

    # Auto Scaling group: Spot, protected, rebalance suspended, tagged, in the default subnets.
    g = group(c)
    assert g is not None
    assert (g["MinSize"], g["DesiredCapacity"], g["MaxSize"]) == (0, 0, 3)
    assert g["Instances"] == []
    assert g["NewInstancesProtectedFromScaleIn"] is True
    assert g["CapacityRebalance"] is True
    assert sorted(g["VPCZoneIdentifier"].split(",")) == default_vpc(c.ec2)[1]
    assert [p["ProcessName"] for p in g["SuspendedProcesses"]] == ["AZRebalance"]
    mixed = g["MixedInstancesPolicy"]
    lt_spec = mixed["LaunchTemplate"]["LaunchTemplateSpecification"]
    assert lt_spec["LaunchTemplateName"] == ASG and lt_spec["Version"] == "$Default"
    (override,) = mixed["LaunchTemplate"]["Overrides"]
    req = override["InstanceRequirements"]
    assert req["VCpuCount"] == {"Min": 2, "Max": 16}
    assert req["MemoryMiB"] == {"Min": 4096}
    assert req["CpuManufacturers"] == ["amazon-web-services"]
    assert req["BurstablePerformance"] == "excluded"
    assert req["AllowedInstanceTypes"] == list(deployment.DEFAULT_INSTANCE_TYPES)
    dist = mixed["InstancesDistribution"]
    assert dist["OnDemandBaseCapacity"] == 0
    assert dist["OnDemandPercentageAboveBaseCapacity"] == 0
    assert dist["SpotAllocationStrategy"] == "price-capacity-optimized"
    tags = naming.tag_dict(g["Tags"])
    assert naming.is_owned(ASG, tags, "dev") and tags[naming.TAG_POOL] == "sci-py314"
    # aegc:role stays on the group; instances get role=worker from the launch template.
    assert {t["Key"]: t["PropagateAtLaunch"] for t in g["Tags"]}[naming.TAG_ROLE] is False
    assert all(t["PropagateAtLaunch"] for t in g["Tags"] if t["Key"] != naming.TAG_ROLE)
    described = c.autoscaling.describe_tags(
        Filters=[{"Name": "auto-scaling-group", "Values": [ASG]}]
    )["Tags"]
    assert naming.tag_dict(described) == tags

    # Manifest: what the engine needs.
    m = deployment.read_manifest(c.ssm, "dev")
    assert m["pools"]["sci-py314"]["task_queue_url"] == tq
    assert m["pools"]["sci-py314"]["python_minor"] == "3.14"
    assert m["bucket"] == spec.bucket


def test_launch_template_requests_imdsv2() -> None:
    data = deployment.launch_template_data(
        DeploymentSpec(deployment="dev", account=ACCOUNT, region=REGION, bucket="b", pools=(POOL,)),
        POOL,
        ami="ami-1",
        sg="sg-1",
        profile_arn="arn:aws:iam::1:instance-profile/aegc/x",
        task_queue_url="https://sqs/q",
    )
    assert data["MetadataOptions"] == {
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 2,
        "HttpEndpoint": "enabled",
        "InstanceMetadataTags": "enabled",
    }


@pytest.mark.xfail(strict=True, reason="substrate#1471: launch templates drop MetadataOptions")
def test_launch_template_metadata_options_round_trip(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    data = c.ec2.describe_launch_template_versions(
        LaunchTemplateName="aegc-dev-sci-py314", Versions=["$Default"]
    )["LaunchTemplateVersions"][0]["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpTokens"] == "required"


@pytest.mark.xfail(
    strict=True,
    reason="substrate#1497: no Graviton types in the catalog, so the group launches nothing",
)
def test_deployed_group_launches_graviton_spot(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=1)
    g = group(c)
    assert g is not None
    (inst,) = g["Instances"]
    assert inst["LifecycleState"] == "InService" and inst["ProtectedFromScaleIn"] is True
    (r,) = c.ec2.describe_instances(InstanceIds=[inst["InstanceId"]])["Reservations"]
    assert r["Instances"][0]["InstanceLifecycle"] == "spot"
    assert r["Instances"][0]["Architecture"] == "arm64"


def test_deploy_is_idempotent(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    again = outcomes(deployment.deploy(c, spec))
    assert "created" not in again.values()
    assert again[("launch template", "aegc-dev-sci-py314")] == "exists"


def test_changed_image_adds_launch_template_version(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    launch_on_substrate(c.autoscaling, ASG)  # substrate#1497
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=2)
    newer = PoolSpec(**{**POOL.__dict__, "image": IMAGE.replace("b" * 64, "c" * 64)})
    r = outcomes(deployment.deploy(c, DeploymentSpec(**{**spec.__dict__, "pools": (newer,)})))
    assert r[("launch template", "aegc-dev-sci-py314")] == "updated"
    lt = c.ec2.describe_launch_templates(LaunchTemplateNames=["aegc-dev-sci-py314"])[
        "LaunchTemplates"
    ][0]
    assert lt["DefaultVersionNumber"] == 2
    # Redeploying never resets capacity the engine set, nor touches the running workers.
    g = group(c)
    assert g is not None and g["DesiredCapacity"] == 2 and len(g["Instances"]) == 2
    mixed = g["MixedInstancesPolicy"]["LaunchTemplate"]["LaunchTemplateSpecification"]
    assert mixed["Version"] == "$Default"


def test_dry_run_creates_nothing(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    r = outcomes(deployment.deploy(c, spec, dry_run=True))
    assert set(r.values()) == {"would-create"}
    assert c.autoscaling.describe_auto_scaling_groups()["AutoScalingGroups"] == []
    assert c.sqs.list_queues(QueueNamePrefix="aegc-dev").get("QueueUrls", []) == []


def test_refuses_untagged_look_alike_queue(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    c, spec = env
    c.sqs.create_queue(QueueName="aegc-dev-sci-py314-dlq")
    with pytest.raises(OwnershipError, match="aegc-dev-sci-py314-dlq"):
        deployment.deploy(c, spec)


def instance_states(c: Clients, ids: list[str]) -> set[str]:
    found = c.ec2.describe_instances(InstanceIds=ids)["Reservations"]
    return {i["State"]["Name"] for r in found for i in r["Instances"]}


def test_destroy_dry_run_then_delete_leaves_foreign_resources(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    # Running workers: destroy has to force-delete the group and wait for it to go.
    launch_on_substrate(c.autoscaling, ASG)  # substrate#1497
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=2)
    g = group(c)
    assert g is not None
    workers = [i["InstanceId"] for i in g["Instances"]]
    assert len(workers) == 2 and instance_states(c, workers) == {"running"}
    # Look-alikes aegc didn't create for "dev".
    foreign = c.sqs.create_queue(QueueName="aegc-dev-foreign")["QueueUrl"]
    other = c.sqs.create_queue(QueueName="aegc-dev-other", tags=naming.tags("other", "task-queue"))[
        "QueueUrl"
    ]

    dry = outcomes(deployment.destroy(c, deployment="dev", region=REGION))
    assert dry[("auto scaling group", ASG)] == "would-delete"
    assert group(c) is not None

    done = outcomes(
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=30, poll_s=0.1)
    )
    assert done[("auto scaling group", ASG)] == "deleted"
    assert done[("security group", "aegc-dev-workers")] == "deleted"
    assert done[("iam role", naming.worker_role("dev", REGION))] == "deleted"
    assert done[("ssm parameter", "/aegc/dev/manifest")] == "deleted"
    assert group(c) is None
    assert instance_states(c, workers) <= {"shutting-down", "terminated"}
    left = sorted(c.sqs.list_queues(QueueNamePrefix="aegc-dev").get("QueueUrls", []))
    assert left == sorted([foreign, other])
    assert (
        c.ec2.describe_launch_templates(
            Filters=[{"Name": "tag:aegc:deployment", "Values": ["dev"]}]
        )["LaunchTemplates"]
        == []
    )
    assert (
        c.ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["aegc-dev-workers"]}]
        )["SecurityGroups"]
        == []
    )
    with pytest.raises(c.iam.exceptions.NoSuchEntityException):
        c.iam.get_role(RoleName=naming.worker_role("dev", REGION))


def test_destroy_refuses_with_queued_tasks(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    launch_on_substrate(c.autoscaling, ASG)  # substrate#1497
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=1)
    tq = c.sqs.get_queue_url(QueueName="aegc-dev-sci-py314-tasks")["QueueUrl"]
    c.sqs.send_message(QueueUrl=tq, MessageBody="{}")
    with pytest.raises(RuntimeError, match=r"aegc-dev-sci-py314-tasks \(1\); use force"):
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False)
    g = group(c)
    assert g is not None and len(g["Instances"]) == 1
    # A claimed (in-flight) task counts too.
    c.sqs.receive_message(QueueUrl=tq, WaitTimeSeconds=0)
    with pytest.raises(RuntimeError, match="queued or running"):
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False)
    deployment.destroy(
        c, deployment="dev", region=REGION, dry_run=False, force=True, wait_s=30, poll_s=0.1
    )
    assert group(c) is None


def test_destroy_finds_groups_past_the_first_page(env: tuple[Clients, DeploymentSpec]) -> None:
    # Auto Scaling returns 50 groups per page. 55 unrelated groups that sort before ours push the
    # deployment's group onto the second page; destroy must still find it (#26).
    c, spec = env
    deployment.deploy(c, spec)
    lt = c.ec2.create_launch_template(
        LaunchTemplateName="a-other", LaunchTemplateData={"ImageId": "ami-12345678"}
    )["LaunchTemplate"]["LaunchTemplateId"]
    for i in range(55):
        c.autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=f"a-other-{i:02d}",
            LaunchTemplate={"LaunchTemplateId": lt},
            MinSize=0,
            MaxSize=0,
            AvailabilityZones=[f"{REGION}a"],
        )
    first = c.autoscaling.describe_auto_scaling_groups()["AutoScalingGroups"]
    assert ASG not in [g["AutoScalingGroupName"] for g in first]  # really on a later page
    done = outcomes(
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=30, poll_s=0.1)
    )
    assert done[("auto scaling group", ASG)] == "deleted"
    assert group(c) is None
    left = list(paginate(c.autoscaling, "describe_auto_scaling_groups", "AutoScalingGroups"))
    assert len(left) == 55  # the unrelated groups are untouched
