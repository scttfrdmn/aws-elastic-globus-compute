"""Build real packed Globus Compute tasks and read results, for tests and selftest."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any


def pack_task(fn: Callable[..., Any], *args: Any, task_uuid: str, **kwargs: Any) -> bytes:
    """Pack ``fn(*args, **kwargs)`` the way the Globus Compute SDK does."""
    from globus_compute_common import messagepack
    from globus_compute_common.messagepack.message_types import Task
    from globus_compute_sdk.serialize import ComputeSerializer

    s = ComputeSerializer()
    buf = s.pack_buffers([s.serialize(fn), s.serialize(args), s.serialize(kwargs)])
    return bytes(messagepack.pack(Task(task_id=uuid.UUID(task_uuid), task_buffer=buf)))


def unpack_result(packed: bytes) -> tuple[bool, Any]:
    """(is_error, value): the deserialized return value, or the error text."""
    from globus_compute_common import messagepack
    from globus_compute_common.messagepack.message_types import Result
    from globus_compute_sdk.serialize import ComputeSerializer

    result = messagepack.unpack(packed)
    if not isinstance(result, Result):
        raise TypeError(f"expected a packed Result, got {type(result).__name__}")
    if result.is_error:
        return True, result.data
    return False, ComputeSerializer().deserialize(result.data)
