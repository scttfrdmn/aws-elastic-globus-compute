"""EC2 vCPU quota preflight for ``aegc deploy`` and ``aegc status``.

Pools launch nothing past the account's vCPU quota: Auto Scaling just records failed launches and
tasks wait. New accounts get 5 Spot vCPUs by default, so check before it bites.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aegc.awsutil import error_code

#: Service Quotas codes and AWS/Usage classes for standard (A, C, D, H, I, M, R, T, Z) instances.
QUOTAS = {
    "spot": ("L-34B43A08", "Standard/Spot"),
    "on-demand": ("L-1216C47A", "Standard/OnDemand"),
}


@dataclass
class QuotaCheck:
    kind: str  # "spot" or "on-demand"
    quota: float | None  # None: couldn't read it
    used: float  # vCPUs in use now (all of the account's instances, not only aegc's)
    worst_case: int  # every pool of this kind at max workers, each at its largest size
    minimums: dict[str, int] = field(default_factory=dict)  # pool -> vCPUs of its smallest worker
    note: str = ""

    @property
    def code(self) -> str:
        return QUOTAS[self.kind][0]

    def increase_hint(self, value: int) -> str:
        return (
            "aws service-quotas request-service-quota-increase --service-code ec2 "
            f"--quota-code {self.code} --desired-value {value}"
        )

    @property
    def errors(self) -> list[str]:
        if self.quota is None:
            return []
        quota = self.quota
        return [
            f"pool {name}: the {self.kind} vCPU quota is {quota:g}, smaller than one worker "
            f"({n} vCPUs), so no worker can ever launch. "
            f"Request more: {self.increase_hint(max(self.worst_case, 16))}"
            for name, n in sorted(self.minimums.items())
            if n > quota
        ]

    @property
    def warnings(self) -> list[str]:
        if self.quota is None:
            return [f"couldn't read the {self.kind} vCPU quota ({self.note}); not checked"]
        if self.errors:
            return []
        free = self.quota - self.used
        if self.worst_case > free:
            return [
                f"{self.kind} vCPU quota {self.quota:g} with {self.used:g} in use leaves {free:g}; "
                f"{', '.join(sorted(self.minimums))} can ask for up to {self.worst_case}, so "
                "scale-out will "
                f"stop short. Request more: "
                f"{self.increase_hint(int(self.used) + self.worst_case)}"
            ]
        return []

    def render(self) -> str:
        quota = "unknown" if self.quota is None else f"{self.quota:g}"
        return (
            f"  {self.kind} vCPU quota {quota}, in use {self.used:g}; "
            f"pools can use up to {self.worst_case}"
        )


def vcpu_quota(sq: Any, code: str) -> tuple[float | None, str]:
    """The applied quota, else AWS's default; ``(None, reason)`` if neither is readable."""
    for call in (sq.get_service_quota, sq.get_aws_default_service_quota):
        try:
            return float(call(ServiceCode="ec2", QuotaCode=code)["Quota"]["Value"]), ""
        except Exception as e:
            reason = error_code(e) or type(e).__name__
            if reason != "NoSuchResourceException":
                return None, reason
    return None, "NoSuchResourceException"


def vcpu_usage(cw: Any, usage_class: str, now: float | None = None) -> float:
    """Peak vCPUs in use over the last 15 minutes (``AWS/Usage``); 0 when nothing is running."""
    now = time.time() if now is None else now
    try:
        r = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "u",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Usage",
                            "MetricName": "ResourceCount",
                            "Dimensions": [
                                {"Name": "Service", "Value": "EC2"},
                                {"Name": "Type", "Value": "Resource"},
                                {"Name": "Resource", "Value": "vCPU"},
                                {"Name": "Class", "Value": usage_class},
                            ],
                        },
                        "Period": 60,
                        "Stat": "Maximum",
                    },
                }
            ],
            StartTime=now - 900,
            EndTime=now,
        )
    except Exception:
        return 0.0
    values = [v for res in r.get("MetricDataResults", []) for v in res.get("Values", [])]
    return float(max(values, default=0.0))


def check(pools: Iterable[Mapping[str, Any]], *, sq: Any, cw: Any) -> list[QuotaCheck]:
    """One check per kind of capacity the pools use. ``pools`` are PoolSpec dicts or manifest
    entries (``name``, ``spot``, ``max_vcpus``, ``vcpu_min``)."""
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for p in pools:
        by_kind.setdefault("spot" if p.get("spot", True) else "on-demand", []).append(p)
    out = []
    for kind, ps in sorted(by_kind.items()):
        code, usage_class = QUOTAS[kind]
        quota, note = vcpu_quota(sq, code)
        out.append(
            QuotaCheck(
                kind=kind,
                quota=quota,
                used=vcpu_usage(cw, usage_class),
                worst_case=sum(_max_vcpus(p) for p in ps),
                minimums={str(p["name"]): int(p["vcpu_min"]) for p in ps},
                note=note,
            )
        )
    return out


def _max_vcpus(p: Mapping[str, Any]) -> int:
    """A pool's vCPU ceiling (v0.1 manifests count workers: each at its largest size)."""
    if "max_vcpus" in p:
        return int(p["max_vcpus"])
    return int(p.get("max_workers", 0)) * int(p.get("vcpu_max", 16))
