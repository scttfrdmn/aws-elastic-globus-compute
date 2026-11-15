import time
from collections.abc import Callable
from typing import Any

import pytest

from aegc import cli, naming, ops
from aegc.infra import deployment
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import no_network_interfaces
from tests.unit.test_deployment import POOL


def test_parse_pool() -> None:
    assert cli.parse_pool("geospatial") == {"env": "geospatial"}
    assert cli.parse_pool("climate:max=8,vcpu=4-16,mem=8192,slots=2,on-demand,idle=120") == {
        "env": "climate", "_max_workers": 8, "vcpu_min": 4, "vcpu_max": 16,
        "memory_mib_min": 8192, "slots": "2", "spot": False, "idle_timeout_s": 120,
    }  # fmt: skip
    assert cli.parse_pool("x:disk=50,throughput=250") == {
        "env": "x", "root_volume_gib": 50, "throughput_mibps": 250,
    }  # fmt: skip
    assert cli.parse_pool("x:max_vcpus=64,min_vcpus=4,walltime=600,max_walltime=3600") == {
        "env": "x", "max_vcpus": 64, "min_vcpus": 4, "walltime_s": 600, "max_walltime_s": 3600,
    }  # fmt: skip
    with pytest.raises(SystemExit):
        cli.parse_pool("x:gpu=1")


def test_reap_only_idle_empty_owned_queues(aws: Callable[[str], Any]) -> None:
    sqs = aws("sqs")
    tags = naming.tags("dev", "result-queue")
    idle = sqs.create_queue(QueueName="aegc-dev-res-aaaaaaaaaaaa", tags=tags)["QueueUrl"]
    busy = sqs.create_queue(QueueName="aegc-dev-res-bbbbbbbbbbbb", tags=tags)["QueueUrl"]
    sqs.send_message(QueueUrl=busy, MessageBody="{}")
    foreign = sqs.create_queue(QueueName="aegc-dev-res-cccccccccccc")["QueueUrl"]  # untagged
    later = time.time() + ops.RESULT_QUEUE_IDLE_S + 60

    dry = {a.name: a.outcome for a in ops.reap(sqs=sqs, deployment="dev", now=later).actions}
    assert dry == {"aegc-dev-res-aaaaaaaaaaaa": "would-delete", "aegc-dev-res-bbbbbbbbbbbb": "kept"}
    assert {a.name: a.outcome for a in ops.reap(sqs=sqs, deployment="dev").actions}[
        "aegc-dev-res-aaaaaaaaaaaa"
    ] == "kept"  # not idle yet

    ops.reap(sqs=sqs, deployment="dev", dry_run=False, now=later)
    left = sqs.list_queues(QueueNamePrefix="aegc-dev-res-").get("QueueUrls", [])
    assert sorted(left) == sorted([busy, foreign]) and idle not in left


# --- footprint (#43) ---------------------------------------------------------------------------


@pytest.fixture
def env(aws: Callable[[str], Any]) -> tuple[deployment.Clients, deployment.DeploymentSpec]:
    c = deployment.Clients(
        iam=aws("iam"),
        ec2=no_network_interfaces(aws("ec2")),  # substrate gap: no DescribeNetworkInterfaces
        autoscaling=aws("autoscaling"),
        sqs=aws("sqs"),
        logs=aws("logs"),
        ssm=aws("ssm"),
    )
    spec = deployment.DeploymentSpec(
        deployment="dev",
        account=ACCOUNT,
        region=REGION,
        bucket=naming.bucket_name(ACCOUNT, REGION),
        pools=(POOL,),
    )
    return c, spec


def footprint(aws: Callable[[str], Any]) -> ops.Footprint:
    return ops.footprint(
        tagging=aws("resourcegroupstaggingapi"),
        sqs=aws("sqs"),
        autoscaling=aws("autoscaling"),
        ec2=aws("ec2"),
        ssm=aws("ssm"),
        deployment="dev",
        region=REGION,
    )


