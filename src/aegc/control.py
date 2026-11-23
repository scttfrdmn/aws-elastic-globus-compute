"""``aegc stop / resume / limits / cost``: control a deployment's spending."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aegc import lambda_pools, ledger, pricing
from aegc.infra.deployment import manifest_generation, read_manifest, update_manifest
from aegc.state import Limits, State, period_start, read_state, write_state

HARD_STOP_GRACE_S = 120


@dataclass
class ControlReport:
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.lines.append(line)

    def __str__(self) -> str:
        return "\n".join(self.lines)


def _min_of(pool: dict[str, Any]) -> tuple[int, str]:
    if pool.get("capacity_type") == "vcpu":
        return int(pool.get("min_vcpus", 0)), "vCPUs"
    return int(pool.get("min_workers", 0)), "workers"


def _set_mappings(
    report: ControlReport, lam: Any, pools: dict[str, dict[str, Any]], enabled: bool
) -> None:
    """Enable or disable the Lambda pools' event source mappings, reporting each."""
    maps = lambda_pools.mappings(pools)
    if not maps:
        return
    verb = "enabled" if enabled else "disabled"
    if lam is None:
        for name in maps:
            report.add(f"  pool {name}: event source mapping NOT {verb} (no Lambda client)")
        return
    errors = lambda_pools.set_all(lam, maps, enabled)
    for name in maps:
        if name in errors:
            report.add(
                f"  pool {name}: couldn't {verb[:-1]} the event source mapping: {errors[name]}"
            )
        else:
            what = lambda_pools.describe(pools[name])
            report.add(f"  pool {name} ({what}): event source mapping {verb}")


def stop(
    *,
    ssm: Any,
    autoscaling: Any,
    deployment: str,
    hard: bool = False,
    grace_s: float = HARD_STOP_GRACE_S,
    sleep: Any = time.sleep,
    lam: Any = None,
) -> ControlReport:
    """Stop spending: state ``stopped``; every group's minimum to 0 so workers can leave; every
    Lambda pool's event source mapping disabled.

    Workers see the state within a minute, fail anything still queued with AEGC_STOPPED, finish
    their tasks (or, with ``hard``, fail them now), then leave their groups. With ``hard``, any
    instance still in a group after ``grace_s`` is terminated. Lambda invocations already running
    finish; records delivered after the state changed are refused with a final result.
    """
    report = ControlReport()
    m = read_manifest(ssm, deployment)
    st = State(
        mode="stopped",
        reason="aegc stop --hard" if hard else "aegc stop",
        since=time.time(),
        by="aegc stop",
        kill=hard,
    )
    write_state(ssm, deployment, st)
    report.add(f"deployment {deployment}: state stopped{' (hard)' if hard else ''}")
    pools = m.get("pools", {})
    _set_mappings(report, lam, pools, False)
    groups = {n: p["auto_scaling_group"] for n, p in pools.items() if not lambda_pools.is_lambda(p)}
    for name, group in sorted(groups.items()):
        autoscaling.update_auto_scaling_group(AutoScalingGroupName=group, MinSize=0)
        report.add(f"  pool {name}: minimum 0; workers drain within about a minute")
    if hard and lambda_pools.mappings(pools):
        report.add(
            "  Lambda pools: invocations already running finish (Lambda can't stop them early); "
            "records not yet started are refused with AEGC_STOPPED"
        )
    if hard and groups:
        sleep(grace_s)
        for name, group in sorted(groups.items()):
            # Desired 0 first: then terminating needs no decrement (which a vCPU-weighted group
            # can refuse) and nothing is replaced. Protected instances aren't terminated by this.
            autoscaling.set_desired_capacity(
                AutoScalingGroupName=group, DesiredCapacity=0, HonorCooldown=False
            )
            found = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group])[
                "AutoScalingGroups"
            ]
            for inst in found[0].get("Instances", []) if found else []:
                if inst.get("LifecycleState", "").startswith("Terminat"):
                    continue
                try:
                    autoscaling.terminate_instance_in_auto_scaling_group(
                        InstanceId=inst["InstanceId"], ShouldDecrementDesiredCapacity=False
                    )
                    report.add(f"  pool {name}: terminated {inst['InstanceId']}")
                except Exception as e:
                    report.add(f"  pool {name}: couldn't terminate {inst['InstanceId']}: {e}")
    return report


def resume(
    *,
    ssm: Any,
    autoscaling: Any,
    deployment: str,
    scheduler: Any | None = None,
    lam: Any = None,
) -> ControlReport:
    """Back to running: clear the state, restore each group's minimum from the manifest, enable
    the Lambda pools' event source mappings, and (given a scheduler client) push the lease
    forward."""
    report = ControlReport()
    m = read_manifest(ssm, deployment)
    before = read_state(ssm, deployment)
    write_state(ssm, deployment, State(reason="aegc resume", since=time.time(), by="aegc resume"))
    report.add(f"deployment {deployment}: {before.mode} -> running")
    pools = m.get("pools", {})
    for name, pool in sorted(pools.items()):
        if lambda_pools.is_lambda(pool):
            continue
        minimum, unit = _min_of(pool)
        autoscaling.update_auto_scaling_group(
            AutoScalingGroupName=pool["auto_scaling_group"], MinSize=minimum
        )
        report.add(f"  pool {name}: minimum {minimum} {unit}")
    _set_mappings(report, lam, pools, True)
    lease_s = int((m.get("lease") or {}).get("lease_s") or 0)
    if scheduler is not None and lease_s:
        from aegc import lease

        n = lease.renew(scheduler, deployment, lease_s)
        report.add(f"  lease: {n} schedule(s) pushed {lease_s // 3600} h ahead")
    return report


