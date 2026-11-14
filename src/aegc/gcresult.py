"""Globus Compute Result helpers shared by the engine and the worker."""

from __future__ import annotations

import uuid


def failed_result(task_uuid: str, code: str, message: str) -> bytes:
    """A packed Globus Compute Result reporting a failure the task itself couldn't report."""
    from globus_compute_common import messagepack
    from globus_compute_common.messagepack.message_types import Result, ResultErrorDetails

    return bytes(
        messagepack.pack(
            Result(
                task_id=uuid.UUID(task_uuid),
                data=message,
                error_details=ResultErrorDetails(code=code, user_message=message),
            )
        )
    )
