"""aegc-worker against substrate SQS + S3, running real Globus Compute tasks."""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aegc import ledger, naming, payloads, protocol, state
from aegc.infra import deployment
from aegc.protocol import Payload, ResultMessage, TaskMessage
from aegc.worker import emf, jsonlog
from aegc.worker.agent import Agent
from aegc.worker.config import WorkerConfig
from aegc.worker.testing import pack_task, unpack_result
from tests.conftest import REGION

BUCKET = "aegc-123456789012-us-west-2"
EID = str(uuid.UUID(int=7))


def double(x: int) -> int:
    return x * 2


def boom() -> None:
    raise ValueError("boom from task")


def die() -> None:
    os._exit(3)


def sleeper(s: float) -> str:
    import time

    time.sleep(s)
    return "woke"


def big() -> bytes:
    return b"x" * 900_000


GROUP = "aegc-dev-p"


class Rig:
    def __init__(self, aws: Callable[[str], Any], tmp: Path) -> None:
        self.aws = aws
        self.sqs = aws("sqs")
        self.s3 = aws("s3")
        self.s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        self.tasks = self.sqs.create_queue(
            QueueName="aegc-dev-p-tasks", Attributes={"VisibilityTimeout": "300"}
        )["QueueUrl"]
        self.results = self.sqs.create_queue(QueueName="aegc-dev-res-x")["QueueUrl"]
        self.asg = aws("autoscaling")
        self.cfg = WorkerConfig(
            deployment="dev",
            pool="p",
            region=REGION,
            task_queue_url=self.tasks,
            bucket=BUCKET,
            slots="2",
            idle_timeout_s=3600,
            scratch=str(tmp),
            instance_id="i-123",
        )
        self.emf_out = io.StringIO()

    def metrics(self) -> list[dict[str, Any]]:
        """The EMF documents the agents wrote."""
        return [json.loads(line) for line in self.emf_out.getvalue().splitlines()]

    def metric_values(self, name: str, outcome: str | None = None) -> list[float]:
        return [
            float(d[name])
            for d in self.metrics()
            if name in d and (outcome is None or d.get("Outcome") == outcome)
        ]

    def join_group(self, *, min_size: int = 0) -> str:
        """Run this worker on a real instance of a real group; returns the instance id."""
        ec2 = self.aws("ec2")
        ami = self.aws("ssm").get_parameter(Name=deployment.AMI_PARAMETER)["Parameter"]["Value"]
        ec2.create_launch_template(
            LaunchTemplateName=GROUP,
            LaunchTemplateData={"ImageId": ami, "InstanceType": "c7g.large"},  # arm64 AMI,
        )
        subnets = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])[
            "Subnets"
        ]
        self.asg.create_auto_scaling_group(
            AutoScalingGroupName=GROUP,
            MinSize=min_size,
            MaxSize=2,
            DesiredCapacity=min_size,
            VPCZoneIdentifier=",".join(s["SubnetId"] for s in subnets),
            LaunchTemplate={"LaunchTemplateName": GROUP, "Version": "$Default"},
            NewInstancesProtectedFromScaleIn=True,
            Tags=[
                {"Key": k, "Value": v, "PropagateAtLaunch": True}
                for k, v in naming.tags("dev", "asg", pool="p").items()
            ],
        )
        self.asg.set_desired_capacity(AutoScalingGroupName=GROUP, DesiredCapacity=1)
        (inst,) = self.group()["Instances"]
        assert inst["LifecycleState"] == "InService"
        self.cfg = WorkerConfig(**{**self.cfg.__dict__, "instance_id": inst["InstanceId"]})
        return str(inst["InstanceId"])

    def group(self) -> dict[str, Any]:
        return dict(
            self.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[GROUP])[
                "AutoScalingGroups"
            ][0]
        )

    def agent(self, **kw: Any) -> Agent:
        kw.setdefault("metrics", emf.Emitter("dev", "p", stream=self.emf_out))
        return Agent(
            config=self.cfg, sqs=self.sqs, s3=self.s3, autoscaling=self.asg, wait_s=1, **kw
        )

    def submit(self, fn: Callable[..., Any], *args: Any, walltime_s: int = 60) -> str:
        tid = str(uuid.uuid4())
        body = pack_task(fn, *args, task_uuid=tid)
        task = payloads.make_payload(
            body, s3=self.s3, bucket=BUCKET, key=protocol.task_payload_key("dev", tid, 1)
        )
        msg = TaskMessage(
            task_uuid=tid,
            attempt=1,
            endpoint_id=EID,
            deployment="dev",
            pool="p",
            reply_to=self.results,
            submitted_at=time.time(),
            task=task,
            walltime_s=walltime_s,
        )
        self.sqs.send_message(QueueUrl=self.tasks, MessageBody=msg.encode())
        return tid

    def replies(self, n: int, timeout_s: float = 30) -> list[ResultMessage]:
        out: list[ResultMessage] = []
        deadline = time.monotonic() + timeout_s
        while len(out) < n and time.monotonic() < deadline:
            r = self.sqs.receive_message(
                QueueUrl=self.results, MaxNumberOfMessages=10, WaitTimeSeconds=1
            )
            for m in r.get("Messages", []):
                out.append(ResultMessage.decode(m["Body"]))
                self.sqs.delete_message(QueueUrl=self.results, ReceiptHandle=m["ReceiptHandle"])
        return out

    def final(self, n: int) -> dict[str, ResultMessage]:
        """The non-'started' reply for each task, keyed by task uuid."""
        got = [m for m in self.replies(n * 2) if m.kind != "started"]
        return {m.task_uuid: m for m in got}

    def task_queue_empty(self) -> bool:
        r = self.sqs.receive_message(QueueUrl=self.tasks, WaitTimeSeconds=0, VisibilityTimeout=0)
        return not r.get("Messages")


