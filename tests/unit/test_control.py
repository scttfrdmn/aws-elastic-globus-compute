"""aegc stop / resume / limits / cost, and redeploy helpers, against substrate."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import pytest

from aegc import control, ledger, pricing, state
from aegc.infra import deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from tests.unit.test_deployment import ASG, POOL, outcomes
from tests.unit.test_deployment import env as env  # the fixture

NOON = 1_800_000_000 - 1_800_000_000 % 86400 + 12 * 3600


def deployed(env: tuple[Clients, DeploymentSpec], *, min_vcpus: int = 0) -> Clients:
    c, spec = env
    pool = PoolSpec(**{**asdict(POOL), "min_vcpus": min_vcpus})
    deployment.deploy(c, DeploymentSpec(**{**spec.__dict__, "pools": (pool,)}))
    return c


def min_size(c: Clients) -> int:
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    return int(g["AutoScalingGroups"][0]["MinSize"])


@pytest.mark.timeout(60)
def test_stop_and_resume(env: tuple[Clients, DeploymentSpec]) -> None:
    c = deployed(env, min_vcpus=2)
    assert min_size(c) == 2
    r = control.stop(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev")
    st = state.read_state(c.ssm, "dev")
    assert st.mode == "stopped" and not st.kill and "state stopped" in str(r)
    assert min_size(c) == 0  # workers can leave; desired is left to them
    control.resume(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev")
    assert state.read_state(c.ssm, "dev").running and min_size(c) == 2


@pytest.mark.timeout(60)
def test_hard_stop_kills_and_terminates_stragglers(env: tuple[Clients, DeploymentSpec]) -> None:
    from tests.substrate_gaps import launch_on_substrate

    c = deployed(env)
    launch_on_substrate(c.autoscaling, ASG)  # substrate#1497
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=1)
    r = control.stop(
        ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", hard=True, sleep=lambda _: None
    )
    assert state.read_state(c.ssm, "dev").kill
    assert "terminated" in str(r)


@pytest.mark.timeout(60)
def test_limits_change_the_manifest(env: tuple[Clients, DeploymentSpec]) -> None:
    c = deployed(env)
    before = deployment.read_manifest(c.ssm, "dev")
    r = control.set_limits(
        ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", max_usd_day=5.0, on_cap="kill"
    )
    after = deployment.read_manifest(c.ssm, "dev")
    assert after["limits"]["max_usd_day"] == 5.0 and after["limits"]["on_cap"] == "kill"
    assert after["generation"] != before["generation"]  # running engines reload
    assert "$5/day" in str(r)
    control.set_limits(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", max_usd_day="none")
    assert deployment.read_manifest(c.ssm, "dev")["limits"]["max_usd_day"] is None


@pytest.mark.timeout(60)
def test_limits_resume_ends_a_cap(env: tuple[Clients, DeploymentSpec]) -> None:
    c = deployed(env)
    state.write_state(c.ssm, "dev", state.State(mode="capped", reason="x", until=4e9))
    control.set_limits(
        ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", max_usd_day=10.0, resume_now=True
    )
    assert state.read_state(c.ssm, "dev").running


@pytest.mark.timeout(60)
def test_cost_from_the_ledger(aws: Callable[[str], Any]) -> None:
    s3 = aws("s3")
    s3.create_bucket(
        Bucket="aegc-cost-test", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )
    for pool, hours in (("a", 2.0), ("b", 1.0)):
        ledger.write(
            s3, "aegc-cost-test",
            ledger.Record(
                instance_id=f"i-{pool}", deployment="dev", pool=pool, instance_type="c8g.large",
                availability_zone="us-west-2a", lifecycle="on-demand", vcpus=2,
                launched_at=NOON - hours * 3600, last_seen=NOON - 1, exit_reason="idle",
            ),
        )  # fmt: skip
    r = control.cost(
        s3=s3,
        bucket="aegc-cost-test",
        deployment="dev",
        prices=pricing.Prices(region="us-west-2"),
        now=NOON,
    )
    per_hour = 0.0798 + pricing.volume_hour(30, 125) + pricing.IPV4_HOUR
    assert r.accrued.usd == pytest.approx(3 * per_hour, rel=1e-3)
    assert r.accrued.instance_hours == pytest.approx(3.0, rel=1e-3)
    assert list(r.accrued.by_pool) == ["a", "b"] and "static" in r.accrued.sources
    assert "about $" in r.render()
    assert control.window_start("24h", NOON) == NOON - 86400
    with pytest.raises(ValueError):
        control.window_start("yesterday", NOON)


def test_pool_from_a_v01_manifest() -> None:
    # v0.1 counted workers; redeploying converts to vCPUs at the pool's smallest size.
    p = PoolSpec.from_manifest(
        "sci-py314",
        {"env": "sci", "python_minor": "3.14", "image": POOL.image, "max_workers": 3,
         "min_workers": 1, "vcpu_min": 4, "vcpu_max": 16, "task_queue_url": "ignored"},
    )  # fmt: skip
    assert (p.max_vcpus, p.min_vcpus) == (12, 4)
    again = PoolSpec.from_manifest("sci-py314", {**asdict(p)})
    assert again == p


@pytest.mark.timeout(60)
def test_prune_pool(env: tuple[Clients, DeploymentSpec]) -> None:
    c = deployed(env)
    entry = deployment.read_manifest(c.ssm, "dev")["pools"][POOL.name]
    c.sqs.send_message(QueueUrl=entry["task_queue_url"], MessageBody="{}")
    with pytest.raises(RuntimeError, match="queued or running"):
        deployment.prune_pool(c, "dev", POOL.name, entry, deployment.Report(), dry_run=False)
    dry = deployment.Report()
    deployment.prune_pool(c, "dev", POOL.name, entry, dry, dry_run=True, force=True)
    assert outcomes(dry)[("auto scaling group", ASG)] == "would-delete"
    done = deployment.Report()
    deployment.prune_pool(
        c, "dev", POOL.name, entry, done, dry_run=False, force=True, wait_s=30, poll_s=0.1
    )
    o = outcomes(done)
    assert o[("auto scaling group", ASG)] == "deleted"
    assert o[("sqs queue", "aegc-dev-sci-py314-tasks")] == "deleted"
    assert not c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])[
        "AutoScalingGroups"
    ]
