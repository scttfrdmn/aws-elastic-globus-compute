"""The cloud-hosted endpoint (#22), its liveness alarm (#60) and the evaluator (#51)."""

from __future__ import annotations

import base64
import io
import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from aegc import endpoint as ep
from aegc import evaluator, lease, ledger, naming, state
from aegc.infra import OwnershipError, Report, deployment, endpoint_host, secrets, userdata
from aegc.infra.deployment import Clients, DeploymentSpec
from tests.conftest import ACCOUNT, REGION
from tests.unit.test_deployment import ASG
from tests.unit.test_deployment import env as env  # the fixture

IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/aegc/endpoint@sha256:" + "c" * 64
EID = "2d44880d-39b5-428e-b6a0-3ce9b3b5c952"
LOGIN = secrets.EndpointLogin(name="aegc-cloud", endpoint_id=EID, storage_db=b"SQLite format 3\0x")


def host_spec(**kw: Any) -> endpoint_host.EndpointHostSpec:
    cloud = ep.EndpointSpec(name="aegc-cloud", deployment="dev", region=REGION, default_env="geo")
    base = endpoint_host.EndpointHostSpec(
        deployment="dev", account=ACCOUNT, region=REGION, name="aegc-cloud", endpoint_id=EID,
        image=IMAGE, configure_script=ep.configure_script(cloud),
    )  # fmt: skip
    return replace(base, **kw)


@pytest.fixture
def cloud(env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]) -> Clients:
    c, _ = env
    return replace(c, secretsmanager=aws("secretsmanager"), cloudwatch=aws("cloudwatch"),
                   sns=aws("sns"))  # fmt: skip


# --- the login secret -------------------------------------------------------------------------


def test_login_round_trip(aws: Callable[[str], Any]) -> None:
    sm = aws("secretsmanager")
    r = Report()
    secrets.put_login(sm, "dev", LOGIN, r, dry_run=False)
    assert secrets.get_login(sm, "dev") == LOGIN
    secrets.put_login(sm, "dev", replace(LOGIN, storage_db=b"newer"), r, dry_run=False)
    assert secrets.get_login(sm, "dev").storage_db == b"newer"
    assert [a.outcome for a in r.actions] == ["created", "updated"]
    secrets.delete_login(sm, "dev", r, dry_run=False)
    assert not secrets.exists(sm, "dev")
    secrets.put_login(sm, "dev", LOGIN, r, dry_run=False)  # no recovery window: name reusable


def test_login_refuses_foreign_and_oversized(aws: Callable[[str], Any]) -> None:
    sm = aws("secretsmanager")
    sm.create_secret(Name="aegc-dev-endpoint", SecretString="{}")  # untagged: not aegc's
    with pytest.raises(OwnershipError):
        secrets.put_login(sm, "dev", LOGIN, Report(), dry_run=False)
    r = Report()
    secrets.delete_login(sm, "dev", r, dry_run=False)
    assert [a.outcome for a in r.actions] == ["kept"]
    with pytest.raises(ValueError, match="65536"):
        secrets.put_login(sm, "prod", replace(LOGIN, storage_db=b"x" * 60_000), Report(),
                          dry_run=False)  # fmt: skip


# --- the host -----------------------------------------------------------------------------------


def test_boot_script() -> None:
    s = host_spec()
    data = endpoint_host.launch_template_data(s, ami="ami-1", sg="sg-1", profile_arn="arn:p")
    script = base64.b64decode(data["UserData"]).decode()
    assert "--secret-id aegc-dev-endpoint" in script
    assert f"GC_ENDPOINT_UUID={EID}" in script and "aegc-evaluate.timer" in script
    assert f"evaluate dev --endpoint-id {EID}" in script and "awslogs-format=json/emf" in script
    (line,) = [ln for ln in script.splitlines() if "| base64 -d" in ln]
    b64 = line.split("echo ", 1)[1].split(" | base64 -d", 1)[0]
    assert base64.b64decode(b64).decode() == s.configure_script
    assert data["InstanceType"] == "t4g.micro"
    assert data["MetadataOptions"]["HttpPutResponseHopLimit"] == 2
    tags = {t["Key"]: t["Value"] for t in data["TagSpecifications"][0]["Tags"]}
    assert tags[naming.TAG_ROLE] == "endpoint"
    with pytest.raises(ValueError, match="pinned"):
        userdata.render_endpoint(userdata.EndpointBoot(
            deployment="dev", name="x", endpoint_id=EID, image="r/aegc/endpoint:latest",
            secret="s", log_group="/aegc/dev", configure_script="true"))  # fmt: skip


