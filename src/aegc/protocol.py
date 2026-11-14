"""Protocol v1: the JSON messages exchanged between AWSEngine and aegc-worker over SQS.

Task messages go from the engine to a pool's task queue. Result messages go from a worker to the
``reply_to`` result queue of the endpoint process that submitted the task. Bodies larger than the
inline limit travel through S3 (see :mod:`aegc.payloads`); a message only carries a pointer.

Delivery is at least once. ``executor_task_id`` (``<task_uuid>:<attempt>``) identifies one attempt;
the engine accepts the first result per ``task_uuid``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1

#: SQS's maximum message size, including message attributes.
MAX_MESSAGE_BYTES = 1_048_576
#: Raw bytes above which a body goes to S3. Base64 grows data by 4/3, and the envelope needs room.
DEFAULT_INLINE_LIMIT = 700_000

#: The only callable a worker will run. Workers never import arbitrary dotted paths from messages.
EXECUTE_TASK = "globus_compute_endpoint.engines.helper.execute_task"
ALLOWED_CALLS = frozenset({EXECUTE_TASK})

#: SQS can't extend a claim past 12 hours, so v0.1 caps walltime below that.
DEFAULT_WALLTIME_S = 39_600
MAX_WALLTIME_S = 41_400

ResultKind = Literal["started", "result", "system_failure"]
RESULT_KINDS: frozenset[str] = frozenset({"started", "result", "system_failure"})

#: Codes for ``system_failure`` results. These trigger the engine's retry path.
ERROR_CODES = frozenset(
    {
        "WORKER_LOST_REPEATEDLY",
        "CHILD_CRASHED",
        "PAYLOAD_MISSING",
        "SPOT_INTERRUPTED",
        "PROTOCOL_ERROR",
    }
)


class ProtocolError(ValueError):
    """A message that can't be decoded or doesn't satisfy protocol v1."""


def executor_task_id(task_uuid: str, attempt: int) -> str:
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    return f"{task_uuid}:{attempt}"


def split_executor_task_id(etid: str) -> tuple[str, int]:
    task_uuid, sep, attempt = etid.rpartition(":")
    if not sep or not task_uuid or not attempt.isdigit() or int(attempt) < 1:
        raise ProtocolError(f"bad executor_task_id {etid!r}")
    return task_uuid, int(attempt)


@dataclass(frozen=True)
class S3Ref:
    bucket: str
    key: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Payload:
    """A body carried either inline (base64) or as a pointer to an S3 object."""

    inline_b64: str | None = None
    s3: S3Ref | None = None

    def __post_init__(self) -> None:
        if (self.inline_b64 is None) == (self.s3 is None):
            raise ProtocolError("payload needs exactly one of inline_b64 or s3")

    @classmethod
    def inline(cls, data: bytes) -> Payload:
        return cls(inline_b64=base64.b64encode(data).decode("ascii"))

    def inline_bytes(self) -> bytes:
        if self.inline_b64 is None:
            raise ProtocolError("payload is not inline")
        return base64.b64decode(self.inline_b64, validate=True)

    def to_json(self) -> dict[str, Any]:
        if self.s3 is not None:
            return {"s3": asdict(self.s3)}
        return {"inline_b64": self.inline_b64}

    @classmethod
    def from_json(cls, d: Any) -> Payload:
        if not isinstance(d, dict):
            raise ProtocolError("payload must be an object")
        if "s3" in d:
            s = d["s3"]
            try:
                return cls(
                    s3=S3Ref(
                        bucket=str(s["bucket"]),
                        key=str(s["key"]),
                        size=int(s["size"]),
                        sha256=str(s["sha256"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ProtocolError(f"bad s3 payload: {e}") from e
        if isinstance(d.get("inline_b64"), str):
            return cls(inline_b64=d["inline_b64"])
        raise ProtocolError("payload needs inline_b64 or s3")


@dataclass(frozen=True)
class Call:
    """How the worker invokes the task. Mirrors the kwargs GlobusComputeEngineBase passes."""

    f: str = EXECUTE_TASK
    run_in_sandbox: bool = False
    task_deserializers: list[str] | None = None
    result_serializers: list[str] | None = None


@dataclass(frozen=True)
class TaskMessage:
    task_uuid: str
    attempt: int
    endpoint_id: str
    deployment: str
    pool: str
    reply_to: str
    submitted_at: float
    task: Payload
    call: Call = field(default_factory=Call)
    resource_spec: dict[str, Any] = field(default_factory=dict)
    walltime_s: int = DEFAULT_WALLTIME_S
    engine_versions: dict[str, str] = field(default_factory=dict)

    @property
    def executor_task_id(self) -> str:
        return executor_task_id(self.task_uuid, self.attempt)

    def validate(self) -> None:
        if self.call.f not in ALLOWED_CALLS:
            raise ProtocolError(f"call {self.call.f!r} is not allowed")
        if not 1 <= self.walltime_s <= MAX_WALLTIME_S:
            raise ProtocolError(f"walltime_s must be 1..{MAX_WALLTIME_S}")
        executor_task_id(self.task_uuid, self.attempt)
        for name in ("task_uuid", "endpoint_id", "deployment", "pool", "reply_to"):
            if not getattr(self, name):
                raise ProtocolError(f"{name} is required")

    def encode(self) -> str:
        self.validate()
        d = asdict(self)
        d["task"] = self.task.to_json()
        return _dumps(
            {"v": PROTOCOL_VERSION, "kind": "task", "executor_task_id": self.executor_task_id, **d}
        )

    @classmethod
    def decode(cls, body: str) -> TaskMessage:
        d = _loads(body, expect_kind="task")
        try:
            msg = cls(
                task_uuid=d["task_uuid"],
                attempt=int(d["attempt"]),
                endpoint_id=d["endpoint_id"],
                deployment=d["deployment"],
                pool=d["pool"],
                reply_to=d["reply_to"],
                submitted_at=float(d["submitted_at"]),
                task=Payload.from_json(d["task"]),
                call=Call(**d.get("call", {})),
                resource_spec=dict(d.get("resource_spec") or {}),
                walltime_s=int(d.get("walltime_s", DEFAULT_WALLTIME_S)),
                engine_versions=dict(d.get("engine_versions") or {}),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"bad task message: {e}") from e
        msg.validate()
        return msg


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str


@dataclass(frozen=True)
class ResultMessage:
    kind: ResultKind
    task_uuid: str
    attempt: int
    endpoint_id: str
    result: Payload | None = None
    error: ErrorInfo | None = None
    worker: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    receive_count: int = 1

    @property
    def executor_task_id(self) -> str:
        return executor_task_id(self.task_uuid, self.attempt)

    def validate(self) -> None:
        if self.kind not in RESULT_KINDS:
            raise ProtocolError(f"unknown result kind {self.kind!r}")
        if self.kind == "result" and self.result is None:
            raise ProtocolError("a result message needs a result payload")
        if self.kind == "system_failure":
            if self.error is None:
                raise ProtocolError("a system_failure message needs an error")
            if self.error.code not in ERROR_CODES:
                raise ProtocolError(f"unknown error code {self.error.code!r}")
        executor_task_id(self.task_uuid, self.attempt)

    def encode(self) -> str:
        self.validate()
        d = asdict(self)
        d["result"] = self.result.to_json() if self.result is not None else None
        return _dumps({"v": PROTOCOL_VERSION, "executor_task_id": self.executor_task_id, **d})

    @classmethod
    def decode(cls, body: str) -> ResultMessage:
        d = _loads(body)
        try:
            err = d.get("error")
            msg = cls(
                kind=d["kind"],
                task_uuid=d["task_uuid"],
                attempt=int(d["attempt"]),
                endpoint_id=d["endpoint_id"],
                result=Payload.from_json(d["result"]) if d.get("result") is not None else None,
                error=ErrorInfo(code=err["code"], message=err["message"]) if err else None,
                worker=dict(d.get("worker") or {}),
                timings={k: float(v) for k, v in (d.get("timings") or {}).items()},
                receive_count=int(d.get("receive_count", 1)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"bad result message: {e}") from e
        msg.validate()
        return msg


def _dumps(d: dict[str, Any]) -> str:
    body = json.dumps(d, separators=(",", ":"), sort_keys=True)
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds the SQS limit; offload the payload to S3")
    return body


def _loads(body: str, expect_kind: str | None = None) -> dict[str, Any]:
    try:
        d = json.loads(body)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"not JSON: {e}") from e
    if not isinstance(d, dict):
        raise ProtocolError("message must be a JSON object")
    if d.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {d.get('v')!r}")
    if expect_kind is not None and d.get("kind") != expect_kind:
        raise ProtocolError(f"expected kind {expect_kind!r}, got {d.get('kind')!r}")
    return d


def task_payload_key(deployment: str, task_uuid: str, attempt: int) -> str:
    return f"payloads/{deployment}/tasks/{task_uuid}/{attempt}.task"


def result_payload_key(deployment: str, task_uuid: str, attempt: int) -> str:
    return (
        f"payloads/{deployment}/results/{task_uuid}/{executor_task_id(task_uuid, attempt)}.result"
    )