@pytest.fixture
def rig(aws: Callable[[str], Any], tmp_path: Path) -> Rig:
    return Rig(aws, tmp_path)


def value_of(m: ResultMessage, s3: Any) -> tuple[bool, Any]:
    assert m.result is not None
    return unpack_result(payloads.read_payload(m.result, s3=s3))


@pytest.mark.timeout(120)
def test_runs_tasks_and_acks(rig: Rig) -> None:
    a = rig.agent()
    t1, t2 = rig.submit(double, 21), rig.submit(boom)
    while a.stats.claimed < 2:
        a.claim_once()
    a.wait_idle()
    res = rig.final(2)
    assert res[t1].kind == "result" and value_of(res[t1], rig.s3) == (False, 42)
    is_err, text = value_of(res[t2], rig.s3)
    assert is_err and "boom from task" in text
    assert res[t1].worker["instance_id"] == "i-123"
    assert rig.task_queue_empty()


@pytest.mark.timeout(120)
def test_crash_is_a_system_failure(rig: Rig) -> None:
    a = rig.agent()
    tid = rig.submit(die)
    a.claim_once()
    a.wait_idle()
    m = rig.final(1)[tid]
    assert m.kind == "system_failure" and m.error is not None
    assert m.error.code == "CHILD_CRASHED" and "status 3" in m.error.message
    assert rig.task_queue_empty()


@pytest.mark.timeout(120)
def test_walltime_kills_and_reports(rig: Rig) -> None:
    a = rig.agent()
    tid = rig.submit(sleeper, 60, walltime_s=1)
    a.claim_once()
    a.wait_idle(timeout_s=60)
    is_err, text = value_of(rig.final(1)[tid], rig.s3)
    assert is_err and "walltime" in text


@pytest.mark.timeout(120)
def test_large_result_goes_through_s3(rig: Rig) -> None:
    a = rig.agent()
    tid = rig.submit(big)
    a.claim_once()
    a.wait_idle()
    m = rig.final(1)[tid]
    assert m.result is not None and m.result.s3 is not None
    assert value_of(m, rig.s3) == (False, b"x" * 900_000)


@pytest.mark.timeout(60)
def test_poison_task_is_not_rerun(rig: Rig) -> None:
    a = rig.agent()
    tid = rig.submit(double, 1)
    # Workers that died holding it: each receive counts as a delivery.
    for n in range(1, rig.cfg.max_deliveries + 1):
        (m,) = rig.sqs.receive_message(
            QueueUrl=rig.tasks,
            VisibilityTimeout=0,
            WaitTimeSeconds=0,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )["Messages"]
        assert m["Attributes"]["ApproximateReceiveCount"] == str(n)
    assert a.claim_once() == 1
    m = rig.final(1)[tid]
    assert m.kind == "system_failure" and m.error is not None
    assert m.error.code == "WORKER_LOST_REPEATEDLY"
    assert a.stats.poison == 1 and rig.task_queue_empty()
    assert rig.metric_values("PoisonTasks") == [1.0]
    assert rig.metric_values("TasksFailed", "poison") == [1.0]


