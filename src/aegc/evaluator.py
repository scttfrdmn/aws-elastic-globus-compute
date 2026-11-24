"""``aegc evaluate``: cloud-side checks that don't wait for an engine to run (#51, #60).

The cloud endpoint's host runs this every 5 minutes:

1. **Liveness (#60).** Ask Globus whether the endpoint is online and publish ``EndpointOnline``
   (namespace ``aegc/endpoint``, dimensions ``Deployment`` and a secret ``Token``). An alarm
   fires when it's missing or 0.
2. **Caps (#51).** Check spend against the deployment's limits and trip it, as an engine would,
   even when no engine (user endpoint process) is running.
   Lambda pools (#21) then follow the state: their event source mappings are enabled while the
   deployment runs (a cap whose period ended reads as running) and disabled otherwise. So do
   S3-event triggers' rules (#83), given an ``events`` client.
3. **Lease.** Renew it, but only while the endpoint is online: the lease stays a dead-man switch
   for the endpoint, so a deployment whose endpoint is gone still freezes.
4. **Reap.** Delete result queues of user endpoint processes long gone.

Nothing changes with ``dry_run``; the report says what would.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aegc import caps, lambda_pools, lease, ops, triggers
from aegc.infra.deployment import read_manifest
from aegc.infra.iam import ENDPOINT_NAMESPACE
from aegc.pricing import Prices
from aegc.spend import SpendGuard
from aegc.state import Limits, read_state

log = logging.getLogger("aegc.evaluate")


@dataclass
class Evaluation:
    deployment: str
    online: bool | None = None  # None: couldn't tell
    state: str = ""
    tripped: str = ""
    lease_renewed: int = 0
    reaped: list[str] = field(default_factory=list)
    mappings: dict[str, str] = field(default_factory=dict)  # Lambda pool -> enabled/disabled/error
    rules: dict[str, str] = field(default_factory=dict)  # trigger rule -> enabled/disabled/error
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def render(self) -> str:
        verb = "would " if self.dry_run else ""
        lines = [
            f"evaluate {self.deployment}: endpoint "
            + {True: "online", False: "OFFLINE", None: "status unknown"}[self.online]
            + f", state {self.state or '?'}"
        ]
        if self.tripped:
            lines.append(f"  {verb}cap it: {self.tripped}")
        if self.lease_renewed:
            lines.append(f"  {verb}renew the lease ({self.lease_renewed} schedule(s))")
        for pool, change in sorted(self.mappings.items()):
            if change in ("enabled", "disabled"):
                done = f"would {change[:-1]}" if self.dry_run else change
                lines.append(f"  {done} Lambda pool {pool}'s event source mapping")
        for rule, change in sorted(self.rules.items()):
            if change in ("enabled", "disabled"):
                done = f"would {change[:-1]}" if self.dry_run else change
                lines.append(f"  {done} trigger rule {rule}")
        for q in self.reaped:
            lines.append(f"  {verb}delete idle result queue {q}")
        for e in self.errors:
            lines.append(f"  error: {e}")
        return "\n".join(lines)


def liveness_dimensions(deployment: str, token: str) -> list[dict[str, str]]:
    """``EndpointOnline``'s dimensions. The token is in the endpoint's secret only, so no one
    else (task code printing EMF into worker logs, say) can publish the series the alarm watches."""
    return [{"Name": "Deployment", "Value": deployment}, {"Name": "Token", "Value": token}]


def put_liveness(
    cloudwatch: Any, deployment: str, token: str, online: bool, *, now: float | None = None
) -> None:
    from datetime import UTC, datetime

    cloudwatch.put_metric_data(
        Namespace=ENDPOINT_NAMESPACE,
        MetricData=[
            {
                "MetricName": "EndpointOnline",
                "Dimensions": liveness_dimensions(deployment, token),
                "Timestamp": datetime.fromtimestamp(time.time() if now is None else now, UTC),
                "Value": 1.0 if online else 0.0,
                "Unit": "Count",
            }
        ],
    )


def globus_status(endpoint_id: str) -> bool:
    """Whether Globus says the endpoint is online (uses the stored login)."""
    from globus_compute_sdk import Client

    status = Client().get_endpoint_status(endpoint_id)
    return str(status.get("status", "")).lower() == "online"


def evaluate(
    *,
    deployment: str,
    region: str,
    clients: dict[str, Any],
    endpoint_id: str = "",
    metric_token: str = "",
    online: Callable[[str], bool] = globus_status,
    dry_run: bool = False,
    now: float | None = None,
) -> Evaluation:
    """One pass. ``clients`` needs ssm, s3, autoscaling, scheduler, sqs, and lambda for a
    deployment with Lambda pools (ec2 and pricing are optional, for live prices; events for
    S3-event triggers)."""
    now = time.time() if now is None else now
    ev = Evaluation(deployment=deployment, dry_run=dry_run)

    if endpoint_id:
        try:
            ev.online = bool(online(endpoint_id))
        except Exception as e:
            ev.errors.append(f"endpoint status: {e}")
        if ev.online is not None and not dry_run and metric_token and "cloudwatch" in clients:
            try:
                put_liveness(clients["cloudwatch"], deployment, metric_token, ev.online, now=now)
            except Exception as e:
                ev.errors.append(f"liveness metric: {e}")

    try:
        m = read_manifest(clients["ssm"], deployment)
        st = read_state(clients["ssm"], deployment, now=now)
    except Exception as e:
        ev.errors.append(f"manifest or state: {e}")
        return ev
    ev.state = st.mode

    limits = Limits.from_dict(m.get("limits"))
    if st.running and limits.any:
        try:
            guard = SpendGuard(
                s3=clients["s3"],
                bucket=str(m["bucket"]),
                deployment=deployment,
                prices=Prices(
                    region=region, ec2=clients.get("ec2"), pricing=clients.get("pricing")
                ),
                cloudwatch=clients.get("cloudwatch"),
                pools=dict(m.get("pools") or {}),
            )
            trip = guard.check(limits, now)
            if trip is not None:
                ev.tripped = trip.reason
                if not dry_run:
                    groups = _group_minimums(clients["autoscaling"], m)
                    _, err = caps.trip(
                        ssm=clients["ssm"], autoscaling=clients["autoscaling"],
                        deployment=deployment, cap=trip, limits=limits, groups=groups,
                        by="evaluator", now=now, lam=clients.get("lambda"),
                        mappings=lambda_pools.mappings(m.get("pools", {})),
                        events=clients.get("events"),
                    )  # fmt: skip
                    if err is not None:
                        ev.errors.append(f"cap: {err}")
                    ev.state = "capped"
        except Exception as e:
            ev.errors.append(f"spend check: {e}")

    pools = m.get("pools", {})
    if lambda_pools.mappings(pools):
        if "lambda" not in clients:
            ev.errors.append("Lambda pools: no lambda client")
        else:
            try:
                running = ev.state == "running"
                _, ev.mappings = lambda_pools.reconcile(
                    clients["lambda"], pools, running, dry_run=dry_run,
                    confirm=lambda: read_state(clients["ssm"], deployment, now=now).running,
                )  # fmt: skip
                ev.errors += [
                    f"Lambda pool {n}: {c}"
                    for n, c in ev.mappings.items()
                    if c not in ("enabled", "disabled")
                ]
            except Exception as e:
                ev.errors.append(f"Lambda pools: {e}")

    if "events" in clients:
        try:
            _, ev.rules = triggers.reconcile(
                clients["events"], deployment, ev.state == "running", dry_run=dry_run,
                confirm=lambda: read_state(clients["ssm"], deployment, now=now).running,
            )  # fmt: skip
            ev.errors += [
                f"trigger {n}: {c}" for n, c in ev.rules.items() if c not in ("enabled", "disabled")
            ]
        except Exception as e:
            ev.errors.append(f"triggers: {e}")

    lease_s = int((m.get("lease") or {}).get("lease_s") or 0)
    if ev.online and lease_s and ev.state in ("running", "capped"):
        try:
            expires = lease.expiry(clients["scheduler"], deployment)
            # Renew hourly (as engines do), not every pass.
            if expires is not None and expires - now < lease_s - lease.renew_interval(lease_s):
                ev.lease_renewed = (
                    1
                    if dry_run
                    else lease.renew(clients["scheduler"], deployment, lease_s, now=now)
                )
        except Exception as e:
            ev.errors.append(f"lease: {e}")

    try:
        r = ops.reap(sqs=clients["sqs"], deployment=deployment, dry_run=dry_run, now=now)
        ev.reaped = [a.name for a in r.actions if a.outcome in ("deleted", "would-delete")]
    except Exception as e:
        ev.errors.append(f"reap: {e}")
    for problem in ev.errors:
        log.warning("evaluate %s: %s", deployment, problem)
    return ev


def _group_minimums(autoscaling: Any, manifest: dict[str, Any]) -> dict[str, int]:
    names = [
        str(p["auto_scaling_group"])
        for p in manifest.get("pools", {}).values()
        if not lambda_pools.is_lambda(p) and p.get("auto_scaling_group")
    ]
    out: dict[str, int] = {}
    for i in range(0, len(names), 50):
        resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=names[i : i + 50])
        for g in resp["AutoScalingGroups"]:
            out[str(g["AutoScalingGroupName"])] = int(g.get("MinSize", 0))
    return out
