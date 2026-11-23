"""Pools of kind ``lambda`` (#21): what the deployer, engine and controls share.

A Lambda pool is one Lambda function (the pool's worker image, command ``aegc-worker lambda``)
fed by the pool's task queue through an SQS event source mapping. It has no Auto Scaling group:
Lambda scales it, up to the mapping's ``MaximumConcurrency``. Freezing it (caps, ``aegc stop``,
the lease and TTL) disables the mapping; resuming enables it again.

**Manifest pool entry** (``/aegc/<d>/manifest`` ``pools[<name>]``): the :class:`PoolSpec` fields
(``kind="lambda"``, ``memory_mib``, ``timeout_s``, ``max_concurrency``, ``ephemeral_mib``, ...)
plus:

- ``task_queue_url``, ``dlq_url``: as for EC2 pools
- ``function_name``: ``aegc-<d>-<pool>`` (:func:`function_name`)
- ``function_arn``
- ``event_source_mapping``: the mapping's UUID
- ``function_role_arn``: the pool's own role, ``aegc-<d>-<pool>-<region>``

and no ``auto_scaling_group``, ``launch_template`` or ``capacity_type``.
"""

from __future__ import annotations

from typing import Any

from aegc import naming

KIND = "lambda"
#: Lambda's limits (on-demand functions, 2026-11): timeout, memory, /tmp, mapping concurrency.
MAX_TIMEOUT_S = 1800
MEMORY_MIB_RANGE = (128, 10_240)
EPHEMERAL_MIB_RANGE = (512, 10_240)
CONCURRENCY_RANGE = (2, 1000)
MAX_IMAGE_BYTES = 10 * 2**30  # uncompressed
#: Kept from each invocation's time for posting the result (aegc.worker.lambda_handler.MARGIN_S).
MARGIN_S = 20

DEFAULT_MEMORY_MIB = 2048
DEFAULT_TIMEOUT_S = 900
DEFAULT_CONCURRENCY = 10


def is_lambda(entry: dict[str, Any]) -> bool:
    """Whether a manifest pool entry is a Lambda pool (EC2 entries have no ``kind``)."""
    return str(entry.get("kind") or "ec2") == KIND


def function_name(deployment: str, pool: str) -> str:
    return f"{naming.PREFIX}-{deployment}-{pool}"


def function_role(deployment: str, pool: str, region: str) -> str:
    return f"{naming.PREFIX}-{deployment}-{pool}-{region}"


def max_walltime_s(entry: dict[str, Any]) -> int:
    """The longest walltime a task on this pool may ask for."""
    return int(entry.get("timeout_s") or DEFAULT_TIMEOUT_S) - MARGIN_S


def set_enabled(lam: Any, entry: dict[str, Any], enabled: bool) -> None:
    """Enable or disable a Lambda pool's event source mapping (freeze or resume it)."""
    lam.update_event_source_mapping(UUID=str(entry["event_source_mapping"]), Enabled=enabled)
