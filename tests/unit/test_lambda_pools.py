"""Lambda pools (#21) in the engine and the controls, against substrate.

Manifests get their Lambda pool entries by hand (deploy creates them in aegc.infra), each with a
real function (PackageType=Image) and SQS event source mapping in substrate.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aegc import (
    control,
    evaluator,
    lambda_pools,
    lease,
    ledger,
    naming,
    ops,
    pricing,
    protocol,
    state,
)
from aegc.infra import Report, deployment
from aegc.infra.deployment import Clients, DeploymentSpec
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import no_network_interfaces
from tests.unit.test_deployment import ASG, POOL
from tests.unit.test_engine import BUCKET, IMAGE, NOON, Env, code_of, submit, triple
from tests.unit.test_engine import set_limits as engine_limits

GEO_LAM = "geo-lam"


def add_lambda_pool(
    aws: Callable[[str], Any],
    name: str,
    *,
    env: str = "geospatial",
    python: str = "3.14",
    memory_mib: int = 2048,
    timeout_s: int = 900,
    concurrency: int = 10,
    **extra: Any,
) -> dict[str, Any]:
    """Create a Lambda pool's queues, function and mapping, and add it to dev's manifest."""
    sqs, lam, iam, ssm = aws("sqs"), aws("lambda"), aws("iam"), aws("ssm")
    fn = lambda_pools.function_name("dev", name)
    tasks = sqs.create_queue(QueueName=f"aegc-dev-{name}-tasks")["QueueUrl"]
    dlq = sqs.create_queue(QueueName=f"aegc-dev-{name}-dlq")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=tasks, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
             "Principal": {"Service": "lambda.amazonaws.com"}}]}  # fmt: skip
    role = iam.create_role(
        RoleName=lambda_pools.function_role("dev", name, REGION),
        AssumeRolePolicyDocument=json.dumps(trust),
    )["Role"]["Arn"]
    f = lam.create_function(
        FunctionName=fn, PackageType="Image", Code={"ImageUri": IMAGE}, Role=role,
        MemorySize=memory_mib, Timeout=timeout_s, Architectures=["arm64"],
    )  # fmt: skip
    esm = lam.create_event_source_mapping(
        EventSourceArn=queue_arn, FunctionName=fn, BatchSize=1,
        ScalingConfig={"MaximumConcurrency": concurrency}, Tags={"aegc:deployment": "dev"},
    )  # fmt: skip
    entry = {
        "kind": "lambda", "env": env, "python_minor": python, "image": IMAGE,
        "memory_mib": memory_mib, "timeout_s": timeout_s, "max_concurrency": concurrency,
        "task_queue_url": tasks, "dlq_url": dlq, "function_name": fn,
        "function_arn": f["FunctionArn"], "event_source_mapping": esm["UUID"],
        "function_role_arn": role, **extra,
    }  # fmt: skip
    m = deployment.read_manifest(ssm, "dev")
    m["pools"][name] = entry
    m["generation"] = deployment.manifest_generation(m)
    ssm.put_parameter(Name="/aegc/dev/manifest", Value=json.dumps(m), Type="String", Overwrite=True)
    return entry


def mapping(aws: Callable[[str], Any], entry: dict[str, Any]) -> str:
    return lambda_pools.mapping_state(aws("lambda"), entry)


def pool_queue(env: Env, name: str) -> list[protocol.TaskMessage]:
    url = env.sqs.get_queue_url(QueueName=f"aegc-dev-{name}-tasks")["QueueUrl"]
    r = env.sqs.receive_message(
        QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=0, VisibilityTimeout=0
    )
    return [protocol.TaskMessage.decode(m["Body"]) for m in r.get("Messages", [])]


@pytest.fixture
def env(aws: Callable[[str], Any], tmp_path: Path, substrate: Any) -> Env:
    return Env(aws, tmp_path, substrate.url)


# --- routing and walltime --------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_tasks_go_to_a_lambda_pool_only_by_name_or_when_only_it_serves_the_env(env: Env) -> None:
    add_lambda_pool(env.aws, GEO_LAM)  # same env and Python as the EC2 pool geo-py314
    add_lambda_pool(env.aws, "fast-lam", env="fast")  # the only pool for env "fast"
    add_lambda_pool(env.aws, "old-lam", env="mixed", python="3.13")
    e = env.engine()
    try:
        submit(e, triple, 1)  # env geospatial, no pool named: the EC2 pool
        assert [m.pool for m in pool_queue(env, GEO_LAM)] == []
        submit(e, triple, 2, spec={"pool": GEO_LAM})
        assert [m.pool for m in pool_queue(env, GEO_LAM)] == [GEO_LAM]
        submit(e, triple, 3, spec={"env": "fast"})
        assert [m.pool for m in pool_queue(env, "fast-lam")] == ["fast-lam"]
        # Python 3.14 client, env "mixed" has only a 3.13 Lambda pool: refused, not misrouted.
        f = submit(e, triple, 4, spec={"env": "mixed"})
        assert code_of(f) == "AEGC_CONFIGURATION"
    finally:
        e.shutdown()
    # A pool= default in user_endpoint_config names it too.
    e = env.engine(pool=GEO_LAM)
    try:
        submit(e, triple, 5)
        assert len(pool_queue(env, GEO_LAM)) == 2
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_python_served_only_by_a_lambda_pool_goes_there(env: Env) -> None:
    add_lambda_pool(env.aws, "geo-lam-313", python="3.13")  # EC2 geo-py314 is 3.14
    e = env.engine(python_minor="3.13")
    try:
        submit(e, triple, 1)
        assert [m.pool for m in pool_queue(env, "geo-lam-313")] == ["geo-lam-313"]
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_walltime_past_the_function_timeout_is_refused(env: Env) -> None:
    add_lambda_pool(env.aws, GEO_LAM, timeout_s=300)
    e = env.engine()
    try:
        submit(e, triple, 1, spec={"pool": GEO_LAM})  # default: the most the function allows
        (msg,) = pool_queue(env, GEO_LAM)
        assert msg.walltime_s == 300 - lambda_pools.MARGIN_S
        f = submit(e, triple, 1, spec={"pool": GEO_LAM, "walltime_s": 600})
        assert code_of(f) == "AEGC_CONFIGURATION"
        from aegc.worker.testing import unpack_result

        text = unpack_result(f.result())[1]
        assert "aegc-dev-geo-lam times out after 300 s" in text and "at most 280 s" in text
    finally:
        e.shutdown()


# --- scaling, spend and caps -----------------------------------------------------------------


@pytest.mark.timeout(60)
def test_lambda_pools_are_not_scaled_but_reported(env: Env) -> None:
    add_lambda_pool(env.aws, GEO_LAM, concurrency=7)
    calls: list[dict[str, Any]] = []
    env.asg.meta.events.register(
        "provide-client-params.auto-scaling.*", lambda params, **_: calls.append(dict(params))
    )
    e = env.engine()
    try:
        for i in range(3):
            submit(e, triple, i, spec={"pool": GEO_LAM})
        calls.clear()
        snap = e.scale_once()
        m = snap.pools[GEO_LAM]
        assert (m.kind, m.visible, m.max, m.mapping) == ("lambda", 3, 7, "Enabled")
        assert all(GEO_LAM not in json.dumps(c) for c in calls), calls
        report = e.get_status_report().global_state["pools"][GEO_LAM]
        assert report["kind"] == "lambda" and report["queue_visible"] == 3
        assert report["mapping"] == "Enabled" and "asg_desired" not in report
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_admission_counts_lambda_pools_at_their_worst_case(env: Env) -> None:
    add_lambda_pool(env.aws, GEO_LAM, memory_mib=2048, concurrency=10)
    e = env.engine()
    try:
        # Worst case: 10 x 2 GB busy for an hour, about $0.96/h. $1.50/day leaves 1.50/1.2 - 0.96
        # = $0.29/h for EC2: 4 vCPUs (2 workers) at $0.0686 per vCPU, of the 8 wanted.
        worst = 10 * pricing.lambda_hourly(2048)
        assert worst == pytest.approx(0.96, abs=0.001)
        engine_limits(env, e, max_usd_day=1.50)
        env.demand(8)
        m = e.scale_once(now=NOON).pools["geo-py314"]
        assert m.admitted == 4 and env.group()["DesiredCapacity"] == 4
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_cap_trip_disables_mappings_and_the_next_period_enables_them(env: Env) -> None:
    entry = add_lambda_pool(env.aws, GEO_LAM)
    e = env.engine()
    try:
        engine_limits(env, e, max_usd_day=0.05)
        ledger.write(env.s3, BUCKET, ledger.Record(
            instance_id="lambda-1", deployment="dev", pool=GEO_LAM, instance_type="",
            availability_zone="", lifecycle="lambda", vcpus=0, launched_at=NOON - 3600,
            last_seen=NOON - 60, kind="lambda", memory_mib=2048, billed_s=3600, invocations=10,
        ))  # fmt: skip
        e.scale_once(now=NOON)
        assert state.read_state(env.ssm, "dev", now=NOON).mode == "capped"
        assert mapping(env.aws, entry) == "Disabled"
        # The cap's period ends: the state reads running again and the scaler enables the pool.
        later = state.period_end("day", NOON) + 60
        e.refresh_state(later, force=True)
        assert e.scale_once(now=later).pools[GEO_LAM].mapping in ("Enabled", "Enabling")
        assert mapping(env.aws, entry) == "Enabled"
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_scaler_doesnt_enable_a_mapping_of_a_stopped_deployment(env: Env) -> None:
    entry = add_lambda_pool(env.aws, GEO_LAM)
    e = env.engine()
    try:
        assert e._state.running  # cached: a stop has just happened behind its back
        state.write_state(env.ssm, "dev", state.State(mode="stopped", by="aegc stop"))
        lambda_pools.set_enabled(env.aws("lambda"), entry, False)
        e.scale_once()
        assert mapping(env.aws, entry) == "Disabled" and not e._state.running
        # And a mapping left enabled while not running (a failed update) is disabled.
        lambda_pools.set_enabled(env.aws("lambda"), entry, True)
        e.scale_once()
        assert mapping(env.aws, entry) == "Disabled"
    finally:
        e.shutdown()


# --- controls --------------------------------------------------------------------------------


@pytest.fixture
def dep_env(aws: Callable[[str], Any]) -> tuple[Clients, DeploymentSpec]:
    """As test_deployment's env: an EC2 pool sci-py314 in the default VPC."""
    c = Clients(
        iam=aws("iam"), ec2=no_network_interfaces(aws("ec2")), autoscaling=aws("autoscaling"),
        sqs=aws("sqs"), logs=aws("logs"), ssm=aws("ssm"),
    )  # fmt: skip
    spec = DeploymentSpec(deployment="dev", account=ACCOUNT, region=REGION,
                          bucket=naming.bucket_name(ACCOUNT, REGION), pools=(POOL,))  # fmt: skip
    return c, spec