def set_limits(
    *,
    ssm: Any,
    autoscaling: Any,
    deployment: str,
    resume_now: bool = False,
    spend: Any | None = None,
    scheduler: Any | None = None,
    lam: Any = None,
    **changes: Any,
) -> ControlReport:
    """Change a deployment's spend limits (``"none"`` removes one). Running engines pick the
    new limits up within a minute. ``resume_now`` also ends a ``capped`` state."""
    report = ControlReport()
    m = read_manifest(ssm, deployment)
    limits = Limits.from_dict(m.get("limits")).merged(**changes)
    if any(v is not None for v in changes.values()):
        m["limits"] = limits.to_dict()
        m["generation"] = manifest_generation(m)
        update_manifest(ssm, deployment, m)
    report.add(f"deployment {deployment}: {describe_limits(limits)}")
    if resume_now:
        st = read_state(ssm, deployment)
        if st.mode == "capped":
            trip = spend.check(limits, time.time()) if spend is not None else None
            if trip is not None:  # it would only trip again within seconds
                raise ValueError(
                    f"still over a limit ({trip.reason}); raise it (e.g. --max-usd-day) to resume"
                )
            report.lines += resume(
                ssm=ssm, autoscaling=autoscaling, deployment=deployment, scheduler=scheduler,
                lam=lam,
            ).lines  # fmt: skip
        else:
            report.add(f"  state is {st.mode}; nothing to resume")
    return report


def describe_limits(limits: Limits) -> str:
    parts = []
    if limits.max_usd_day is not None:
        parts.append(f"${limits.max_usd_day:g}/day")
    if limits.max_usd_month is not None:
        parts.append(f"${limits.max_usd_month:g}/month")
    if limits.max_instance_hours_day is not None:
        parts.append(f"{limits.max_instance_hours_day:g} instance-hours/day")
    if not parts:
        return "no spend limits"
    return f"limits {', '.join(parts)} (on_cap={limits.on_cap})"


# --- cost -----------------------------------------------------------------------------------------


@dataclass
class CostReport:
    deployment: str
    start: float
    end: float
    accrued: pricing.Accrued
    by: str
    actual: str = ""

    def render(self) -> str:
        span = f"{_iso(self.start)} to {_iso(self.end)}"
        lines = [
            f"deployment {self.deployment}, {span}: about ${self.accrued.usd:.4f} "
            f"({self.accrued.instance_hours:.2f} instance-hours; prices: "
            f"{', '.join(sorted(self.accrued.sources)) or 'none'})"
        ]
        rows = self.accrued.by_pool if self.by == "pool" else self.accrued.by_type
        for key, usd in sorted(rows.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {key or '?'}: ${usd:.4f}")
        lines.append(
            "  estimate from the cost ledger: instance time x Spot or on-demand price, plus root "
            "volume and IPv4; Lambda pools (lambda-<memory>MB): billed time x memory, plus "
            "requests; SQS, S3, ECR, logs and data transfer aren't included"
        )
        if self.actual:
            lines.append(self.actual)
        return "\n".join(lines)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def cost(
    *,
    s3: Any,
    bucket: str,
    deployment: str,
    prices: pricing.Prices,
    since: str = "today",
    by: str = "pool",
    now: float | None = None,
) -> CostReport:
    now = time.time() if now is None else now
    start = window_start(since, now)
    recs = list(ledger.read(s3, bucket, deployment, start, now))
    return CostReport(deployment, start, now, pricing.accrue(recs, start, now, prices), by)


def window_start(since: str, now: float) -> float:
    """``today``, ``month``, or a duration such as ``24h``, ``7d``, ``90m``."""
    if since == "today":
        return period_start("day", now)
    if since == "month":
        return period_start("month", now)
    units = {"m": 60, "h": 3600, "d": 86400}
    if since and since[-1] in units and since[:-1].isdigit():
        return now - int(since[:-1]) * units[since[-1]]
    raise ValueError(f"--since must be today, month, or a duration like 24h; not {since!r}")


def actual_cost(ce: Any, deployment: str, start: float, end: float) -> str:
    """Cost Explorer's figure for resources tagged ``aegc:deployment`` (needs the tag active;
    each call costs $0.01; data lags by up to a day)."""
    day = 86400
    s, e = (
        time.strftime("%Y-%m-%d", time.gmtime(start)),
        time.strftime("%Y-%m-%d", time.gmtime(end + day)),
    )
    try:
        r = ce.get_cost_and_usage(
            TimePeriod={"Start": s, "End": e},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": "aegc:deployment", "Values": [deployment]}},
        )
    except Exception as ex:
        return f"  Cost Explorer: unavailable ({ex})"
    total = sum(float(p["Total"]["UnblendedCost"]["Amount"]) for p in r.get("ResultsByTime", []))
    return (
        f"  Cost Explorer (tag aegc:deployment={deployment}, {s} to {e}): ${total:.4f}. "
        "If this is 0, activate the aegc:deployment cost allocation tag in Billing."
    )
