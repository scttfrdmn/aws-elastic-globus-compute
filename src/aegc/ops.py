"""Operational views and housekeeping: ``aegc status`` and ``aegc reap``."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aegc import naming
from aegc.awsutil import is_not_found
from aegc.infra import Outcome, Report
from aegc.infra.deployment import read_manifest

#: Result queues untouched this long, and empty, belong to endpoint processes that are gone.
RESULT_QUEUE_IDLE_S = 14 * 24 * 3600


@dataclass
class PoolStatus:
    name: str
    env: str
    python_minor: str
    queued: int = 0
    running: int = 0
    dead_letters: int = 0
    desired: int = 0
    in_service: int = 0
    max_workers: int = 0
    last_activity: str = ""


@dataclass
class DeploymentStatus:
    deployment: str
    region: str
    pools: list[PoolStatus] = field(default_factory=list)
    result_queues: dict[str, int] = field(default_factory=dict)  # name -> waiting results

    def render(self) -> str:
        lines = [f"deployment {self.deployment} ({self.region})"]
        for p in self.pools:
            lines.append(
                f"  pool {p.name}: env {p.env}, Python {p.python_minor} | tasks queued {p.queued}, "
                f"running {p.running}, dead-lettered {p.dead_letters} | workers {p.in_service} "
                f"in service, {p.desired} desired, max {p.max_workers}"
            )
            if p.last_activity:
                lines.append(f"    last scaling activity: {p.last_activity}")
        for name, n in sorted(self.result_queues.items()):
            note = "  <- waiting; start the endpoint to deliver" if n else ""
            lines.append(f"  result queue {name}: {n} result(s){note}")
        return "\n".join(lines)


def _counts(sqs: Any, url: str) -> tuple[int, int]:
    a = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(a.get("ApproximateNumberOfMessages", 0)), int(
        a.get("ApproximateNumberOfMessagesNotVisible", 0)
    )


def status(
    *, ssm: Any, sqs: Any, autoscaling: Any, deployment: str, region: str
) -> DeploymentStatus:
    m = read_manifest(ssm, deployment)
    out = DeploymentStatus(deployment=deployment, region=region)
    for name, pool in sorted(m.get("pools", {}).items()):
        ps = PoolStatus(
            name=name,
            env=str(pool.get("env")),
            python_minor=str(pool.get("python_minor")),
            max_workers=int(pool.get("max_workers", 0)),
        )
        ps.queued, ps.running = _counts(sqs, pool["task_queue_url"])
        ps.dead_letters, _ = _counts(sqs, pool["dlq_url"])
        groups = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[pool["auto_scaling_group"]]
        )["AutoScalingGroups"]
        if groups:
            g = groups[0]
            ps.desired = int(g.get("DesiredCapacity", 0))
            ps.in_service = sum(
                1 for i in g.get("Instances", []) if i.get("LifecycleState") == "InService"
            )
            acts = autoscaling.describe_scaling_activities(
                AutoScalingGroupName=pool["auto_scaling_group"], MaxRecords=1
            ).get("Activities", [])
            if acts:
                a = acts[0]
                ps.last_activity = f"{a.get('StatusCode', '')}: {a.get('Description', '')}".strip()
        out.pools.append(ps)
    for url in _result_queues(sqs, deployment):
        visible, inflight = _counts(sqs, url)
        out.result_queues[url.rsplit("/", 1)[-1]] = visible + inflight
    return out


def _result_queues(sqs: Any, deployment: str) -> list[str]:
    prefix = f"{naming.PREFIX}-{deployment}-res-"
    urls = sqs.list_queues(QueueNamePrefix=prefix).get("QueueUrls", [])
    return [
        u
        for u in urls
        if naming.is_owned(
            u.rsplit("/", 1)[-1], dict(sqs.list_queue_tags(QueueUrl=u).get("Tags", {})), deployment
        )
    ]


def reap(
    *,
    sqs: Any,
    deployment: str,
    dry_run: bool = True,
    idle_s: float = RESULT_QUEUE_IDLE_S,
    now: float | None = None,
) -> Report:
    """Delete result queues of endpoint processes that are long gone: aegc-owned, empty, idle."""
    now = time.time() if now is None else now
    report = Report()
    verb: Outcome = "would-delete" if dry_run else "deleted"
    for url in _result_queues(sqs, deployment):
        name = url.rsplit("/", 1)[-1]
        a = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "LastModifiedTimestamp",
                "CreatedTimestamp",
            ],
        )["Attributes"]
        waiting = int(a.get("ApproximateNumberOfMessages", 0)) + int(
            a.get("ApproximateNumberOfMessagesNotVisible", 0)
        )
        last = max(int(a.get("LastModifiedTimestamp", 0)), int(a.get("CreatedTimestamp", 0)))
        if waiting:
            report.add("sqs queue", name, "kept", f"{waiting} result(s) waiting")
        elif now - last < idle_s:
            report.add("sqs queue", name, "kept", "recently active")
        else:
            if not dry_run:
                try:
                    sqs.delete_queue(QueueUrl=url)
                except Exception as e:
                    if not is_not_found(e):
                        raise
            report.add("sqs queue", name, verb, "idle result queue")
    return report
