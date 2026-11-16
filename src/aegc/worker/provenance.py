"""Provenance in results (#40): where and how a task ran, in the packed Result's ``details``.

execute_task already fills ``details`` (OS, Python and SDK versions, the endpoint id). The worker
adds ``details["aegc"]``: the image digest, instance type, Availability Zone, Spot or on-demand,
vCPUs and slots, attempt, receive count, timings and an estimated cost. The same record goes in the
worker's ``task_finished`` log line, which ``aegc provenance`` reads if a client doesn't see
``details``.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("aegc.worker")

KEY = "aegc"


def merge(packed: bytes, aegc: dict[str, Any]) -> tuple[bytes, bool | None]:
    """``packed`` with ``details["aegc"] = aegc`` merged in, and whether it's an error Result.

    Keeps every existing ``details`` key. Anything that isn't a packed Result, or doesn't repack,
    comes back unchanged (with ``None`` for the error flag when it couldn't be read).
    """
    from globus_compute_common import messagepack
    from globus_compute_common.messagepack.message_types import Result

    try:
        result = messagepack.unpack(packed)
    except Exception as e:
        log.warning("couldn't unpack a result to add provenance: %s", e)
        return packed, None
    if not isinstance(result, Result):
        return packed, None
    try:
        details = dict(result.details or {})
        details[KEY] = {**dict(details.get(KEY) or {}), **aegc}
        result.details = details
        return bytes(messagepack.pack(result)), result.is_error
    except Exception as e:
        log.warning("couldn't add provenance to a result: %s", e)
        return packed, result.is_error


def details(packed: bytes) -> dict[str, Any]:
    """A packed Result's ``details`` (``{}`` if none)."""
    from globus_compute_common import messagepack

    return dict(getattr(messagepack.unpack(packed), "details", None) or {})