def test_deploy_and_undeploy(cloud: Clients) -> None:
    c = cloud
    r = endpoint_host.deploy(c, host_spec(alarm_email="ops@example.org"))
    outcomes = {(a.resource, a.name): a.outcome for a in r.actions}
    assert outcomes[("auto scaling group", "aegc-dev-endpoint")] == "created"
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=["aegc-dev-endpoint"])[
        "AutoScalingGroups"
    ][0]
    assert (g["MinSize"], g["MaxSize"], g["DesiredCapacity"]) == (1, 1, 1)
    gtags = {t["Key"]: t["Value"] for t in g["Tags"]}
    assert gtags[naming.TAG_ROLE] == "endpoint-asg"  # worker/operator policies need "asg"
    (alarm,) = c.cloudwatch.describe_alarms(AlarmNames=["aegc-dev-endpoint-offline"])[
        "MetricAlarms"
    ]
    assert alarm["MetricName"] == "EndpointOnline" and alarm["Threshold"] == 1
    # substrate#1620: DescribeAlarms omits TreatMissingData (AWS returns it).
    assert alarm.get("TreatMissingData", "breaching") == "breaching"
    assert alarm["AlarmActions"] and alarm["AlarmActions"][0].endswith(":aegc-dev-endpoint-alarm")
    again = {(a.resource, a.name): a.outcome for a in endpoint_host.deploy(c, host_spec()).actions}
    assert again[("auto scaling group", "aegc-dev-endpoint")] == "exists"
    assert again[("launch template", "aegc-dev-endpoint")] == "exists"

    dry = endpoint_host.undeploy(c, "dev", REGION, dry_run=True, wait_s=30, poll_s=0.1)
    assert {a.outcome for a in dry.actions} == {"would-delete"}
    done = endpoint_host.undeploy(c, "dev", REGION, dry_run=False, wait_s=30, poll_s=0.1)
    kinds = {a.resource for a in done.actions if a.outcome == "deleted"}
    assert {"auto scaling group", "launch template", "cloudwatch alarm", "sns topic",
            "iam role", "iam instance profile"} <= kinds  # fmt: skip
    assert not c.cloudwatch.describe_alarms(AlarmNames=["aegc-dev-endpoint-offline"])[
        "MetricAlarms"
    ]


def test_destroy_removes_the_endpoint_too(
    cloud: Clients, env: tuple[Clients, DeploymentSpec]
) -> None:
    c = cloud
    _, spec = env
    deployment.deploy(c, spec)
    secrets.put_login(c.secretsmanager, "dev", LOGIN, Report(), dry_run=False)
    endpoint_host.deploy(c, host_spec())
    deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=30, poll_s=0.1)
    assert not secrets.exists(c.secretsmanager, "dev")
    left = deployment.verify_destroyed(c, deployment="dev", region=REGION, wait_s=0)
    assert left.actions == [], left


# --- the evaluator --------------------------------------------------------------------------


def evaluate(c: Clients, aws: Callable[[str], Any], **kw: Any) -> evaluator.Evaluation:
    clients = {"ssm": c.ssm, "s3": aws("s3"), "autoscaling": c.autoscaling,
               "scheduler": aws("scheduler"), "sqs": c.sqs}  # fmt: skip
    return evaluator.evaluate(deployment="dev", region=REGION, clients=clients, **kw)


def test_evaluator_trips_caps_without_an_engine(
    env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = env
    s3 = aws("s3")
    s3.create_bucket(Bucket=spec.bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    deployment.deploy(c, replace(spec, limits={"max_usd_day": 0.01}))
    c.autoscaling.update_auto_scaling_group(AutoScalingGroupName=ASG, MinSize=2, MaxSize=8)
    now = time.time()
    ledger.write(s3, spec.bucket, ledger.Record(
        instance_id="i-1", deployment="dev", pool="sci-py314", instance_type="c8g.large",
        availability_zone="us-west-2a", lifecycle="on-demand", vcpus=2,
        launched_at=now - 3600, last_seen=now - 1, exit_reason=""))  # fmt: skip
    out = io.StringIO()
    dry = evaluate(c, aws, dry_run=True, endpoint_id=EID, online=lambda _: True, out=out)
    assert dry.tripped and state.read_state(c.ssm, "dev").running  # a dry run changes nothing
    assert out.getvalue() == ""
    ev = evaluate(c, aws, endpoint_id=EID, online=lambda _: True, out=out)
    assert ev.tripped and ev.state == "capped" and not ev.errors, ev.render()
    st = state.read_state(c.ssm, "dev")
    assert st.mode == "capped" and st.by == "evaluator"
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    assert g["AutoScalingGroups"][0]["MinSize"] == 0
    line = json.loads(out.getvalue().splitlines()[-1])
    assert line["EndpointOnline"] == 1 and line["Deployment"] == "dev"
    assert line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Deployment"]]


@pytest.mark.timeout(60)
def test_evaluator_renews_the_lease_only_while_online(
    env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = env
    c = replace(c, scheduler=aws("scheduler"))
    deployment.deploy(c, replace(spec, lease_s=4 * 3600))
    before = lease.expiry(c.scheduler, "dev")
    assert before is not None
    later = time.time() + 2 * 3600  # past the renewal interval
    off = evaluate(c, aws, endpoint_id=EID, online=lambda _: False, now=later, out=io.StringIO())
    assert off.online is False and off.lease_renewed == 0
    assert lease.expiry(c.scheduler, "dev") == before  # a dead endpoint lets the lease run out
    on = evaluate(c, aws, endpoint_id=EID, online=lambda _: True, now=later, out=io.StringIO())
    assert on.lease_renewed >= 1 and lease.expiry(c.scheduler, "dev") > before  # type: ignore[operator]
    soon = evaluate(c, aws, endpoint_id=EID, online=lambda _: True, now=later + 300,
                    out=io.StringIO())  # fmt: skip
    assert soon.lease_renewed == 0  # hourly, not every pass


def test_evaluator_reports_globus_errors(env: tuple[Clients, DeploymentSpec],
                                         aws: Callable[[str], Any]) -> None:  # fmt: skip
    c, spec = env
    deployment.deploy(c, spec)

    def broken(_: str) -> bool:
        raise RuntimeError("token expired")

    ev = evaluate(c, aws, endpoint_id=EID, online=broken, out=io.StringIO())
    assert ev.online is None and any("token expired" in e for e in ev.errors)
    assert "status unknown" in ev.render()
