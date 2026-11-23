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
#: At least 1 GiB: the runtime, the forkserver (which preloads globus_compute_endpoint) and the
#: task share it, and at 1769 MB Lambda gives one vCPU.
MEMORY_MIB_RANGE = (1024, 10_240)
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

    ``confirm`` (optional) re-reads the state and returns whether it's running: it's called
    before changing anything (nothing changes if it disagrees with ``running``) and again after
    enabling (a stop that landed in between is undone at once). Returns (mapping state by pool,
    what changed by pool: ``enabled``, ``disabled``, or the error, including mappings whose state
    couldn't be read).
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
    unread = {n: s for n, s in states.items() if s.startswith("unknown")}
    if not wrong or (confirm is not None and bool(confirm()) != running):
        return states, dict(unread)
    verb = "enabled" if running else "disabled"
    if dry_run:
        return states, {**unread, **{n: verb for n in wrong}}
    errors = set_all(lam, wrong, running)
    changes = {**unread, **{n: str(errors[n]) if n in errors else verb for n in wrong}}
    for n in wrong:
        if n not in errors:
            states[n] = "Enabling" if running else "Disabling"
    if running and confirm is not None and not confirm():
        # Stopped or capped while we enabled: freeze again now, not on the next pass.
        set_all(lam, {n: u for n, u in wrong.items() if n not in errors}, False)
        for n in wrong:
            if n not in errors:
                states[n], changes[n] = "Disabling", "disabled"
    return states, changes


# --- spend ------------------------------------------------------------------------------------

#: GetMetricData period: hourly sums are kept for 455 days.
METRIC_PERIOD_S = 3600


def accrue_metrics(
    cloudwatch: Any,
    pools: dict[str, dict[str, Any]],
    start: float,
    end: float,
    deployment: str = "",
) -> pricing.Accrued:
    """Lambda pools' spend in ``[start, end)`` from Lambda's own metrics (``AWS/Lambda``
    ``Duration`` and ``Invocations`` by function), priced at each pool's memory.

    Not from the cost ledger: tasks run with the function role, so anything that role could write
    a task could rewrite, and an invocation that times out never gets to write. Init isn't in
    ``Duration`` but images are billed for it (about 5 s per execution environment, found live):
    with ``deployment``, it comes from the handler's ``aegc`` ``InitSeconds`` metric, written
    before any task runs in that environment (a task could only add to it). Hours are rounded
    out to whole hours, so a window that doesn't start on the hour runs a little high. Lambda time
    isn't instance-hours.
    """
    from datetime import UTC, datetime

    lam = {n: p for n, p in sorted(pools.items()) if is_lambda(p)}
    if not lam or end <= start:
        return pricing.Accrued(0.0, 0.0, {}, {}, frozenset())
    queries = []
    for i, entry in enumerate(lam.values()):
        fn = str(entry.get("function_name") or "")
        for metric, stat in (("Duration", "Sum"), ("Invocations", "Sum")):
            queries.append(
                {
                    "Id": f"{metric[0].lower()}{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Lambda",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "FunctionName", "Value": fn}],
                        },
                        "Period": METRIC_PERIOD_S,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
        if deployment:
            pool = list(lam)[i]
            queries.append(
                {
                    "Id": f"n{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "aegc",
                            "MetricName": "InitSeconds",
                            "Dimensions": [
                                {"Name": "Deployment", "Value": deployment},
                                {"Name": "Pool", "Value": pool},
                            ],
                        },
                        "Period": METRIC_PERIOD_S,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
            )
    lo = start - start % METRIC_PERIOD_S
    totals: dict[str, float] = {}
    for i in range(0, len(queries), 500):
        kw: dict[str, Any] = {
            "MetricDataQueries": queries[i : i + 500],
            "StartTime": datetime.fromtimestamp(lo, UTC),
            "EndTime": datetime.fromtimestamp(end, UTC),
        }
        while True:
            r = cloudwatch.get_metric_data(**kw)
            for res in r.get("MetricDataResults", []):
                totals[res["Id"]] = totals.get(res["Id"], 0.0) + sum(res.get("Values", []))
            if not r.get("NextToken"):
                break
            kw["NextToken"] = r["NextToken"]
    usd = 0.0
    by_pool: dict[str, float] = {}
    by_type: dict[str, float] = {}
    for i, (pool, entry) in enumerate(lam.items()):
        billed_s = totals.get(f"d{i}", 0.0) / 1000 + totals.get(f"n{i}", 0.0)
        cost = pricing.lambda_cost(billed_s, memory_mib(entry), int(totals.get(f"i{i}", 0)))
        if not cost:
            continue
        usd += cost
        by_pool[pool] = by_pool.get(pool, 0.0) + cost
        kind = f"lambda-{memory_mib(entry)}MB"
        by_type[kind] = by_type.get(kind, 0.0) + cost
    return pricing.Accrued(usd, 0.0, by_pool, by_type, frozenset({"static"} if usd else set()))