def deployed(c: Clients, spec: DeploymentSpec, aws: Callable[[str], Any]) -> dict[str, Any]:
    deployment.deploy(c, spec)
    c.autoscaling.update_auto_scaling_group(AutoScalingGroupName=ASG, MinSize=2, MaxSize=8)
    m = deployment.read_manifest(c.ssm, "dev")
    m["pools"]["sci-py314"]["min_vcpus"] = 2
    m["generation"] = deployment.manifest_generation(m)
    c.ssm.put_parameter(Name="/aegc/dev/manifest", Value=json.dumps(m), Type="String",
                        Overwrite=True)  # fmt: skip
    return add_lambda_pool(aws, GEO_LAM)


def min_size(c: Clients) -> int:
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    return int(g["AutoScalingGroups"][0]["MinSize"])


@pytest.mark.timeout(60)
def test_stop_and_resume_disable_and_enable_mappings(
    dep_env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = dep_env
    entry = deployed(c, spec, aws)
    lam = aws("lambda")
    r = control.stop(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", lam=lam)
    assert mapping(aws, entry) == "Disabled" and min_size(c) == 0
    assert "pool geo-lam (lambda 2048 MB x 10): event source mapping disabled" in str(r)
    r = control.resume(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", lam=lam)
    assert mapping(aws, entry) == "Enabled" and min_size(c) == 2
    assert "event source mapping enabled" in str(r)
    # --hard: nothing to terminate for Lambda; its running invocations finish.
    r = control.stop(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", lam=lam, hard=True,
                     sleep=lambda _: None)  # fmt: skip
    assert mapping(aws, entry) == "Disabled" and "invocations already running finish" in str(r)
    # Without a Lambda client, the report says the pool wasn't frozen.
    control.resume(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", lam=lam)
    r = control.stop(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev")
    assert "event source mapping NOT disabled (no Lambda client)" in str(r)


@pytest.mark.timeout(60)
def test_limits_resume_enables_mappings(
    dep_env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = dep_env
    entry = deployed(c, spec, aws)
    lam = aws("lambda")
    cap = state.State(mode="capped", reason="$1 of $1/day", by="engine",
                      until=time.time() + 3600)  # fmt: skip
    state.write_state(c.ssm, "dev", cap)
    lambda_pools.set_enabled(lam, entry, False)
    control.set_limits(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", resume_now=True,
                       lam=lam, max_usd_day=5.0)  # fmt: skip
    assert state.read_state(c.ssm, "dev").running and mapping(aws, entry) == "Enabled"


@pytest.mark.timeout(60)
def test_evaluator_with_mixed_pools(
    dep_env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    from dataclasses import replace

    c, spec = dep_env
    s3 = aws("s3")
    s3.create_bucket(Bucket=spec.bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    entry = deployed(c, replace(spec, limits={"max_usd_day": 0.01}), aws)
    now = NOON
    ledger.write(s3, spec.bucket, ledger.Record(
        instance_id="i-1", deployment="dev", pool="sci-py314", instance_type="c8g.large",
        availability_zone="us-west-2a", lifecycle="on-demand", vcpus=2,
        launched_at=now - 3600, last_seen=now - 1, exit_reason=""))  # fmt: skip
    clients = {"ssm": c.ssm, "s3": s3, "autoscaling": c.autoscaling, "scheduler": aws("scheduler"),
               "sqs": c.sqs, "lambda": aws("lambda")}  # fmt: skip
    ev = evaluator.evaluate(deployment="dev", region=REGION, clients=clients, now=now)
    assert ev.state == "capped" and not ev.errors, ev.render()
    assert min_size(c) == 0 and mapping(aws, entry) == "Disabled"
    # The next day the cap has ended; nobody wrote the state, so the evaluator enables the pool.
    later = state.period_end("day", now) + 60
    dry = evaluator.evaluate(deployment="dev", region=REGION, clients=clients, now=later,
                             dry_run=True)  # fmt: skip
    assert dry.mappings == {GEO_LAM: "enabled"} and mapping(aws, entry) == "Disabled"
    assert "would enable Lambda pool geo-lam's event source mapping" in dry.render()
    ev = evaluator.evaluate(deployment="dev", region=REGION, clients=clients, now=later)
    assert ev.state == "running" and ev.mappings == {GEO_LAM: "enabled"}, ev.render()
    assert mapping(aws, entry) == "Enabled"


# --- the lease -------------------------------------------------------------------------------

LEASE = lease.LeaseSpec(
    deployment="dev", region=REGION, account=ACCOUNT, groups={"sci-py314": ASG},
    mappings={GEO_LAM: "0b9f1a52-0000-4000-8000-000000000001"}, lease_s=7200,
)  # fmt: skip
NOW = 1_800_000_000.0


def test_lease_schedules_disable_mappings() -> None:
    arn = f"arn:aws:iam::{ACCOUNT}:role/aegc/aegc-dev-scheduler-{REGION}"
    s = lease.schedules(LEASE, arn, now=NOW)
    assert sorted(s) == ["lease-geo-lam", "lease-sci-py314", "lease-state"]
    t = s["lease-geo-lam"]["Target"]
    assert t["Arn"] == "arn:aws:scheduler:::aws-sdk:lambda:updateEventSourceMapping"
    assert json.loads(t["Input"]) == {"UUID": LEASE.mappings[GEO_LAM], "Enabled": False}
    assert t["RoleArn"] == arn
    ttl = lease.schedules(replace_spec(lease_s=0, ttl_at=NOW + 86400), arn, now=NOW)
    assert sorted(ttl) == ["ttl-geo-lam", "ttl-sci-py314", "ttl-state"]


def replace_spec(**kw: Any) -> lease.LeaseSpec:
    return lease.LeaseSpec(**{**LEASE.__dict__, **kw})


def test_lease_role_may_update_only_this_deployments_mappings() -> None:
    _, policy = lease._role_documents(LEASE)
    (st,) = [s for s in policy["Statement"] if s["Action"] == "lambda:UpdateEventSourceMapping"]
    assert st["Resource"] == f"arn:aws:lambda:{REGION}:{ACCOUNT}:event-source-mapping:*"
    assert st["Condition"]["StringEquals"] == {"aws:ResourceTag/aegc:deployment": "dev"}
    assert st["Condition"]["ArnLike"] == {
        "lambda:FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:aegc-dev-*"
    }


@pytest.mark.timeout(60)
def test_lease_with_mappings_ensure_renew_fire(
    dep_env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = dep_env
    entry = deployed(c, spec, aws)
    sch, lam = aws("scheduler"), aws("lambda")
    lspec = replace_spec(mappings={GEO_LAM: entry["event_source_mapping"]})
    lease.ensure_lease(scheduler=sch, iam=c.iam, spec=lspec, report=Report(), dry_run=False,
                       now=NOW)  # fmt: skip
    got = sch.get_schedule(Name="lease-geo-lam", GroupName="aegc-dev")
    assert json.loads(got["Target"]["Input"]) == {
        "UUID": entry["event_source_mapping"],
        "Enabled": False,
    }
    assert lease.renew(sch, "dev", 7200, now=NOW + 3600) == 3
    got = sch.get_schedule(Name="lease-geo-lam", GroupName="aegc-dev")
    assert lease.parse_at(got["ScheduleExpression"]) == NOW + 3600 + 7200
    assert got["Target"]["Arn"] == lease.LAMBDA_TARGET
    # substrate stores the schedules but doesn't fire them (substrate#1501, #1511): fire() does
    # what they do.
    lease.fire(c.ssm, c.autoscaling, "dev", {"sci-py314": ASG}, lam=lam,
               mappings=lspec.mappings)  # fmt: skip
    assert state.read_state(c.ssm, "dev").mode == "expired"
    assert min_size(c) == 0 and mapping(aws, entry) == "Disabled"
    # Dropping the Lambda pool drops its schedule.
    lease.ensure_lease(scheduler=sch, iam=c.iam, spec=replace_spec(mappings={}), report=Report(),
                       dry_run=False, now=NOW)  # fmt: skip
    names = {s["Name"] for s in sch.list_schedules(GroupName="aegc-dev")["Schedules"]}
    assert names == {"lease-state", "lease-sci-py314"}
    lease.destroy_lease(scheduler=sch, iam=c.iam, deployment="dev", region=REGION,
                        report=Report(), dry_run=False)  # fmt: skip
    assert lease.expiry(sch, "dev") is None


# --- status, metrics, cost -------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_status_shows_lambda_pools(
    dep_env: tuple[Clients, DeploymentSpec], aws: Callable[[str], Any]
) -> None:
    c, spec = dep_env
    entry = deployed(c, spec, aws)
    for i in range(2):
        c.sqs.send_message(QueueUrl=entry["task_queue_url"], MessageBody=f"t{i}")
    c.sqs.receive_message(QueueUrl=entry["task_queue_url"], MaxNumberOfMessages=1)  # running
    st = ops.status(ssm=c.ssm, sqs=c.sqs, autoscaling=c.autoscaling, deployment="dev",
                    region=REGION, lam=aws("lambda"))  # fmt: skip
    text = st.render()
    assert (
        "pool geo-lam: env geospatial, Python 3.14 | tasks queued 1, running 1, dead-lettered 0 | "
        "lambda 2048 MB x 10, event source mapping Enabled"
    ) in text, text
    assert "1.16 vCPU(s), 2 GiB per task" in text
    (p,) = [p for p in st.to_dict()["pools"] if p["name"] == GEO_LAM]
    assert p["kind"] == "lambda" and p["mapping"] == "Enabled" and p["max_capacity"] == 10
    assert p["usd_per_hour"] == pytest.approx(pricing.lambda_hourly(2048))
    assert "vCPUs desired, max 8" in text  # the EC2 pool still renders as before
    # No Lambda client: still no crash.
    text = ops.status(ssm=c.ssm, sqs=c.sqs, autoscaling=c.autoscaling, deployment="dev",
                      region=REGION).render()  # fmt: skip
    assert "event source mapping unknown" in text


class Metrics:
    """GetMetricData with no data (substrate#1595), recording the queries."""

    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []

    def get_metric_data(self, **kw: Any) -> dict[str, Any]:
        self.queries += kw["MetricDataQueries"]
        return {"MetricDataResults": []}


def test_metrics_read_the_function_not_a_group() -> None:
    from aegc import observability

    manifest = {"pools": {GEO_LAM: {"kind": "lambda", "function_name": "aegc-dev-geo-lam"},
                          "sci-py314": {"auto_scaling_group": ASG}}}  # fmt: skip
    cw = Metrics()
    s = observability.metrics(cw=cw, manifest=manifest, deployment="dev", region=REGION,
                              start=NOW - 3600, end=NOW)  # fmt: skip
    metrics = [q["MetricStat"]["Metric"] for q in cw.queries]
    lam = [m for m in metrics if m["Namespace"] == "AWS/Lambda"]
    assert {m["MetricName"] for m in lam} == {"ConcurrentExecutions", "Throttles"}
    assert all(m["Dimensions"] == [{"Name": "FunctionName", "Value": "aegc-dev-geo-lam"}]
               for m in lam)  # fmt: skip
    groups = [m for m in metrics if m["Namespace"] == "AWS/AutoScaling"]
    assert {d["Value"] for m in groups for d in m["Dimensions"]} == {ASG}
    (p,) = [p for p in s.pools if p.name == GEO_LAM]
    assert "concurrent executions max ?, throttles 0" in "\n".join(p.render())


@pytest.mark.timeout(60)
def test_cost_shows_lambda(aws: Callable[[str], Any]) -> None:
    s3 = aws("s3")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    ledger.write(s3, BUCKET, ledger.Record(
        instance_id="lambda-1", deployment="dev", pool=GEO_LAM, instance_type="",
        availability_zone="", lifecycle="lambda", vcpus=0, launched_at=NOON - 600,
        last_seen=NOON - 60, kind="lambda", memory_mib=2048, billed_s=360, invocations=4,
    ))  # fmt: skip
    r = control.cost(s3=s3, bucket=BUCKET, deployment="dev", prices=pricing.Prices(region=REGION),
                     by="type", now=NOON)  # fmt: skip
    usd = pricing.lambda_cost(360, 2048, 4)
    assert f"  lambda-2048MB: ${usd:.4f}" in r.render()
    r.by = "pool"
    assert f"  geo-lam: ${usd:.4f}" in r.render()


# --- the contract helpers --------------------------------------------------------------------


def test_contract_helpers() -> None:
    pools = {
        "a": {"kind": "lambda", "event_source_mapping": "u-a", "memory_mib": 3538},
        "b": {"auto_scaling_group": "g"},
    }
    assert lambda_pools.mappings(pools) == {"a": "u-a"}
    assert lambda_pools.per_task(pools["a"]) == (2.0, 3538 / 1024)
    assert lambda_pools.describe(pools["a"]) == "lambda 3538 MB x 10"
