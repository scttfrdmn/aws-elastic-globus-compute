import json
import os
import uuid

import boto3
import pytest
from moto import mock_aws

from aegc import payloads, protocol
from aegc.protocol import (
    DEFAULT_INLINE_LIMIT,
    MAX_MESSAGE_BYTES,
    ErrorInfo,
    Payload,
    ProtocolError,
    ResultMessage,
    TaskMessage,
)

TID = str(uuid.UUID(int=1))
EID = str(uuid.UUID(int=2))


def task(**kw: object) -> TaskMessage:
    base: dict[str, object] = dict(
        task_uuid=TID,
        attempt=1,
        endpoint_id=EID,
        deployment="dev",
        pool="sci-py314",
        reply_to="https://sqs.us-west-2.amazonaws.com/1/aegc-dev-res-x",
        submitted_at=1.5,
        task=Payload.inline(b"packed"),
    )
    base.update(kw)
    return TaskMessage(**base)  # type: ignore[arg-type]


def test_task_round_trip() -> None:
    m = task(resource_spec={"env": "sci"}, walltime_s=60)
    back = TaskMessage.decode(m.encode())
    assert back == m
    assert back.executor_task_id == f"{TID}:1"
    assert back.task.inline_bytes() == b"packed"


def test_task_rejects_disallowed_call() -> None:
    body = json.loads(task().encode())
    body["call"]["f"] = "os.system"
    with pytest.raises(ProtocolError, match="not allowed"):
        TaskMessage.decode(json.dumps(body))


@pytest.mark.parametrize("walltime", [0, protocol.MAX_WALLTIME_S + 1])
def test_task_walltime_bounds(walltime: int) -> None:
    with pytest.raises(ProtocolError, match="walltime"):
        task(walltime_s=walltime).encode()


def test_wrong_version_and_kind() -> None:
    body = json.loads(task().encode())
    with pytest.raises(ProtocolError, match="version"):
        TaskMessage.decode(json.dumps({**body, "v": 2}))
    with pytest.raises(ProtocolError, match="expected kind"):
        TaskMessage.decode(json.dumps({**body, "kind": "result"}))
    with pytest.raises(ProtocolError, match="JSON"):
        TaskMessage.decode("{")


@pytest.mark.parametrize("kind", ["started", "result", "system_failure"])
def test_result_round_trip(kind: str) -> None:
    m = ResultMessage(
        kind=kind,  # type: ignore[arg-type]
        task_uuid=TID,
        attempt=2,
        endpoint_id=EID,
        result=Payload.inline(b"res") if kind == "result" else None,
        error=ErrorInfo("CHILD_CRASHED", "signal 9") if kind == "system_failure" else None,
        worker={"instance_id": "i-1"},
        timings={"exec_s": 1.25},
        receive_count=3,
    )
    assert ResultMessage.decode(m.encode()) == m


def test_result_validation() -> None:
    with pytest.raises(ProtocolError, match="needs a result"):
        ResultMessage(kind="result", task_uuid=TID, attempt=1, endpoint_id=EID).encode()
    with pytest.raises(ProtocolError, match="unknown error code"):
        ResultMessage(
            kind="system_failure",
            task_uuid=TID,
            attempt=1,
            endpoint_id=EID,
            error=ErrorInfo("NOPE", "x"),
        ).encode()


def test_executor_task_id_parsing() -> None:
    assert protocol.split_executor_task_id(f"{TID}:7") == (TID, 7)
    for bad in ["", TID, f"{TID}:0", f"{TID}:x", ":1"]:
        with pytest.raises(ProtocolError):
            protocol.split_executor_task_id(bad)


def test_payload_needs_exactly_one_form() -> None:
    with pytest.raises(ProtocolError):
        Payload()
    with pytest.raises(ProtocolError):
        Payload(inline_b64="", s3=protocol.S3Ref("b", "k", 0, ""))


def test_inline_limit_fits_in_one_sqs_message() -> None:
    """A body right at the inline limit, plus the envelope, stays under SQS's 1 MiB."""
    m = task(task=Payload.inline(os.urandom(DEFAULT_INLINE_LIMIT)))
    assert len(m.encode().encode()) < MAX_MESSAGE_BYTES


def test_oversized_inline_message_is_refused() -> None:
    with pytest.raises(ProtocolError, match="SQS limit"):
        task(task=Payload.inline(os.urandom(800_000))).encode()


def test_payload_keys() -> None:
    assert protocol.task_payload_key("dev", TID, 2) == f"payloads/dev/tasks/{TID}/2.task"
    assert (
        protocol.result_payload_key("dev", TID, 2) == f"payloads/dev/results/{TID}/{TID}:2.result"
    )


@mock_aws
def test_make_and_read_payload_inline_and_s3() -> None:
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(
        Bucket="aegc-test", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )

    small = payloads.make_payload(b"x" * 10, s3=s3, bucket="aegc-test", key="k1")
    assert small.inline_b64 is not None
    assert payloads.read_payload(small, s3=None) == b"x" * 10

    big_data = os.urandom(DEFAULT_INLINE_LIMIT + 1)
    big = payloads.make_payload(big_data, s3=s3, bucket="aegc-test", key="k2")
    assert big.s3 is not None and big.s3.size == len(big_data)
    assert payloads.read_payload(big, s3=s3) == big_data

    # Round-trips through a message too.
    assert TaskMessage.decode(task(task=big).encode()).task == big

    payloads.delete_payload(big, s3=s3)
    with pytest.raises(payloads.PayloadMissing):
        payloads.read_payload(big, s3=s3)


@mock_aws
def test_read_payload_detects_tampering() -> None:
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(
        Bucket="aegc-test", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )
    p = payloads.make_payload(
        os.urandom(DEFAULT_INLINE_LIMIT + 1), s3=s3, bucket="aegc-test", key="k"
    )
    s3.put_object(Bucket="aegc-test", Key="k", Body=b"tampered")
    with pytest.raises(ProtocolError, match="sha256"):
        payloads.read_payload(p, s3=s3)
