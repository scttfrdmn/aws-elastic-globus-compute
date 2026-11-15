"""The deployment lease (EventBridge Scheduler). substrate stores schedules but has no schedule
groups or tags and never fires targets (substrate#1501), so the parts that need those are strict
xfails; what the schedules *do* is tested through lease.fire()."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from aegc import lease, state
from aegc.infra import Report, deployment
from aegc.infra.deployment import Clients, DeploymentSpec
from tests.conftest import ACCOUNT, REGION
from tests.unit.test_deployment import ASG
from tests.unit.test_deployment import env as env  # the fixture

NOW = 1_800_000_000.0
SPEC = lease.LeaseSpec(
    deployment="dev", region=REGION, account=ACCOUNT, groups={"sci-py314": ASG}, lease_s=7200
)


def test_schedule_definitions() -> None:
    arn = f"arn:aws:iam::{ACCOUNT}:role/aegc/aegc-dev-scheduler-{REGION}"
    s = lease.schedules(SPEC, arn, now=NOW)
    assert sorted(s) == ["lease-sci-py314", "lease-state"]
    st = s["lease-state"]
    assert st["GroupName"] == "aegc-dev" and st["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert lease.parse_at(st["ScheduleExpression"]) == NOW + 7200
    assert st["Target"]["Arn"] == lease.SSM_TARGET and st["Target"]["RoleArn"] == arn
    body = json.loads(st["Target"]["Input"])
    assert body["Name"] == "/aegc/dev/state" and body["Overwrite"] is True
    assert state.State.from_json(body["Value"]).mode == "expired"
    pool = json.loads(s["lease-sci-py314"]["Target"]["Input"])
    assert pool == {"AutoScalingGroupName": ASG, "MinSize": 0}
    # A TTL adds ttl-* schedules at an absolute time; no lease means no lease-* schedules.
    ttl = lease.schedules(
        lease.LeaseSpec(**{**SPEC.__dict__, "lease_s": 0, "ttl_at": NOW + 86400}), arn, now=NOW
    )
    assert sorted(ttl) == ["ttl-sci-py314", "ttl-state"]
    assert lease.parse_at(ttl["ttl-state"]["ScheduleExpression"]) == NOW + 86400


def test_durations() -> None:
    assert lease.parse_duration("48h") == 172800 and lease.parse_duration("7d") == 604800
    assert lease.parse_duration("none") == 0
    with pytest.raises(ValueError):
        lease.parse_duration("forever")


@pytest.mark.timeout(60)
def test_firing_freezes_the_deployment(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    c.autoscaling.update_auto_scaling_group(AutoScalingGroupName=ASG, MinSize=2)
    lease.fire(c.ssm, c.autoscaling, "dev", {"sci-py314": ASG})
    assert state.read_state(c.ssm, "dev").mode == "expired"
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])["AutoScalingGroups"]
    assert g[0]["MinSize"] == 0


@pytest.mark.timeout(60)
def test_ensure_renew_destroy(aws: Callable[[str], Any]) -> None:
    sch, iam = aws("scheduler"), aws("iam")
    r = Report()
    lease.ensure_lease(scheduler=sch, iam=iam, spec=SPEC, report=r, dry_run=False, now=NOW)
    assert lease.expiry(sch, "dev") == NOW + 7200
    assert lease.renew(sch, "dev", 7200, now=NOW + 3600) == 2
    assert lease.expiry(sch, "dev") == NOW + 3600 + 7200
    lease.destroy_lease(scheduler=sch, iam=iam, deployment="dev", region=REGION, report=r,
                        dry_run=False)  # fmt: skip
    assert lease.expiry(sch, "dev") is None


def test_new_role_is_retried() -> None:
    from botocore.exceptions import ClientError

    calls = []

    def create() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "The execution role you "
                           "provide must allow AWS EventBridge Scheduler to assume the role."}},
                "CreateSchedule",
            )  # fmt: skip
        return "ok"

    assert lease._with_role_retry(create, sleep=lambda _: None) == "ok" and len(calls) == 3
