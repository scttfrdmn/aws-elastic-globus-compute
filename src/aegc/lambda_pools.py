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

import time
from typing import Any

from aegc import naming, pricing
from aegc.awsutil import error_code, is_not_found

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


# --- freezing and resuming every Lambda pool of a deployment (engine, caps, controls, lease) ------

#: Lambda's arm64 vCPUs per MiB of memory: one vCPU at 1769 MB, up to 6 at 10,240.
MIB_PER_VCPU = 1769
MAX_VCPUS = 6
#: Mapping states (GetEventSourceMapping ``State``) that are, or are becoming, enabled.
ENABLED_STATES = frozenset({"Enabled", "Enabling", "Creating"})


def mappings(pools: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Pool name -> event source mapping UUID, for a manifest's Lambda pools."""
    return {
        name: str(p["event_source_mapping"])
        for name, p in sorted(pools.items())
        if is_lambda(p) and p.get("event_source_mapping")
    }


def set_all(
    lam: Any, maps: dict[str, str], enabled: bool, *, sleep: Any = None
) -> dict[str, Exception]:
    """Enable or disable each mapping (pool -> UUID). One failing doesn't stop the rest; returns
    the errors by pool. A mapping mid-update (ResourceInUseException) is retried a few times."""
    sleep = time.sleep if sleep is None else sleep
    errors: dict[str, Exception] = {}
    for pool, uuid in sorted(maps.items()):
        for attempt in range(5):
            try:
                lam.update_event_source_mapping(UUID=uuid, Enabled=enabled)
                errors.pop(pool, None)
                break
            except Exception as e:
                errors[pool] = e
                if is_not_found(e) or error_code(e) != "ResourceInUseException" or attempt == 4:
                    break
                sleep(1.0)
    return errors


def mapping_state(lam: Any, entry: dict[str, Any]) -> str:
    """The mapping's ``State`` (``Enabled``, ``Disabled``, ``Enabling``, ...), or ``""``."""
    r = lam.get_event_source_mapping(UUID=str(entry["event_source_mapping"]))
    return str(r.get("State") or "")


def memory_mib(entry: dict[str, Any]) -> int:
    return int(entry.get("memory_mib") or DEFAULT_MEMORY_MIB)


def concurrency(entry: dict[str, Any]) -> int:
    return int(entry.get("max_concurrency") or DEFAULT_CONCURRENCY)


def per_task(entry: dict[str, Any]) -> tuple[float, float]:
    """(vCPUs, GiB) one task gets: one invocation at a time per execution environment."""
    mib = memory_mib(entry)
    return round(min(MAX_VCPUS, mib / MIB_PER_VCPU), 2), mib / 1024


def worst_case_usd_per_hour(entry: dict[str, Any]) -> float:
    """Every execution environment the mapping allows, busy for an hour."""
    return concurrency(entry) * pricing.lambda_hourly(memory_mib(entry))


def describe(entry: dict[str, Any]) -> str:
    """``lambda 2048 MB x 10``."""
    return f"lambda {memory_mib(entry)} MB x {concurrency(entry)}"


def reconcile(
    lam: Any,
    pools: dict[str, dict[str, Any]],
    running: bool,
    *,
    confirm: Any = None,
    dry_run: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    """Make each Lambda pool's mapping follow the deployment state: enabled while it's running,
    disabled otherwise. A cap whose period ends reads as running without anyone writing it, so
    something has to turn its pools back on; and a freeze whose mapping update failed is retried.

    ``confirm`` (optional) re-reads the state before enabling anything and returns whether it's
    still running. Returns (mapping state by pool, what changed by pool: ``enabled``,
    ``disabled`` or the error).
    """
    states: dict[str, str] = {}
    for name, uuid in mappings(pools).items():
        try:
            states[name] = mapping_state(lam, {"event_source_mapping": uuid})
        except Exception as e:
            states[name] = f"unknown ({error_code(e) or e})"
    if running:
        wrong = {n: u for n, u in mappings(pools).items() if states.get(n) == "Disabled"}
    else:
        wrong = {n: u for n, u in mappings(pools).items() if states.get(n) in ENABLED_STATES}
    if not wrong or (running and confirm is not None and not confirm()):
        return states, {}
    verb = "enabled" if running else "disabled"
    if dry_run:
        return states, {n: verb for n in wrong}
    errors = set_all(lam, wrong, running)
    changes = {n: str(errors[n]) if n in errors else verb for n in wrong}
    for n in wrong:
        if n not in errors:
            states[n] = "Enabling" if running else "Disabling"
    return states, changes
