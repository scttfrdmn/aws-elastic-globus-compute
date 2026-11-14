"""AWSEngine against substrate, driven the way the endpoint's interchange drives it."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from globus_compute_endpoint.engines.base import GCFuture

from aegc import naming, protocol
from aegc.engine import AWSEngine
from aegc.infra import deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from aegc.protocol import ErrorInfo, Payload, ResultMessage
from aegc.worker.agent import Agent
from aegc.worker.config import WorkerConfig
from aegc.worker.testing import pack_task, unpack_result
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import FakeAutoScaling, sqs_for_worker, sqs_with_counts

BUCKET = naming.bucket_name(ACCOUNT, REGION)
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/aegc/geospatial@sha256:" + "d" * 64
EID = uuid.UUID(int=99)


def triple(x: int) -> int:
    return 3 * x


class Env:
    def __init__(self, aws: Callable[[str], Any], tmp: Path, *, python: str = "3.14") -> None:
        self.aws = aws
        self.tmp = tmp
        self.asg = FakeAutoScaling()
        self.sqs = aws("sqs")
        self.s3 = aws("s3")
        self.ssm = aws("ssm")
        self.s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        ec2 = aws("ec2")
        vpc = ec2.create_vpc(CidrBlock="172.31.0.0/16")["Vpc"]["VpcId"]
        subnet = ec2.create_subnet(
            VpcId=vpc, CidrBlock="172.31.0.0/20", AvailabilityZone=f"{REGION}a"
        )["Subnet"]["SubnetId"]
        pool = PoolSpec(
            name="geo-py314",
            env="geospatial",
            python_minor=python,
            image=IMAGE,
            max_workers=5,
            slots="2",
        )
        self.spec = DeploymentSpec(
            deployment="dev",
            account=ACCOUNT,
            region=REGION,
            bucket=BUCKET,
            pools=(pool,),
            subnet_ids=(subnet,),
        )
        deployment.deploy(
            Clients(
                iam=aws("iam"),
                ec2=ec2,
                autoscaling=self.asg,
                sqs=self.sqs,
                logs=aws("logs"),
                ssm=self.ssm,
            ),
            self.spec,
        )
        self.task_queue = self.sqs.get_queue_url(QueueName="aegc-dev-geo-py314-tasks")["QueueUrl"]

    def engine(self, *, python_minor: str = "3.14", sqs: Any = None, **kw: Any) -> AWSEngine:
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
            "sqs": sqs or self.sqs,
            "s3": self.s3,
            "ssm": self.ssm,
            "autoscaling": self.asg,
        }
        e.start(endpoint_id=EID, run_dir=str(self.tmp / "ep"))
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
        return Agent(
            config=cfg, sqs=sqs_for_worker(self.sqs), s3=self.s3, autoscaling=self.asg, wait_s=1
        )


@pytest.fixture
def env(aws: Callable[[str], Any], tmp_path: Path) -> Env:
    return Env(aws, tmp_path)


def submit(
    e: AWSEngine, fn: Callable[..., Any], *args: Any, spec: dict[str, Any] | None = None
) -> GCFuture:
    tid = uuid.uuid4()
    f = GCFuture(tid)
    e.submit(f, pack_task(fn, *args, task_uuid=str(tid)), spec or {})
    return f


def queued(env: Env) -> list[protocol.TaskMessage]:
    """Peek at the pool's task queue without consuming it."""
    r = env.sqs.receive_message(QueueUrl=env.task_queue, MaxNumberOfMessages=10, WaitTimeSeconds=0)
    msgs = r.get("Messages", [])
    for m in msgs:
        # substrate#1462 ignores a per-receive VisibilityTimeout, so release explicitly.
        env.sqs.change_message_visibility(
            QueueUrl=env.task_queue, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0
        )
    return [protocol.TaskMessage.decode(m["Body"]) for m in msgs]


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
        "autoscaling": FakeAutoScaling(),
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
    counts: dict[str, tuple[int, int]] = {}
    e = env.engine(sqs=sqs_with_counts(env.sqs, counts))  # substrate#1472
    try:
        counts[env.task_queue] = (7, 0)
        snap = e.scale_once()
        assert env.asg.groups["aegc-dev-geo-py314"]["DesiredCapacity"] == 4  # ceil(7 / 2 slots)
        assert snap.pools["geo-py314"].visible == 7
        counts[env.task_queue] = (0, 0)
        e.scale_once()
        assert env.asg.groups["aegc-dev-geo-py314"]["DesiredCapacity"] == 4  # never scales in
        counts[env.task_queue] = (50, 0)
        e.scale_once()
        assert env.asg.groups["aegc-dev-geo-py314"]["DesiredCapacity"] == 5  # capped at max
    finally:
        e.shutdown()


@pytest.mark.timeout(60)
def test_stall_breaker_pauses_scale_out(env: Env) -> None:
    counts = {env.task_queue: (3, 0)}
    e = env.engine(sqs=sqs_with_counts(env.sqs, counts))
    try:
        e.scale_once(now=1000.0)  # desired -> 2
        assert not e.scale_once(now=1010.0).pools["geo-py314"].stalled  # nothing in service yet
        snap = e.scale_once(now=1010.0 + 15 * 60)
        assert snap.pools["geo-py314"].stalled
        # Paused: even more demand doesn't scale out while stalled.
        counts[env.task_queue] = (9, 0)
        assert e.scale_once(now=1010.0 + 15 * 60 + 60).pools["geo-py314"].desired == 2
        assert e.get_status_report().global_state["pools"]["geo-py314"]["stalled"] is True
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