@pytest.mark.timeout(60)
def test_missing_payload(rig: Rig) -> None:
    a = rig.agent()
    tid = str(uuid.uuid4())
    msg = TaskMessage(
        task_uuid=tid,
        attempt=1,
        endpoint_id=EID,
        deployment="dev",
        pool="p",
        reply_to=rig.results,
        submitted_at=time.time(),
        task=Payload(s3=protocol.S3Ref(BUCKET, "payloads/dev/tasks/gone/1.task", 10, "0" * 64)),
    )
    rig.sqs.send_message(QueueUrl=rig.tasks, MessageBody=msg.encode())
    a.claim_once()
    m = rig.final(1)[tid]
    assert m.error is not None and m.error.code == "PAYLOAD_MISSING"


@pytest.mark.timeout(120)
def test_interruption_releases_inflight(rig: Rig) -> None:
    a = rig.agent()
    tid = rig.submit(sleeper, 30)
    a.claim_once()
    started = [m for m in rig.replies(1) if m.kind == "started"]
    assert started and started[0].task_uuid == tid
    a.handle_notice("interruption")
    # Released with visibility 0: another worker can claim it straight away.
    r = rig.sqs.receive_message(QueueUrl=rig.tasks, WaitTimeSeconds=1)
    assert r.get("Messages") and tid in r["Messages"][0]["Body"]
    assert a.stats.released == 1
    assert rig.metric_values("Interruptions") == [1.0]
    assert rig.metric_values("Releases") == [1.0]


def test_watchdog_heartbeat_needs_a_live_claim_loop(rig: Rig, tmp_path: Path) -> None:
    # The heartbeat thread outlives a wedged claim loop; it must stop vouching for it so the host
    # watchdog replaces the instance instead of paying for it indefinitely.
    a = rig.agent()
    hb = tmp_path / "hb"
    now = time.monotonic()
    a._loop_tick = now
    assert a._touch_heartbeat_file(hb, now) and hb.exists()
    hb.unlink()
    assert not a._touch_heartbeat_file(hb, now + a.loop_stale_s + 1)
    assert not hb.exists()


@pytest.mark.timeout(60)
def test_rebalance_keeps_working(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    # Auto Scaling's capacity rebalance replaces the instance; the worker keeps claiming until
    # it's terminated (SIGTERM) or interrupted. Draining would strand the instance in the group.
    a = rig.agent()
    with caplog.at_level(logging.INFO, logger="aegc"):
        a.handle_notice("rebalance")
        a.handle_notice("rebalance")
    assert sum("rebalance" in r.getMessage() for r in caplog.records) == 1
    tid = rig.submit(pow, 3, 2)
    a.claim_once()
    assert rig.final(1)[tid].error is None and a.stats.completed == 1


@pytest.mark.timeout(60)
def test_idle_drain_leaves_the_group(rig: Rig) -> None:
    me = rig.join_group()
    assert rig.group()["DesiredCapacity"] == 1
    clock = [0.0]
    a = rig.agent(clock=lambda: clock[0])
    a.config = WorkerConfig(**{**rig.cfg.__dict__, "idle_timeout_s": 10})
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(a.run()), daemon=True)
    clock[0] = 100.0
    t.start()
    t.join(30)
    assert not t.is_alive() and result == ["idle"]
    # Terminated through Auto Scaling, decrementing desired capacity: no replacement launches.
    g = rig.group()
    assert g["DesiredCapacity"] == 0
    assert me not in [i["InstanceId"] for i in g["Instances"]]
    (r,) = rig.aws("ec2").describe_instances(InstanceIds=[me])["Reservations"]
    assert r["Instances"][0]["State"]["Name"] in {"shutting-down", "terminated"}


@pytest.mark.timeout(60)
def test_idle_at_min_size_keeps_running(rig: Rig) -> None:
    # MinSize 1: Auto Scaling refuses a decrementing terminate with ValidationError.
    me = rig.join_group(min_size=1)
    clock = [0.0]
    a = rig.agent(clock=lambda: clock[0])
    a.config = WorkerConfig(**{**rig.cfg.__dict__, "idle_timeout_s": 10})
    clock[0] = 100.0
    reason = a.run(max_cycles=2)
    assert reason == "stopped"
    g = rig.group()
    assert g["DesiredCapacity"] == 1
    assert [(i["InstanceId"], i["LifecycleState"]) for i in g["Instances"]] == [(me, "InService")]


