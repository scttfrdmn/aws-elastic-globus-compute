"""``aegc evaluate``: cloud-side checks that don't wait for an engine to run (#51, #60).

The cloud endpoint's host runs this every 5 minutes:

1. **Liveness (#60).** Ask Globus whether the endpoint is online and publish ``EndpointOnline``
   (namespace ``aegc/endpoint``, dimensions ``Deployment`` and a secret ``Token``). An alarm
   fires when it's missing or 0.
2. **Caps (#51).** Check spend against the deployment's limits and trip it, as an engine would,
   even when no engine (user endpoint process) is running.
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

from aegc import caps, lease, ops
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
    """One pass. ``clients`` needs ssm, s3, autoscaling, scheduler, sqs (ec2 and pricing are
    optional, for live prices)."""
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
            )
            trip = guard.check(limits, now)
            if trip is not None:
                ev.tripped = trip.reason
                if not dry_run:
                    groups = _group_minimums(clients["autoscaling"], m)
                    _, err = caps.trip(
                        ssm=clients["ssm"], autoscaling=clients["autoscaling"],
                        deployment=deployment, cap=trip, limits=limits, groups=groups,
                        by="evaluator", now=now,
                    )  # fmt: skip
                    if err is not None:
                        ev.errors.append(f"cap: {err}")
                    ev.state = "capped"
        except Exception as e:
            ev.errors.append(f"spend check: {e}")

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
    names = [str(p["auto_scaling_group"]) for p in manifest.get("pools", {}).values()]
    out: dict[str, int] = {}
    for i in range(0, len(names), 50):
        resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=names[i : i + 50])
        for g in resp["AutoScalingGroups"]:
            out[str(g["AutoScalingGroupName"])] = int(g.get("MinSize", 0))
    return out