def look_alikes(aws: Callable[[str], Any]) -> None:
    """Resources with aegc's names for dev that aegc didn't create for dev."""
    sqs, ec2, ssm = aws("sqs"), aws("ec2"), aws("ssm")
    sqs.create_queue(QueueName="aegc-dev-foreign")  # untagged
    sqs.create_queue(QueueName="aegc-dev-other", tags=naming.tags("other", "task-queue"))
    ec2.create_launch_template(
        LaunchTemplateName="aegc-dev-fake", LaunchTemplateData={"ImageId": "ami-12345678"}
    )
    ssm.put_parameter(Name="/aegc/dev/stray", Value="1", Type="String")


def test_footprint_finds_owned_and_look_alikes(
    env: tuple[deployment.Clients, deployment.DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    look_alikes(aws)
    before = sorted(c.sqs.list_queues(QueueNamePrefix="aegc-dev").get("QueueUrls", []))

    fp = footprint(aws)
    owned = {(i.kind, i.name) for i in fp.owned}
    role = naming.worker_role("dev", REGION)
    assert {
        ("sqs queue", "aegc-dev-sci-py314-tasks"),
        ("sqs queue", "aegc-dev-sci-py314-dlq"),
        ("auto scaling group", "aegc-dev-sci-py314"),
        ("launch template", "aegc-dev-sci-py314"),
        ("ssm parameter", "/aegc/dev/manifest"),
        ("log group", "/aegc/dev"),
        ("iam role", role),
    } <= owned
    assert {(i.kind, i.name) for i in fp.look_alikes} == {
        ("sqs queue", "aegc-dev-foreign"),
        ("sqs queue", "aegc-dev-other"),
        ("launch template", "aegc-dev-fake"),
        ("ssm parameter", "/aegc/dev/stray"),
    }
    by_name = {i.name: i for i in fp.items.values()}
    assert by_name["aegc-dev-sci-py314-tasks"].found_by == {"tags", "name"}
    assert by_name[role].found_by == {"tags"}
    assert "look-alike" in fp.render() and "4 look-alike(s)" in fp.render()

    # A report only: nothing changed.
    assert sorted(c.sqs.list_queues(QueueNamePrefix="aegc-dev").get("QueueUrls", [])) == before
    assert c.ssm.get_parameter(Name="/aegc/dev/stray")["Parameter"]["Value"] == "1"
    assert deployment.read_manifest(c.ssm, "dev")["deployment"] == "dev"


def test_footprint_of_nothing(aws: Callable[[str], Any]) -> None:
    fp = footprint(aws)
    assert fp.items == {} and "0 owned, 0 look-alike(s)" in fp.render()


@pytest.mark.xfail(
    strict=True,
    reason="substrate gap (not filed): GetResources omits EC2 security groups, launch templates "
    "and volumes, and IAM instance profiles",
)
def test_footprint_tagging_api_covers_ec2_and_profiles(
    env: tuple[deployment.Clients, deployment.DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    tagged = {(i.kind) for i in footprint(aws).items.values() if "tags" in i.found_by}
    assert {"security group", "launch template", "iam instance profile"} <= tagged


def test_cli_destroy_verify_only(
    env: tuple[deployment.Clients, deployment.DeploymentSpec],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    monkeypatch.setattr(cli, "_clients", lambda sess: c)  # the shimmed clients (substrate gap)
    argv = ["--region", REGION, "destroy", "dev"]
    # --verify alone checks and deletes nothing; the deployment is all there, so it fails.
    assert cli.main([*argv, "--verify"]) == 1
    assert "remaining" in capsys.readouterr().out
    assert deployment.read_manifest(c.ssm, "dev")
    # A plain dry run doesn't verify.
    assert cli.main(argv) == 0
    assert "dry run; pass --yes" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main([*argv, "--verify", "--no-verify"])
    assert cli.main([*argv, "--yes", "--no-verify"]) == 0
    assert "verified" not in capsys.readouterr().out
    assert cli.main([*argv, "--verify"]) == 0
    assert "verified: nothing of dev remains" in capsys.readouterr().out
