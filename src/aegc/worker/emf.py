"""CloudWatch metrics from the worker, as Embedded Metric Format (EMF) lines on stdout.

The worker's container logs through the awslogs driver with ``awslogs-format=json/emf``, so
CloudWatch Logs extracts these lines into metrics in namespace ``aegc``. Workers need no
``cloudwatch:PutMetricData``.

Dimensions are low-cardinality by design (CloudWatch bills per metric, and each distinct dimension
value is a new metric): ``[Deployment, Pool]`` and ``[Deployment]``, and on the task counts also
``[Deployment, Pool, Outcome]``. Never task or instance ids; those go in the JSON log lines.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any

NAMESPACE = "aegc"

UNITS: dict[str, str] = {
    "TasksCompleted": "Count",  # the task ran and returned a Result (Outcome ok or task_error)
    "TasksFailed": "Count",  # aegc failed it (Outcome walltime, crashed, poison, ...)
    "ExecSeconds": "Seconds",
    "QueueWaitSeconds": "Seconds",  # submit (the engine's clock) to claim
    "BootToFirstClaimSeconds": "Seconds",
    "Interruptions": "Count",  # Spot interruption notices
    "Releases": "Count",  # in-flight tasks handed back to the queue
    "PoisonTasks": "Count",  # delivered too often; failed without running
    "WorkerExit": "Count",
}
TASK_COUNTS = frozenset({"TasksCompleted", "TasksFailed"})

#: Outcome values. TasksCompleted: the function ran (it may have raised: task_error).
COMPLETED_OUTCOMES = ("ok", "task_error")
FAILED_OUTCOMES = ("walltime", "crashed", "poison", "payload_missing", "refused")

BASE_DIMENSIONS = (("Deployment", "Pool"), ("Deployment",))
OUTCOME_DIMENSIONS = (*BASE_DIMENSIONS, ("Deployment", "Pool", "Outcome"))


def document(
    deployment: str,
    pool: str,
    values: dict[str, float],
    *,
    outcome: str = "",
    properties: dict[str, Any] | None = None,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """One EMF document. ``properties`` are logged with it but aren't metrics or dimensions."""
    unknown = set(values) - set(UNITS)
    if unknown:
        raise ValueError(f"unknown metrics {sorted(unknown)}")
    if set(values) & TASK_COUNTS and outcome not in (*COMPLETED_OUTCOMES, *FAILED_OUTCOMES):
        raise ValueError(f"task counts need a known Outcome, not {outcome!r}")
    plain = [n for n in values if n not in TASK_COUNTS]
    counts = [n for n in values if n in TASK_COUNTS]
    directives = []
    for names, dims in ((plain, BASE_DIMENSIONS), (counts, OUTCOME_DIMENSIONS)):
        if names:
            directives.append(
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(d) for d in dims],
                    "Metrics": [{"Name": n, "Unit": UNITS[n]} for n in names],
                }
            )
    doc: dict[str, Any] = {**(properties or {})}
    doc.update(
        {
            "_aws": {
                "Timestamp": int(time.time() * 1000) if timestamp_ms is None else timestamp_ms,
                "CloudWatchMetrics": directives,
            },
            "Deployment": deployment,
            "Pool": pool,
            **({"Outcome": outcome} if counts else {}),
            **{n: float(v) for n, v in values.items()},
        }
    )
    return doc


@dataclass
class Emitter:
    """Writes EMF lines; never raises (metrics mustn't break the worker)."""

    deployment: str
    pool: str
    stream: IO[str] | None = None  # None: sys.stdout at write time
    properties: dict[str, Any] = field(default_factory=dict)  # e.g. instance_id

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def emit(
        self, values: dict[str, float], *, outcome: str = "", **properties: Any
    ) -> dict[str, Any] | None:
        try:
            doc = document(
                self.deployment,
                self.pool,
                values,
                outcome=outcome,
                properties={**self.properties, **properties},
            )
            line = json.dumps(doc, default=str, separators=(",", ":"))
            with self._lock:
                out = self.stream or sys.stdout
                out.write(line + "\n")
                out.flush()
            return doc
        except Exception:
            return None
