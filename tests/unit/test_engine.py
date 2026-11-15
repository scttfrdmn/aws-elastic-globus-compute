"""AWSEngine against substrate, driven the way the endpoint's interchange drives it."""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from globus_compute_endpoint.engines.base import GCFuture

from aegc import ledger, naming, pricing, protocol, state
from aegc.engine import STALL_AFTER_S, STALL_PAUSE_S, AWSEngine
from aegc.infra import deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from aegc.protocol import ErrorInfo, Payload, ResultMessage
from aegc.worker.agent import Agent
from aegc.worker.config import WorkerConfig
from aegc.worker.testing import pack_task, unpack_result
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import launch_on_substrate

BUCKET = naming.bucket_name(ACCOUNT, REGION)
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/aegc/geospatial@sha256:" + "d" * 64
EID = uuid.UUID(int=99)
GROUP = "aegc-dev-geo-py314"


def triple(x: int) -> int:
    return 3 * x


class Env:
    def __init__(
        self, aws: Callable[[str], Any], tmp: Path, substrate_url: str, *, python: str = "3.14"
    ) -> None:
        self.aws = aws
        self.tmp = tmp
        self.substrate_url = substrate_url
        self.asg = aws("autoscaling")
        self.sqs = aws("sqs")
        self.s3 = aws("s3")
        self.ssm = aws("ssm")
        self.s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        pool = PoolSpec(
            name="geo-py314",
            env="geospatial",
            python_minor=python,
            image=IMAGE,
            max_vcpus=10,
            slots="2",
        )
        self.spec = DeploymentSpec(
            deployment="dev",
            account=ACCOUNT,
            region=REGION,
            bucket=BUCKET,
            pools=(pool,),  # in the default VPC
        )
        deployment.deploy(
            Clients(
                iam=aws("iam"),
                ec2=aws("ec2"),
                autoscaling=self.asg,
                sqs=self.sqs,
                logs=aws("logs"),
                ssm=self.ssm,
            ),
            self.spec,
        )
        self.task_queue = self.sqs.get_queue_url(QueueName="aegc-dev-geo-py314-tasks")["QueueUrl"]
        launch_on_substrate(self.asg, GROUP)  # substrate#1497

    def engine(self, *, python_minor: str = "3.14", **kw: Any) -> AWSEngine:
        e = AWSEngine(
            deployment="dev",
            region=REGION,
            env="geospatial",
            python_minor=python_minor,
            result_wait_s=1,
            scale_interval_s=3600,
            endpoint_id=EID,
            **kw,
        )
        e._clients = {
            "sqs": self.sqs,
            "s3": self.s3,
            "ssm": self.ssm,
            "autoscaling": self.asg,
            "ec2": self.aws("ec2"),
            "pricing": self.aws("pricing"),
        }
        e.start(endpoint_id=EID, run_dir=str(self.tmp / "ep"))
        # Tests drive scale_once() with a fake clock; let the scaler's real-time first pass
        # finish first, or it can race them (it once set _stalled_since to wall-clock time).
        assert e.first_scale_pass.wait(10)
        return e

    def worker(self) -> Agent:
        cfg = WorkerConfig(
            deployment="dev",
            pool="geo-py314",
            region=REGION,
            task_queue_url=self.task_queue,
            bucket=BUCKET,
            slots="2",
            scratch=str(self.tmp / "scratch"),
            instance_id="i-abc",
        )
        return Agent(config=cfg, sqs=self.sqs, s3=self.s3, autoscaling=self.asg, wait_s=1)

    def demand(self, n: int) -> None:
        """Queue n tasks no worker has claimed yet."""
        for i in range(0, n, 10):
            self.sqs.send_message_batch(
                QueueUrl=self.task_queue,
                Entries=[{"Id": str(j), "MessageBody": "{}"} for j in range(min(10, n - i))],
            )

    def drain(self) -> None:
        """Take every queued task off the queue (workers finished them)."""
        while msgs := self.sqs.receive_message(
            QueueUrl=self.task_queue, MaxNumberOfMessages=10, WaitTimeSeconds=0
        ).get("Messages"):
            self.sqs.delete_message_batch(
                QueueUrl=self.task_queue,
                Entries=[
                    {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(msgs)
                ],
            )

    def group(self) -> dict[str, Any]:
        return dict(
            self.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[GROUP])[
                "AutoScalingGroups"
            ][0]
        )

    def seed_launch_failure(self, message: str) -> None:
        """Make the group's launches fail, as with no Spot capacity (substrate control plane)."""
        req = urllib.request.Request(
            f"{self.substrate_url}/v1/autoscaling/launch-failure",
            data=json.dumps({"autoScalingGroupName": GROUP, "statusMessage": message}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass

    def clear_launch_failure(self) -> None:
        req = urllib.request.Request(
            f"{self.substrate_url}/v1/autoscaling/launch-failure?autoScalingGroupName={GROUP}",
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass


@pytest.fixture
def env(aws: Callable[[str], Any], tmp_path: Path, substrate: Any) -> Env:
    return Env(aws, tmp_path, substrate.url)


def submit(
    e: AWSEngine, fn: Callable[..., Any], *args: Any, spec: dict[str, Any] | None = None
) -> GCFuture:
    tid = uuid.uuid4()
    f = GCFuture(tid)
    e.submit(f, pack_task(fn, *args, task_uuid=str(tid)), spec or {})
    return f


def queued(env: Env) -> list[protocol.TaskMessage]:
    """Peek at the pool's task queue without consuming it."""
    r = env.sqs.receive_message(
        QueueUrl=env.task_queue, MaxNumberOfMessages=10, WaitTimeSeconds=0, VisibilityTimeout=0
    )
    return [protocol.TaskMessage.decode(m["Body"]) for m in r.get("Messages", [])]


@pytest.mark.timeout(120)
def test_end_to_end_engine_and_worker(env: Env) -> None:
    e = env.engine()
    try:
        f = submit(e, triple, 14)
        # Durable before submit returned: the task is already on the pool's queue.
        (msg,) = queued(env)
        assert msg.pool == "geo-py314" and msg.reply_to.endswith(
            naming.result_queue("dev", str(EID))
        )
        w = env.worker()
        w.claim_once()
        w.wait_idle()
        assert unpack_result(f.result(timeout=30)) == (False, 42)
        assert f.job_id == "i-abc"  # placement reported from the worker's "started"
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_missing_deployment_fails_tasks_not_the_endpoint(
    aws: Callable[[str], Any], tmp_path: Path
) -> None:
    e = AWSEngine(deployment="nope", region=REGION, result_wait_s=1, endpoint_id=EID)
    e._clients = {
        "sqs": aws("sqs"),
        "s3": aws("s3"),
        "ssm": aws("ssm"),
        "autoscaling": aws("autoscaling"),
    }
    e.start(endpoint_id=EID, run_dir=str(tmp_path))  # must not raise
    try:
        f = submit(e, triple, 1)
        is_err, text = unpack_result(f.result(timeout=5))
        assert is_err and "aegc deploy nope" in text
        assert "not found" in e.get_status_report().global_state["config_error"]
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("python", "spec", "expect"),
    [
        ("3.13", {}, "no worker image for Python 3.13"),
        ("3.14", {"vcpus": 8}, "unsupported resource_specification keys ['vcpus']"),
        ("3.14", {"env": "climate"}, "no pool for env 'climate'"),
        ("3.14", {"pool": "nope"}, "no pool 'nope'"),
        ("3.14", {"walltime_s": 99_999}, "walltime_s must be between"),
    ],
)
def test_bad_requests_fail_immediately(
    env: Env, python: str, spec: dict[str, Any], expect: str
) -> None:
    e = env.engine(python_minor=python)
    try:
        f = submit(e, triple, 1, spec=spec)
        is_err, text = unpack_result(f.result(timeout=5))
        assert is_err and expect in text
        assert queued(env) == []
    finally:
        e.shutdown()


def _post(env: Env, e: AWSEngine, msg: ResultMessage) -> None:
    env.sqs.send_message(QueueUrl=e._result_queue_url, MessageBody=msg.encode())


@pytest.mark.timeout(60)
def test_system_failure_retries_then_succeeds(env: Env) -> None:
    e = env.engine()
    try:
        f = submit(e, triple, 5)
        (first,) = queued(env)
        _post(
            env,
            e,
            ResultMessage(
                kind="system_failure",
                task_uuid=first.task_uuid,
                attempt=1,
                endpoint_id=str(EID),
                error=ErrorInfo("CHILD_CRASHED", "signal 9"),
            ),
        )
        deadline = time.monotonic() + 15
        attempts: list[int] = []
        while time.monotonic() < deadline and 2 not in attempts:
            attempts = [m.attempt for m in queued(env)]
            time.sleep(0.2)
        assert 2 in attempts, "the base class resubmits after a system failure"
        _post(
            env,
            e,
            ResultMessage(
                kind="result",
                task_uuid=first.task_uuid,
                attempt=2,
                endpoint_id=str(EID),
                result=Payload.inline(b"packed-ok"),
            ),
        )
        assert f.result(timeout=15) == b"packed-ok"
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_first_result_wins_and_duplicates_are_dropped(env: Env) -> None:
    e = env.engine()
    try:
        f = submit(e, triple, 1)
        tid = str(f.gc_task_id)
        for data in (b"first", b"second"):
            _post(
                env,
                e,
                ResultMessage(
                    kind="result",
                    task_uuid=tid,
                    attempt=1,
                    endpoint_id=str(EID),
                    result=Payload.inline(data),
                ),
            )
        assert f.result(timeout=15) == b"first"
        time.sleep(2.5)
        assert not (env.tmp / "ep" / "unacked_results" / tid).exists()
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_orphan_result_is_recovered_for_the_interchange(env: Env) -> None:
    e = env.engine()
    try:
        orphan = str(uuid.uuid4())  # submitted by an earlier endpoint process
        _post(
            env,
            e,
            ResultMessage(
                kind="result",
                task_uuid=orphan,
                attempt=1,
                endpoint_id=str(EID),
                result=Payload.inline(b"from-before"),
            ),
        )
        path = env.tmp / "ep" / "unacked_results" / orphan
        deadline = time.monotonic() + 15
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert path.read_bytes() == b"from-before"
        assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_scaler_scales_out_only(env: Env) -> None:
    e = env.engine()
    try:
        env.demand(7)
        snap = e.scale_once()
        assert snap.pools["geo-py314"].visible == 7
        g = env.group()
        # vCPUs: 7 tasks at 2 slots per 2-vCPU worker = 4 workers = 8 vCPUs. (substrate launches
        # one instance per unit, not per 2 vCPUs: substrate#1502.)
        assert g["DesiredCapacity"] == 8
        assert g["Instances"]  # Auto Scaling launched workers
        env.drain()
        assert e.scale_once().pools["geo-py314"].visible == 0
        assert env.group()["DesiredCapacity"] == 8  # never scales in; workers leave themselves
        env.demand(50)
        snap = e.scale_once()
        assert snap.pools["geo-py314"].visible == 50
        assert snap.pools["geo-py314"].desired == env.group()["DesiredCapacity"] == 10  # max
        assert snap.pools["geo-py314"].last_scaling_error == ""
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_scaler_counts_claimed_tasks(env: Env) -> None:
    e = env.engine()
    try:
        env.demand(3)
        env.sqs.receive_message(QueueUrl=env.task_queue, MaxNumberOfMessages=3, WaitTimeSeconds=0)
        m = e.scale_once().pools["geo-py314"]
        assert (m.visible, m.inflight) == (0, 3)
        assert env.group()["DesiredCapacity"] == 4  # ceil(3 / 2) workers x 2 vCPUs
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_stall_breaker_pauses_scale_out(env: Env) -> None:
    no_capacity = "There is no Spot capacity available that matches your request."
    env.seed_launch_failure(no_capacity)
    e = env.engine()
    try:
        env.demand(3)
        t0 = 1000.0
        snap = e.scale_once(now=t0)
        assert snap.pools["geo-py314"].desired == env.group()["DesiredCapacity"] == 4
        assert env.group()["Instances"] == []  # every launch failed
        activities = env.asg.describe_scaling_activities(AutoScalingGroupName=GROUP)["Activities"]
        assert activities and {a["StatusCode"] for a in activities} == {"Failed"}
        assert no_capacity in activities[0]["StatusMessage"]

        assert not e.scale_once(now=t0 + 10).pools["geo-py314"].stalled  # not for long yet
        snap = e.scale_once(now=t0 + 10 + STALL_AFTER_S)
        assert snap.pools["geo-py314"].stalled and snap.pools["geo-py314"].in_service == 0
        # Paused: even more demand doesn't scale out while stalled.
        env.demand(6)
        assert e.scale_once(now=t0 + 10 + STALL_AFTER_S + 60).pools["geo-py314"].desired == 4
        assert env.group()["DesiredCapacity"] == 4
        assert e.get_status_report().global_state["pools"]["geo-py314"]["stalled"] is True

        # Capacity comes back: the group launches, and after the pause the scaler resumes.
        env.clear_launch_failure()
        snap = e.scale_once(now=t0 + 10 + STALL_AFTER_S + STALL_PAUSE_S + 1)
        assert not snap.pools["geo-py314"].stalled and snap.pools["geo-py314"].in_service > 0
        g = env.group()
        assert g["DesiredCapacity"] == 10  # ceil(9 / 2) workers x 2 vCPUs, capped at max
        assert {i["LifecycleState"] for i in g["Instances"]} == {"InService"}
    finally:
        e.shutdown()


@pytest.mark.timeout(30)
def test_status_report_never_raises_and_shutdown_is_quick(env: Env) -> None:
    e = env.engine()
    e._snapshot = None  # type: ignore[assignment]
    report = e.get_status_report().global_state
    assert report["engine_type"] == "AWSEngine" and "status_error" in report
    t0 = time.monotonic()
    e.shutdown()
    assert time.monotonic() - t0 < 3


def test_repr_and_ha() -> None:
    e = AWSEngine(deployment="dev", env="geospatial")
    assert "deployment='dev'" in repr(e)
    with pytest.raises(ValueError, match="high-assurance"):
        e.assert_ha_compliant()


# --- spend safety (v0.2) -------------------------------------------------------------------------

NOON = 1_800_000_000 - 1_800_000_000 % 86400 + 12 * 3600  # a fixed UTC noon


def code_of(f: GCFuture) -> str:
    from globus_compute_common import messagepack

    r = messagepack.unpack(f.result(timeout=5))
    return str(r.error_details.code) if r.error_details else ""


def set_limits(env: Env, e: AWSEngine, **limits: Any) -> None:
    m = deployment.read_manifest(env.ssm, "dev")
    m["limits"] = state.Limits(**limits).to_dict()
    m["generation"] = deployment.manifest_generation(m)
    env.ssm.put_parameter(
        Name="/aegc/dev/manifest", Value=json.dumps(m), Type="String", Overwrite=True
    )
    e._manifest_read = -1e18
    assert e.refresh_manifest()
    assert e._spend is not None
    e._spend.prices = pricing.Prices(region=REGION)  # static prices: deterministic


def spend_record(env: Env, usd_hours: float, *, now: float = NOON) -> None:
    """A finished c8g.large on-demand worker in today's ledger, lasting ``usd_hours`` hours."""
    ledger.write(
        env.s3,
        BUCKET,
        ledger.Record(
            instance_id=f"i-{uuid.uuid4().hex[:8]}", deployment="dev", pool="geo-py314",
            instance_type="c8g.large", availability_zone="us-west-2a", lifecycle="on-demand",
            vcpus=2, launched_at=now - usd_hours * 3600, last_seen=now - 60, exit_reason="idle",
        ),
    )  # fmt: skip


@pytest.mark.timeout(60)
def test_deploy_creates_running_state(env: Env) -> None:
    assert state.read_state(env.ssm, "dev").running
    # A redeploy doesn't reset a non-running state.
    state.write_state(env.ssm, "dev", state.State(mode="stopped", by="test"))
    deployment.ensure_state(
        Clients(iam=None, ec2=None, autoscaling=None, sqs=None, logs=None, ssm=env.ssm),
        env.spec, deployment.Report(), dry_run=False,
    )  # fmt: skip
    assert state.read_state(env.ssm, "dev").mode == "stopped"


@pytest.mark.timeout(60)
def test_stopped_refuses_new_tasks(env: Env) -> None:
    state.write_state(env.ssm, "dev", state.State(mode="stopped", reason="aegc stop"))
    e = env.engine()
    try:
        f = submit(e, triple, 1)
        assert code_of(f) == "AEGC_STOPPED"
        assert "aegc resume dev" in unpack_result(f.result())[1]
        assert queued(env) == []
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_halt_fails_own_queued_tasks_and_purges_only_them(env: Env) -> None:
    e = env.engine()
    try:
        mine = [submit(e, triple, i) for i in range(3)]
        env.sqs.send_message(QueueUrl=env.task_queue, MessageBody="not ours")
        state.write_state(env.ssm, "dev", state.State(mode="expired", reason="lease"))
        e.refresh_state(force=True)
        assert {code_of(f) for f in mine} == {"AEGC_EXPIRED"}
        left = env.sqs.receive_message(QueueUrl=env.task_queue, MaxNumberOfMessages=10)
        assert [m["Body"] for m in left.get("Messages", [])] == ["not ours"]
        # Back to running: new tasks are accepted again.
        state.write_state(env.ssm, "dev", state.State())
        e.refresh_state(force=True)
        submit(e, triple, 9)
        assert len(queued(env)) == 1
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_cap_trips_and_fails_queued_tasks(env: Env) -> None:
    e = env.engine()
    try:
        set_limits(env, e, max_usd_day=0.05)
        spend_record(env, 1.0)  # about $0.088 so far today
        f = submit(e, triple, 3)
        snap = e.scale_once(now=NOON)
        st = state.read_state(env.ssm, "dev", now=NOON)
        assert st.mode == "capped" and st.until == state.period_end("day", NOON)
        assert snap.state == "capped" and env.group()["DesiredCapacity"] == 0
        assert code_of(f) == "AEGC_BUDGET_EXCEEDED"
        assert code_of(submit(e, triple, 4)) == "AEGC_BUDGET_EXCEEDED"
        # The next day the cap resets.
        assert state.read_state(env.ssm, "dev", now=NOON + 86400).running
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_admission_scales_out_only_what_the_cap_allows(env: Env) -> None:
    e = env.engine()
    try:
        # Static prices: one more 2-vCPU worker is about $0.137/h (priciest allowed family, plus
        # volume and IPv4), $0.0686 per vCPU. $0.40/day with nothing spent: 0.40/1.2/0.0686 = 4.9
        # vCPUs, rounded down to whole workers: 4 vCPUs (2 workers) of the 8 wanted.
        set_limits(env, e, max_usd_day=0.40)
        env.demand(8)  # 8 tasks / 2 slots = 4 workers = 8 vCPUs wanted
        m = e.scale_once(now=NOON).pools["geo-py314"]
        assert m.admitted == 4 and env.group()["DesiredCapacity"] == 4
        assert state.read_state(env.ssm, "dev", now=NOON).running
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_no_capacity_fails_queued_tasks(env: Env) -> None:
    env.seed_launch_failure("There is no Spot capacity available that matches your request.")
    e = env.engine(stall_fail_s=600)
    try:
        f = submit(e, triple, 2)
        e.scale_once(now=NOON)  # scales out to 1; no worker will come
        e.scale_once(now=NOON + 1)  # starved from here (or earlier, if the real-time scaler ran)
        e.scale_once(now=NOON + 602)
        assert code_of(f) == "AEGC_NO_CAPACITY"
        assert "L-34B43A08" in unpack_result(f.result())[1]
        assert queued(env) == []
    finally:
        env.clear_launch_failure()
        e.shutdown()


@pytest.mark.timeout(60)
def test_pool_walltime_default_and_ceiling(env: Env) -> None:
    e = env.engine()
    try:
        assert e._manifest is not None
        pool = e._manifest["pools"]["geo-py314"]
        pool["walltime_s"], pool["max_walltime_s"] = 600, 1200
        submit(e, triple, 1)
        (msg,) = queued(env)
        assert msg.walltime_s == 600
        f = submit(e, triple, 1, spec={"walltime_s": 5000})
        assert code_of(f) == "AEGC_CONFIGURATION"
        assert "between 1 and 1200" in unpack_result(f.result())[1]
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_running_engine_picks_up_a_redeploy(env: Env) -> None:
    e = env.engine()
    try:
        assert not e.refresh_manifest(now=time.time() + 120)  # nothing changed
        m = deployment.read_manifest(env.ssm, "dev")
        m["pools"]["geo-py314"]["max_vcpus"] = 18
        m["generation"] = deployment.manifest_generation(m)
        env.ssm.put_parameter(
            Name="/aegc/dev/manifest", Value=json.dumps(m), Type="String", Overwrite=True
        )
        assert e.refresh_manifest(now=time.time() + 240)
        assert e.scale_once().pools["geo-py314"].max == 18
    finally:
        e.shutdown()


@pytest.mark.xfail(strict=True, reason="substrate#1502: DesiredCapacityType=vcpu isn't modelled")
@pytest.mark.timeout(60)
def test_vcpu_capacity_launches_workers_not_units(env: Env) -> None:
    e = env.engine()
    try:
        env.demand(7)
        e.scale_once()
        g = env.group()
        assert g["DesiredCapacity"] == 8  # vCPUs
        assert len(g["Instances"]) == 4  # 2-vCPU workers (m5.large on substrate#1497's shim)
    finally:
        e.shutdown()
