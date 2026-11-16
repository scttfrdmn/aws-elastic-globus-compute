"""``aegc metrics`` and ``aegc provenance``: what the workers report (#39, #40).

Workers write EMF lines (:mod:`aegc.worker.emf`) that CloudWatch turns into metrics in namespace
``aegc``, and one JSON ``task_finished`` log line per task to ``/aegc/<d>``. ``aegc metrics``
reads the metrics, and the Auto Scaling group metrics deploy turns on, with GetMetricData;
``aegc provenance`` finds a task's lines with FilterLogEvents. Both are in the operator policy.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from aegc import naming
from aegc.worker import emf

#: (metric, CloudWatch statistic) pairs read per pool, from the [Deployment, Pool] set.
_POOL_STATS = (
    ("TasksCompleted", "Sum"),
    ("TasksFailed", "Sum"),
    ("ExecSeconds", "Sum"),
    ("ExecSeconds", "SampleCount"),
    ("ExecSeconds", "Maximum"),
    ("QueueWaitSeconds", "Sum"),
    ("QueueWaitSeconds", "SampleCount"),
    ("QueueWaitSeconds", "Maximum"),
    ("BootToFirstClaimSeconds", "Sum"),
    ("BootToFirstClaimSeconds", "SampleCount"),
    ("Interruptions", "Sum"),
    ("Releases", "Sum"),
    ("PoisonTasks", "Sum"),
    ("WorkerExit", "Sum"),
)
#: Auto Scaling group metrics (namespace AWS/Auto Scaling), in the group's capacity units.
_GROUP_STATS = (("GroupInServiceCapacity", "Maximum"), ("GroupDesiredCapacity", "Maximum"))


def period_for(span_s: float) -> int:
    """A GetMetricData period for a window: 1 minute up to 3 hours, 5 up to a day, else 1 hour
    (CloudWatch keeps 1-minute data for 15 days and 5-minute data for 63)."""
    if span_s <= 3 * 3600:
        return 60
    if span_s <= 86400:
        return 300
    return 3600


@dataclass
class PoolMetrics:
    name: str
    completed: float = 0
    failed: float = 0
    by_outcome: dict[str, float] = field(default_factory=dict)  # Outcome -> tasks
    exec_avg_s: float | None = None
    exec_max_s: float | None = None
    queue_wait_avg_s: float | None = None
    queue_wait_max_s: float | None = None
    boot_to_first_claim_avg_s: float | None = None
    interruptions: float = 0
    releases: float = 0
    poison: float = 0
    worker_exits: float = 0
    in_service_max: float | None = None
    desired_max: float | None = None
    unit: str = "vCPUs"

    def render(self) -> list[str]:
        def outcomes(names: tuple[str, ...]) -> str:
            parts = [f"{self.by_outcome[o]:g} {o}" for o in names if self.by_outcome.get(o)]
            return f" ({', '.join(parts)})" if parts else ""

        lines = [
            f"  pool {self.name}: {self.completed:g} task(s) completed"
            f"{outcomes(emf.COMPLETED_OUTCOMES)}, {self.failed:g} failed"
            f"{outcomes(emf.FAILED_OUTCOMES)}"
        ]
        times = []
        if self.exec_avg_s is not None:
            times.append(f"exec avg {_s(self.exec_avg_s)}, max {_s(self.exec_max_s)}")
        if self.queue_wait_avg_s is not None:
            times.append(
                f"queue wait avg {_s(self.queue_wait_avg_s)}, max {_s(self.queue_wait_max_s)}"
            )
        if self.boot_to_first_claim_avg_s is not None:
            times.append(f"boot to first claim avg {_s(self.boot_to_first_claim_avg_s)}")
        if times:
            lines.append("    " + " | ".join(times))
        events = (
            f"interruptions {self.interruptions:g}, releases {self.releases:g}, "
            f"poison {self.poison:g}, worker exits {self.worker_exits:g}"
        )
        if self.in_service_max is not None or self.desired_max is not None:
            events += (
                f" | {self.unit} in service max {_n(self.in_service_max)}, "
                f"desired max {_n(self.desired_max)}"
            )
        lines.append("    " + events)
        return lines


@dataclass
class MetricsSummary:
    deployment: str
    region: str
    start: float
    end: float
    period_s: int
    pools: list[PoolMetrics] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return any(
            p.completed or p.failed or p.exec_avg_s is not None or p.worker_exits
            or p.in_service_max is not None
            for p in self.pools
        )  # fmt: skip

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        lines = [
            f"metrics for {self.deployment} ({self.region}), {_utc(self.start)} to "
            f"{_utc(self.end)} (CloudWatch namespace {emf.NAMESPACE}; a few minutes behind)"
        ]
        for p in self.pools:
            lines.extend(p.render())
        if self.pools:
            done = sum(p.completed for p in self.pools)
            failed = sum(p.failed for p in self.pools)
            lines.append(f"  total: {done:g} completed, {failed:g} failed")
        if not self.has_data:
            lines.append(
                "  no data in this window: no tasks ran, or the workers' images predate aegc 0.4 "
                "(rebuild them to get metrics)"
            )
        return "\n".join(lines)


def metrics(
    *,
    cw: Any,
    manifest: dict[str, Any],
    deployment: str,
    region: str,
    start: float,
    end: float,
) -> MetricsSummary:
    """Per-pool totals over ``[start, end)`` from CloudWatch GetMetricData."""
    period = period_for(end - start)
    out = MetricsSummary(deployment, region, start, end, period)
    queries: list[dict[str, Any]] = []
    wanted: dict[str, tuple[str, str, str]] = {}  # query id -> (pool, metric or outcome, stat)

    def add(
        pool: str, key: str, stat: str, namespace: str, name: str, dims: dict[str, str]
    ) -> None:
        qid = f"q{len(queries)}"
        wanted[qid] = (pool, key, stat)
        queries.append(
            {
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": name,
                        "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                    },
                    "Period": period,
                    "Stat": stat,
                },
                "ReturnData": True,
            }
        )

    pools: dict[str, dict[str, Any]] = dict(manifest.get("pools") or {})
    for name in sorted(pools):
        dims = {"Deployment": deployment, "Pool": name}
        for metric, stat in _POOL_STATS:
            add(name, metric, stat, emf.NAMESPACE, metric, dims)
        for metric, outcomes in (
            ("TasksCompleted", emf.COMPLETED_OUTCOMES),
            ("TasksFailed", emf.FAILED_OUTCOMES),
        ):
            for o in outcomes:
                add(name, f"Outcome:{o}", "Sum", emf.NAMESPACE, metric, {**dims, "Outcome": o})
        group = str(
            pools[name].get("auto_scaling_group") or naming.auto_scaling_group(deployment, name)
        )
        for metric, stat in _GROUP_STATS:
            add(name, metric, stat, "AWS/AutoScaling", metric, {"AutoScalingGroupName": group})

    values: dict[str, list[float]] = {}
    for i in range(0, len(queries), 500):  # GetMetricData takes up to 500 queries
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"NextToken": token} if token else {}
            r = cw.get_metric_data(
                MetricDataQueries=queries[i : i + 500], StartTime=start, EndTime=end, **kw
            )
            for res in r.get("MetricDataResults", []):
                values.setdefault(str(res["Id"]), []).extend(
                    float(v) for v in res.get("Values", [])
                )
            token = r.get("NextToken")
            if not token:
                break

    agg: dict[tuple[str, str, str], float | None] = {}
    for qid, (pool, key, stat) in wanted.items():
        vs = values.get(qid, [])
        agg[(pool, key, stat)] = (max(vs) if stat == "Maximum" else sum(vs)) if vs else None

    def avg(pool: str, metric: str) -> float | None:
        total, n = agg.get((pool, metric, "Sum")), agg.get((pool, metric, "SampleCount"))
        return total / n if total is not None and n else None

    for name in sorted(pools):

        def get(key: str, stat: str = "Sum", pool: str = name) -> float:
            return agg.get((pool, key, stat)) or 0.0

        pm = PoolMetrics(
            name=name,
            completed=get("TasksCompleted"),
            failed=get("TasksFailed"),
            by_outcome={
                o: get(f"Outcome:{o}")
                for o in (*emf.COMPLETED_OUTCOMES, *emf.FAILED_OUTCOMES)
                if get(f"Outcome:{o}")
            },
            exec_avg_s=avg(name, "ExecSeconds"),
            exec_max_s=agg.get((name, "ExecSeconds", "Maximum")),
            queue_wait_avg_s=avg(name, "QueueWaitSeconds"),
            queue_wait_max_s=agg.get((name, "QueueWaitSeconds", "Maximum")),
            boot_to_first_claim_avg_s=avg(name, "BootToFirstClaimSeconds"),
            interruptions=get("Interruptions"),
            releases=get("Releases"),
            poison=get("PoisonTasks"),
            worker_exits=get("WorkerExit"),
            in_service_max=agg.get((name, "GroupInServiceCapacity", "Maximum")),
            desired_max=agg.get((name, "GroupDesiredCapacity", "Maximum")),
            unit="vCPUs" if pools[name].get("capacity_type") == "vcpu" else "instances",
        )
        out.pools.append(pm)
    return out


# --- provenance ----------------------------------------------------------------------------------


def provenance(
    *, logs: Any, deployment: str, task_uuid: str, start: float, end: float | None = None
) -> list[dict[str, Any]]:
    """The ``task_finished`` records of ``task_uuid`` in ``/aegc/<d>``, oldest first.

    One per attempt that finished (a released attempt has one too, with ``posted`` false). Uses
    a quoted-term filter on the task id and checks each line's JSON here, which also skips task
    output that happens to print the id.
    """
    tid = str(uuid.UUID(task_uuid))  # a plain UUID: nothing else goes into the filter pattern
    kw: dict[str, Any] = {
        "logGroupName": naming.log_group(deployment),
        "filterPattern": f'"{tid}"',
        "startTime": int(start * 1000),
    }
    if end is not None:
        kw["endTime"] = int(end * 1000)
    found: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        r = logs.filter_log_events(**kw, **({"nextToken": token} if token else {}))
        for e in r.get("events", []):
            try:
                rec = json.loads(e.get("message", ""))
            except ValueError:
                continue
            if (
                isinstance(rec, dict)
                and rec.get("event") == "task_finished"
                and rec.get("task_uuid") == tid
            ):
                found.append({**rec, "log_stream": e.get("logStreamName", "")})
        token = r.get("nextToken")
        if not token:
            break
    return sorted(found, key=lambda rec: (int(rec.get("attempt") or 0), str(rec.get("ts", ""))))


def render_provenance(task_uuid: str, deployment: str, records: list[dict[str, Any]]) -> str:
    lines = [f"task {task_uuid} in {deployment}: {len(records)} attempt(s) finished"]
    for rec in records:
        p = rec.get("provenance") or {}
        t = p.get("timings") or rec
        cost = p.get("est_cost_usd")
        lines.append(
            f"  attempt {rec.get('attempt')} (delivery {rec.get('receive_count')}): "
            f"{rec.get('outcome')} in {_s(t.get('exec_s'))}, queued {_s(t.get('queue_wait_s'))}"
            + ("" if rec.get("posted", True) else " (released: not reported)")
        )
        where = " ".join(
            str(v)
            for v in (
                p.get("instance_type") or "?",
                p.get("lifecycle"),
                p.get("availability_zone"),
                p.get("instance_id") or rec.get("instance_id"),
            )
            if v
        )
        lines.append(
            f"    {where} | pool {p.get('pool') or rec.get('pool', '?')}, "
            f"{p.get('slots', '?')} slot(s), {p.get('vcpus', '?')} vCPUs"
        )
        lines.append(f"    image {p.get('image') or '(unknown)'}")
        if cost is not None:
            lines.append(f"    estimated cost ${cost:.6f} ({p.get('est_cost_source') or 'static'})")
        lines.append(f"    finished {rec.get('ts', '')} in log stream {rec.get('log_stream', '')}")
    return "\n".join(lines)


def _s(v: Any) -> str:
    return "?" if v is None else f"{float(v):.1f} s"


def _n(v: float | None) -> str:
    return "?" if v is None else f"{v:g}"


def _utc(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))
