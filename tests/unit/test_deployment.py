import json
from collections.abc import Callable
from typing import Any

import pytest

from aegc import naming
from aegc.infra import OwnershipError, Report, deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import FakeAutoScaling, sqs_with_counts

IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/aegc/geospatial@sha256:" + "b" * 64
POOL = PoolSpec(name="sci-py314", env="geospatial", python_minor="3.14", image=IMAGE, max_workers=3)


@pytest.fixture
def env(aws: Callable[[str], Any]) -> tuple[Clients, FakeAutoScaling, DeploymentSpec]:
    asg = FakeAutoScaling()
    c = Clients(
        iam=aws("iam"),
        ec2=aws("ec2"),
        autoscaling=asg,
        sqs=aws("sqs"),
        logs=aws("logs"),
        ssm=aws("ssm"),
    )
    # substrate#1464: no default VPC yet, so build one and pass its subnets explicitly.
    vpc = c.ec2.create_vpc(CidrBlock="172.31.0.0/16")["Vpc"]["VpcId"]
    subnets = tuple(
        c.ec2.create_subnet(
            VpcId=vpc, CidrBlock=f"172.31.{i * 16}.0/20", AvailabilityZone=f"{REGION}{az}"
        )["Subnet"]["SubnetId"]
        for i, az in enumerate("abc")
    )
    spec = DeploymentSpec(
        deployment="dev",
        account=ACCOUNT,
        region=REGION,
        bucket=naming.bucket_name(ACCOUNT, REGION),
        pools=(POOL,),
        subnet_ids=subnets,
    )
    return c, asg, spec


def outcomes(r: Report) -> dict[tuple[str, str], str]:
    return {(a.resource, a.name): a.outcome for a in r.actions}


def test_deploy_creates_everything(env: tuple[Clients, FakeAutoScaling, DeploymentSpec]) -> None:
    c, asg, spec = env
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

    # Auto Scaling group: Spot, protected, rebalance suspended, tagged.
    g = asg.groups["aegc-dev-sci-py314"]
    assert g["DesiredCapacity"] == 0 and g["MaxSize"] == 3
    assert g["NewInstancesProtectedFromScaleIn"] is True
    dist = g["MixedInstancesPolicy"]["InstancesDistribution"]
    assert dist["OnDemandPercentageAboveBaseCapacity"] == 0
    assert dist["SpotAllocationStrategy"] == "price-capacity-optimized"
    assert asg.suspended["aegc-dev-sci-py314"] == {"AZRebalance"}
    assert naming.is_owned("aegc-dev-sci-py314", naming.tag_dict(g["Tags"]), "dev")

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
    env: tuple[Clients, FakeAutoScaling, DeploymentSpec],
) -> None:
    c, _, spec = env
    deployment.deploy(c, spec)
    data = c.ec2.describe_launch_template_versions(
        LaunchTemplateName="aegc-dev-sci-py314", Versions=["$Default"]
    )["LaunchTemplateVersions"][0]["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpTokens"] == "required"


def test_deploy_is_idempotent(env: tuple[Clients, FakeAutoScaling, DeploymentSpec]) -> None:
    c, _, spec = env
    deployment.deploy(c, spec)
    again = outcomes(deployment.deploy(c, spec))
    assert "created" not in again.values()
    assert again[("launch template", "aegc-dev-sci-py314")] == "exists"


def test_changed_image_adds_launch_template_version(
    env: tuple[Clients, FakeAutoScaling, DeploymentSpec],
) -> None:
    c, asg, spec = env
    deployment.deploy(c, spec)
    asg.set_desired_capacity(AutoScalingGroupName="aegc-dev-sci-py314", DesiredCapacity=2)
    newer = PoolSpec(**{**POOL.__dict__, "image": IMAGE.replace("b" * 64, "c" * 64)})
    r = outcomes(deployment.deploy(c, DeploymentSpec(**{**spec.__dict__, "pools": (newer,)})))
    assert r[("launch template", "aegc-dev-sci-py314")] == "updated"
    lt = c.ec2.describe_launch_templates(LaunchTemplateNames=["aegc-dev-sci-py314"])[
        "LaunchTemplates"
    ][0]
    assert lt["DefaultVersionNumber"] == 2
    # Redeploying never resets capacity the engine set.
    assert asg.groups["aegc-dev-sci-py314"]["DesiredCapacity"] == 2


def test_dry_run_creates_nothing(env: tuple[Clients, FakeAutoScaling, DeploymentSpec]) -> None:
    c, asg, spec = env
    r = outcomes(deployment.deploy(c, spec, dry_run=True))
    assert set(r.values()) == {"would-create"}
    assert asg.groups == {}
    assert c.sqs.list_queues(QueueNamePrefix="aegc-dev").get("QueueUrls", []) == []


def test_refuses_untagged_look_alike_queue(
    env: tuple[Clients, FakeAutoScaling, DeploymentSpec],
) -> None:
    c, _, spec = env
    c.sqs.create_queue(QueueName="aegc-dev-sci-py314-dlq")
    with pytest.raises(OwnershipError, match="aegc-dev-sci-py314-dlq"):
        deployment.deploy(c, spec)


def test_destroy_dry_run_then_delete_leaves_foreign_resources(
    env: tuple[Clients, FakeAutoScaling, DeploymentSpec],
) -> None:
    c, asg, spec = env
    deployment.deploy(c, spec)
    # Look-alikes aegc didn't create for "dev".
    foreign = c.sqs.create_queue(QueueName="aegc-dev-foreign")["QueueUrl"]
    other = c.sqs.create_queue(QueueName="aegc-dev-other", tags=naming.tags("other", "task-queue"))[
        "QueueUrl"
    ]

    dry = outcomes(deployment.destroy(c, deployment="dev", region=REGION))
    assert dry[("auto scaling group", "aegc-dev-sci-py314")] == "would-delete"
    assert "aegc-dev-sci-py314" in asg.groups

    done = outcomes(deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, poll_s=0))
    assert done[("auto scaling group", "aegc-dev-sci-py314")] == "deleted"
    assert done[("iam role", naming.worker_role("dev", REGION))] == "deleted"
    assert done[("ssm parameter", "/aegc/dev/manifest")] == "deleted"
    assert asg.groups == {}
    left = sorted(c.sqs.list_queues(QueueNamePrefix="aegc-dev").get("QueueUrls", []))
    assert left == sorted([foreign, other])
    assert (
        c.ec2.describe_launch_templates(
            Filters=[{"Name": "tag:aegc:deployment", "Values": ["dev"]}]
        )["LaunchTemplates"]
        == []
    )
    with pytest.raises(c.iam.exceptions.NoSuchEntityException):
        c.iam.get_role(RoleName=naming.worker_role("dev", REGION))


def test_destroy_refuses_with_queued_tasks(
    env: tuple[Clients, FakeAutoScaling, DeploymentSpec],
) -> None:
    c, asg, spec = env
    deployment.deploy(c, spec)
    tq = c.sqs.get_queue_url(QueueName="aegc-dev-sci-py314-tasks")["QueueUrl"]
    c.sqs.send_message(QueueUrl=tq, MessageBody="{}")
    real_sqs = c.sqs
    c.sqs = sqs_with_counts(real_sqs, {tq: (1, 0)})  # substrate#1472
    with pytest.raises(RuntimeError, match="queued or running"):
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False)
    assert "aegc-dev-sci-py314" in asg.groups
    deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, force=True, poll_s=0)
    assert asg.groups == {}
