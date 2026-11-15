"""Regression tests for the v0.2 review findings (each would have failed before its fix)."""

# ruff: noqa: F811  (fixtures imported from the other test modules)

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import pytest

from aegc import control, engine, lease, ledger, pricing, spend, state
from aegc.infra import deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from aegc.worker.config import WorkerConfig
from tests.conftest import REGION
from tests.unit.test_deployment import ASG, POOL
from tests.unit.test_deployment import env as env  # the fixture
from tests.unit.test_engine import (
    GROUP,
    NOON,
    Env,
    code_of,
    queued,
    set_limits,
    submit,
    triple,
)
from tests.unit.test_engine import env as engine_env  # noqa: F401  (fixture)
from tests.unit.test_worker import Rig, rig  # noqa: F401  (fixtures)


class _Quiet:
    """Stands in for the engine's wake-up event: submit() then doesn't wake the real-time
    scaler, which would race the test's fake clock."""

    def set(self) -> None: ...

    def clear(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool:
        time.sleep(min(timeout or 0, 0.05))
        return False


def quiet(e: engine.AWSEngine) -> engine.AWSEngine:
    e._scale_now = _Quiet()  # type: ignore[assignment]
    return e


def auto_slots(e: engine.AWSEngine) -> None:
    assert e._manifest is not None
    e._manifest["pools"]["geo-py314"]["slots"] = "auto"  # the default: one task per vCPU


@pytest.mark.timeout(60)
def test_1_one_task_under_a_limit_gets_a_worker(engine_env: Env) -> None:
    # Finding 1: 1 task = 1 vCPU, rounded down to whole 2-vCPU workers = 0: the task hung.
    e = quiet(engine_env.engine())
    try:
        auto_slots(e)
        set_limits(engine_env, e, max_usd_day=5.0)
        auto_slots(e)  # set_limits reloads the manifest
        submit(e, triple, 1)
        m = e.scale_once(now=NOON).pools["geo-py314"]
        assert m.admitted == 2 and engine_env.group()["DesiredCapacity"] == 2
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_1_tasks_blocked_by_limits_fail_clearly(engine_env: Env) -> None:
    e = quiet(engine_env.engine())
    try:
        set_limits(engine_env, e, max_usd_day=0.10)  # less than an hour of one worker x 1.2
        f = submit(e, triple, 1)
        e.scale_once(now=NOON)
        assert not f.done()
        e.scale_once(now=NOON + engine.BLOCKED_FAIL_S + 1)
        assert code_of(f) == "AEGC_BUDGET_EXCEEDED"
        assert queued(engine_env) == []
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_4_redeploy_while_stopped_keeps_minimum_zero(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    pool = PoolSpec(**{**asdict(POOL), "min_vcpus": 2})
    spec = DeploymentSpec(**{**spec.__dict__, "pools": (pool,)})
    deployment.deploy(c, spec)
    control.stop(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev")
    deployment.deploy(c, spec)  # e.g. to change a limit
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    assert g["AutoScalingGroups"][0]["MinSize"] == 0
    control.resume(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev")
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    assert g["AutoScalingGroups"][0]["MinSize"] == 2


@pytest.mark.timeout(60)
def test_5_lifetime_at_the_minimum_is_replaced(rig: Rig) -> None:
    me = rig.join_group(min_size=1)
    rig.cfg = WorkerConfig(**{**rig.cfg.__dict__, "max_lifetime_s": 0})
    assert rig.agent().run(max_cycles=5) == "lifetime"
    g = rig.group()
    # Not decremented below the minimum: terminated for a replacement.
    assert g["DesiredCapacity"] == 1
    states = {i["InstanceId"]: i["LifecycleState"] for i in g["Instances"]}
    assert states.get(me, "Terminating").startswith("Terminat")


@pytest.mark.timeout(60)
def test_6_purge_receives_foreign_messages_once(engine_env: Env) -> None:
    from aegc import protocol

    e = engine_env.engine()
    other = engine_env.sqs.create_queue(QueueName="aegc-dev-res-000000000000")["QueueUrl"]
    try:
        mine = [submit(e, triple, i) for i in range(2)]
        for (msg,) in [[m] for m in queued(engine_env)][:1]:
            foreign = protocol.TaskMessage(**{**msg.__dict__, "task_uuid": str(msg.task_uuid)[::-1],
                                              "reply_to": other})  # fmt: skip
        for _ in range(3):
            engine_env.sqs.send_message(
                QueueUrl=engine_env.task_queue, MessageBody=foreign.encode()
            )
        e.fail_owned("AEGC_STOPPED", "test")
        assert {code_of(f) for f in mine} == {"AEGC_STOPPED"}
        left = engine_env.sqs.receive_message(
            QueueUrl=engine_env.task_queue, MaxNumberOfMessages=10,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )["Messages"]  # fmt: skip
        assert len(left) == 3
        # Received once by the purge, once now: never more.
        assert {int(m["Attributes"]["ApproximateReceiveCount"]) for m in left} == {2}
    finally:
        e.shutdown()


def test_7_short_leases() -> None:
    assert lease.renew_interval(48 * 3600) == 3600
    assert lease.renew_interval(3 * 3600) == 3600
    with pytest.raises(ValueError, match="3 h"):
        lease.check_lease(1800)
    assert lease.check_lease(0) == 0  # no lease


@pytest.mark.timeout(60)
def test_8_capped_deployments_keep_their_lease(
    engine_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    renewed: list[str] = []
    monkeypatch.setattr(lease, "renew", lambda _s, d, _l, now=None: renewed.append(d) or 2)
    e = engine_env.engine()
    try:
        e._lease_renewed = -1e18
        renewed.clear()  # the engine's first real-time pass may have renewed already
        e._set_state(state.State(mode="capped", reason="x", until=NOON + 86400))
        assert e.renew_lease(now=NOON) and renewed == ["dev"]
        e._set_state(state.State(mode="stopped"))
        assert not e.renew_lease(now=NOON + 7200)
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_9_resume_refuses_while_still_over(
    env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = env
    deployment.deploy(c, spec)
    s3 = aws("s3")
    s3.create_bucket(Bucket="aegc-review", CreateBucketConfiguration={"LocationConstraint": REGION})
    now = time.time()
    ledger.write(s3, "aegc-review", ledger.Record(
        instance_id="i-1", deployment="dev", pool=POOL.name, instance_type="c8g.large",
        availability_zone="us-west-2a", lifecycle="on-demand", vcpus=2,
        launched_at=now - 3600, last_seen=now - 1, exit_reason="idle"))  # fmt: skip
    guard = spend.SpendGuard(s3=s3, bucket="aegc-review", deployment="dev",
                             prices=pricing.Prices(region=REGION))  # fmt: skip
    control.set_limits(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", max_usd_day=0.05)
    state.write_state(c.ssm, "dev", state.State(mode="capped", reason="x", until=now + 3600))
    with pytest.raises(ValueError, match="still over"):
        control.set_limits(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev",
                           resume_now=True, spend=guard)  # fmt: skip
    assert state.read_state(c.ssm, "dev").mode == "capped"
    control.set_limits(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev",
                       resume_now=True, spend=guard, max_usd_day=5.0)  # fmt: skip
    assert state.read_state(c.ssm, "dev").running


def test_10_ledger_lookback_and_running_slack() -> None:
    from datetime import UTC, datetime, timedelta

    start = NOON - 12 * 3600  # midnight
    first = datetime.fromtimestamp(start, UTC).date() - timedelta(days=ledger.LOOKBACK_DAYS)
    assert ledger.days(start, NOON)[0] == first.isoformat()
    assert len(ledger.days(start, NOON)) == ledger.LOOKBACK_DAYS + 1
    p = pricing.Prices(region=REGION)
    rec = {"launched_at": NOON - 3600, "last_seen": NOON - 900, "instance_type": "c8g.large",
           "lifecycle": "on-demand"}  # fmt: skip
    # Seen 15 minutes ago and not exited: still running (records are written every 5 minutes
    # and read up to 5 minutes late), so the whole hour counts.
    assert pricing.accrue([rec], NOON - 7200, NOON, p).instance_hours == pytest.approx(1.0)


def test_11_admission_near_midnight_uses_a_full_hour() -> None:
    class NoLedger(spend.SpendGuard):
        def records(self, now: float) -> list[dict[str, Any]]:
            return []

    g = NoLedger(s3=None, bucket="", deployment="dev", prices=pricing.Prices(region=REGION))
    lim = state.Limits(max_usd_day=1.0)
    kw = dict(rate_now=0.0, instances_now=0.0, want_units=100, unit_usd_per_hour=0.07,
              unit_instances=0.5)  # fmt: skip
    noon = g.admit(lim, NOON, **kw)  # type: ignore[arg-type]
    late = g.admit(lim, NOON + 12 * 3600 - 30, **kw)  # 23:59:30
    assert late == noon == 11  # floor(1 / 1.2 / 0.07)


def test_b_workers_must_fit_the_pool() -> None:
    with pytest.raises(ValueError, match="larger than"):
        PoolSpec(**{**asdict(POOL), "max_vcpus": 8, "vcpu_max": 16})
    with pytest.raises(ValueError, match="lifetime"):
        PoolSpec(**{**asdict(POOL), "max_lifetime_s": 0})


def test_c_image_versions() -> None:
    from aegc.cli import _version

    assert _version("0.1.0") < (0, 2) <= _version("0.2.0") and _version("0.10.1") > (0, 2)


@pytest.mark.timeout(60)
def test_d_worker_rechecks_before_refusing(rig: Rig) -> None:
    from tests.unit.test_worker import stateful

    a, ssm = stateful(rig)
    state.write_state(ssm, "dev", state.State(mode="stopped"))
    halt = a.check_state()
    assert halt is not None
    tid = rig.submit(pow, 2, 3)
    state.write_state(ssm, "dev", state.State())  # resumed meanwhile
    a._state_checked = -1e18  # its last check is stale
    assert a.fail_queued_once(halt) == 0 and a._halt is None
    a.claim_once()
    a.wait_idle()
    from tests.unit.test_worker import value_of

    assert value_of(rig.final(1)[tid], rig.s3) == (False, 8)


def test_e_a_passed_ttl_isnt_scheduled() -> None:
    spec = lease.LeaseSpec(deployment="dev", region=REGION, account="1", groups={"p": "g"},
                           lease_s=0, ttl_at=NOON - 60)  # fmt: skip
    assert lease.schedules(spec, "arn", now=NOON) == {}


def test_f_failed_price_lookups_are_cached() -> None:
    calls = []

    class Broken:
        def get_products(self, **kw: Any) -> Any:
            calls.append(kw)
            raise RuntimeError("throttled")

    p = pricing.Prices(region=REGION, pricing=Broken())
    for _ in range(5):
        assert p.on_demand("c8g.large") == (0.0798, "static")
    assert len(calls) == 1


@pytest.mark.timeout(60)
def test_cap_reset_restores_minimums(engine_env: Env) -> None:
    e = engine_env.engine()
    try:
        assert e._manifest is not None
        e._manifest["pools"]["geo-py314"]["min_vcpus"] = 2
        engine_env.asg.update_auto_scaling_group(AutoScalingGroupName=GROUP, MinSize=0)
        until = NOON + 3600
        state.write_state(
            engine_env.ssm, "dev", state.State(mode="capped", reason="x", until=until)
        )
        e.refresh_state(now=NOON, force=True)
        assert e._state.mode == "capped"
        e.refresh_state(now=until + 1, force=True)  # the period ended
        assert e._state.running
        g = engine_env.group()
        assert g["MinSize"] == 2
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_a_idle_worker_leaves_a_vcpu_weighted_group(env: tuple[Clients, DeploymentSpec]) -> None:
    # Desired 1 vCPU launches one 2-vCPU worker; its decrement (2) exceeds desired, which the
    # group refuses. The worker lowers desired by its weight and leaves without a replacement.
    from aegc.worker.agent import Agent

    c, spec = env
    deployment.deploy(c, spec)
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=1)
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    (inst,) = g["AutoScalingGroups"][0]["Instances"]
    cfg = WorkerConfig(deployment="dev", pool=POOL.name, region=REGION, task_queue_url="",
                       bucket="b", instance_id=inst["InstanceId"])  # fmt: skip
    a = Agent(config=cfg, sqs=c.sqs, s3=None, autoscaling=c.autoscaling)
    assert a._leave_group()
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    assert g["AutoScalingGroups"][0]["DesiredCapacity"] == 0
    left = {i["InstanceId"]: i["LifecycleState"] for i in g["AutoScalingGroups"][0]["Instances"]}
    assert not left.get(inst["InstanceId"], "Terminating").startswith("InService")


@pytest.mark.timeout(60)
def test_live_1_no_refusal_right_after_resume(engine_env: Env) -> None:
    # Found live: a task submitted just after `aegc resume` was refused from the engine's
    # 30-second-old copy of the state.
    e = engine_env.engine()
    try:
        state.write_state(engine_env.ssm, "dev", state.State(mode="stopped"))
        e.refresh_state(force=True)
        state.write_state(engine_env.ssm, "dev", state.State())  # aegc resume
        e._state_read = time.time() - 10  # the copy is a little stale
        submit(e, triple, 1)
        assert len(queued(engine_env)) == 1  # sent, not refused
    finally:
        e.shutdown()
