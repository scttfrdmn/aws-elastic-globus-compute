"""aegc-worker against substrate SQS + S3, running real Globus Compute tasks."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aegc import naming, payloads, protocol
from aegc.infra import deployment
from aegc.protocol import Payload, ResultMessage, TaskMessage
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

    def join_group(self, *, min_size: int = 0) -> str:
        """Run this worker on a real instance of a real group; returns the instance id."""
        ec2 = self.aws("ec2")
        ami = self.aws("ssm").get_parameter(Name=deployment.AMI_PARAMETER)["Parameter"]["Value"]
        ec2.create_launch_template(
            LaunchTemplateName=GROUP,
            LaunchTemplateData={"ImageId": ami, "InstanceType": "m5.large"},
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