@pytest.mark.timeout(120)
def test_heartbeat_extends_visibility(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    a = rig.agent()
    rig.submit(sleeper, 5)
    a.claim_once()
    with caplog.at_level(logging.WARNING, logger="aegc"):
        assert a.extend_visibility() == 1
    assert not [r for r in caplog.records if "visibility" in r.getMessage()]
    attrs = rig.sqs.get_queue_attributes(
        QueueUrl=rig.tasks,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    assert (
        attrs["ApproximateNumberOfMessages"],
        attrs["ApproximateNumberOfMessagesNotVisible"],
    ) == (
        "0",
        "1",
    )
    a.wait_idle()


def test_config_from_env() -> None:
    import base64
    import json

    raw = base64.b64encode(
        json.dumps(
            {
                "deployment": "dev",
                "pool": "p",
                "region": "r",
                "task_queue_url": "q",
                "bucket": "b",
                "slots": "3",
                "future_key": 1,
            }
        ).encode()
    ).decode()
    cfg = WorkerConfig.from_env({"AEGC_WORKER_CONFIG_B64": raw, "AEGC_INSTANCE_ID": "i-9"})
    assert cfg.slot_count == 3 and cfg.instance_id == "i-9" and cfg.heartbeat_s == 100
    assert cfg.image == ""
    image = "r/aegc/geospatial@sha256:" + "a" * 64
    cfg = WorkerConfig.from_env({"AEGC_WORKER_CONFIG_B64": raw, "AEGC_IMAGE": image})
    assert cfg.image == image  # user data passes it (-e AEGC_IMAGE) for provenance


# --- observability (v0.4: #39, #40) --------------------------------------------------------------

IMAGE = "123456789012.dkr.ecr.us-west-2.amazonaws.com/aegc/geospatial@sha256:" + "c" * 64


@pytest.mark.timeout(120)
def test_provenance_metrics_and_the_task_finished_line(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    from globus_compute_common import messagepack

    rig.cfg = WorkerConfig(
        **{**rig.cfg.__dict__, "instance_type": "c8g.large", "availability_zone": "us-west-2a",
           "lifecycle": "spot", "image": IMAGE}
    )  # fmt: skip
    a = rig.agent()
    t1, t2 = rig.submit(double, 21), rig.submit(boom)
    with caplog.at_level(logging.INFO, logger="aegc"):
        while a.stats.claimed < 2:
            a.claim_once()
        a.wait_idle()
    res = rig.final(2)
    assert value_of(res[t1], rig.s3) == (False, 42)  # still the task's value

    # details["aegc"] is merged into what execute_task already put in details.
    assert res[t1].result is not None
    packed = payloads.read_payload(res[t1].result, s3=rig.s3)
    details = messagepack.unpack(packed).details
    assert {"os", "python_version", "globus_compute_sdk_version"} <= set(details)
    prov = details["aegc"]
    assert prov["image"] == IMAGE
    assert (prov["instance_type"], prov["availability_zone"], prov["lifecycle"]) == (
        "c8g.large", "us-west-2a", "spot",
    )  # fmt: skip
    assert prov["slots"] == 2 and prov["vcpus"] == os.cpu_count()
    assert (prov["attempt"], prov["receive_count"]) == (1, 1)
    t = prov["timings"]
    assert t["received_at"] <= t["started_at"] <= t["finished_at"]
    assert t["exec_s"] >= 0 and t["queue_wait_s"] >= 0
    assert prov["est_cost_usd"] > 0 and prov["est_cost_source"] == "static"
    assert res[t1].timings["queue_wait_s"] == pytest.approx(t["queue_wait_s"], abs=0.01)

    # One task_finished line per task, carrying the same record; JSON with the task fields.
    done = {r.task_uuid: r for r in caplog.records if getattr(r, "event", "") == "task_finished"}
    assert set(done) == {t1, t2}
    assert (done[t1].outcome, done[t2].outcome) == ("ok", "task_error")
    line = json.loads(jsonlog.JsonFormatter().format(done[t1]))
    assert line["msg"].startswith("task finished: ") and line["level"] == "INFO"
    assert line["etid"] == f"{t1}:1" and line["receive_count"] == 1 and line["posted"] is True
    assert line["provenance"]["image"] == IMAGE

    # Metrics: a user exception is a completed task (Outcome task_error), not a failed one.
    assert rig.metric_values("TasksCompleted", "ok") == [1.0]
    assert rig.metric_values("TasksCompleted", "task_error") == [1.0]
    assert not rig.metric_values("TasksFailed")
    assert len(rig.metric_values("ExecSeconds")) == 2
    assert len(rig.metric_values("QueueWaitSeconds")) == 2
    assert len(rig.metric_values("BootToFirstClaimSeconds")) == 1  # once per worker
    directives = [m for d in rig.metrics() for m in d["_aws"]["CloudWatchMetrics"]]
    dims = {n for m in directives for dim_set in m["Dimensions"] for n in dim_set}
    assert dims == {"Deployment", "Pool", "Outcome"}


# --- deployment state, lifetime, ledger (v0.2) ---------------------------------------------------


def result_code(m: ResultMessage, s3: Any) -> str:
    from globus_compute_common import messagepack

    assert m.kind == "result" and m.result is not None
    r = messagepack.unpack(payloads.read_payload(m.result, s3=s3))
    return str(r.error_details.code) if r.error_details else ""


def stateful(rig: Rig) -> tuple[Agent, Any]:
    ssm = rig.aws("ssm")
    rig.cfg = WorkerConfig(**{**rig.cfg.__dict__, "state_parameter": "/aegc/dev/state"})
    return rig.agent(ssm=ssm), ssm


@pytest.mark.timeout(60)
def test_running_state_claims_normally(rig: Rig) -> None:
    a, ssm = stateful(rig)
    state.write_state(ssm, "dev", state.State())
    assert a.check_state() is None
    tid = rig.submit(pow, 2, 5)
    a.claim_once()
    a.wait_idle()
    assert value_of(rig.final(1)[tid], rig.s3) == (False, 32)


@pytest.mark.timeout(60)
def test_stopped_fails_queued_tasks_clearly(rig: Rig) -> None:
    a, ssm = stateful(rig)
    tids = [rig.submit(pow, 2, i) for i in range(3)]
    state.write_state(ssm, "dev", state.State(mode="stopped", reason="aegc stop", by="test"))
    st = a.check_state()
    assert st is not None and st.mode == "stopped"
    n = 0
    while n < 3:
        got = a.fail_queued_once(st)
        assert got, "every queued task gets a refusal"
        n += got
    replies = rig.final(3)
    assert set(replies) == set(tids)
    # A final result (kind=result), not a system_failure: the engine won't retry it.
    assert {result_code(m, rig.s3) for m in replies.values()} == {"AEGC_STOPPED"}
    assert rig.task_queue_empty()


@pytest.mark.timeout(60)
def test_claim_racing_a_halt_is_refused(rig: Rig) -> None:
    a, ssm = stateful(rig)
    state.write_state(
        ssm, "dev", state.State(mode="capped", reason="$0.02/day", until=time.time() + 3600)
    )
    a.check_state()
    tid = rig.submit(pow, 2, 2)
    a.claim_once()  # an ordinary claim while halted still doesn't run the task
    assert result_code(rig.final(1)[tid], rig.s3) == "AEGC_BUDGET_EXCEEDED"


@pytest.mark.timeout(120)
def test_kill_fails_running_tasks(rig: Rig) -> None:
    a, ssm = stateful(rig)
    tid = rig.submit(sleeper, 30)
    a.claim_once()
    assert [m.kind for m in rig.replies(1)] == ["started"]
    state.write_state(ssm, "dev", state.State(mode="stopped", kill=True, by="aegc stop --hard"))
    a.check_state()
    assert result_code(rig.final(1)[tid], rig.s3) == "AEGC_STOPPED"
    assert not a._busy() and rig.task_queue_empty()


@pytest.mark.timeout(60)
def test_halted_worker_leaves_the_group(rig: Rig) -> None:
    me = rig.join_group()
    a, ssm = stateful(rig)
    state.write_state(ssm, "dev", state.State(mode="expired", reason="lease", by="lease"))
    a.check_state()
    assert a.run(max_cycles=3) == "expired"
    g = rig.group()
    assert g["DesiredCapacity"] == 0 and me not in [i["InstanceId"] for i in g["Instances"]]


@pytest.mark.timeout(60)
def test_lifetime_drains(rig: Rig) -> None:
    rig.cfg = WorkerConfig(**{**rig.cfg.__dict__, "max_lifetime_s": 0, "instance_id": ""})
    assert rig.agent().run(max_cycles=5) == "lifetime"
    (doc,) = [d for d in rig.metrics() if "WorkerExit" in d]
    assert doc["WorkerExit"] == 1 and doc["reason"] == "lifetime"


@pytest.mark.timeout(60)
def test_ledger_records_the_instance(rig: Rig) -> None:
    me = rig.join_group()
    rig.cfg = WorkerConfig(
        **{**rig.cfg.__dict__, "instance_type": "c8g.large", "availability_zone": "us-west-2a",
           "lifecycle": "spot", "max_lifetime_s": 0}
    )  # fmt: skip
    a = rig.agent()
    a.run(max_cycles=2)
    (rec,) = ledger.read(rig.s3, BUCKET, "dev", time.time() - 60, time.time() + 60)
    assert rec["instance_id"] == me and rec["instance_type"] == "c8g.large"
    assert rec["lifecycle"] == "spot" and rec["exit_reason"] == "lifetime"
    assert rec["last_seen"] >= rec["launched_at"]
