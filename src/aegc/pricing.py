"""Instance prices for cost estimates and spend caps.

Spot prices come from ``DescribeSpotPriceHistory`` (latest price per type and AZ, cached for an
hour), on-demand prices from the Price List API (cached for a day). If those aren't available, a
small static table of Graviton on-demand prices (2026-11) is used and labelled ``static``. It
holds for us-west-2 and us-east-1, whose prices for these types are the same; other regions differ
by a few percent. Every rate includes the worker's root volume and public IPv4 address.

One function, :func:`accrue`, turns ledger records into dollars. Both ``aegc cost`` and the
engine's spend caps use it, so what you see is what's enforced.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

HOURS_PER_MONTH = 730.0
GP3_GB_MONTH = 0.08
GP3_MIBPS_MONTH = 0.04  # per MiB/s above the included 125
GP3_INCLUDED_MIBPS = 125
IPV4_HOUR = 0.005
RUNNING_SLACK_S = 1200
FAILED_LOOKUP_TTL_S = 3600  # retry a failed price lookup at most hourly

#: On-demand $/h, read 2026-11-14 (Price List API) for us-west-2; us-east-1's are the same
#: (checked for c8g, m8g, c9g, r9g). Not region-specific: a fallback only, in any region.
STATIC_ON_DEMAND = {
    "c7g.large": 0.0725,
    "m7g.large": 0.0816,
    "r7g.large": 0.1071,
    "c8g.large": 0.0798,
    "m8g.large": 0.0898,
    "r8g.large": 0.1178,
    "c9g.large": 0.0869,
    "m9g.large": 0.0978,
    "r9g.large": 0.1284,
    "c8gd.large": 0.0980,
    "c9gd.large": 0.1068,
}
#: Per-vCPU fallback for types not in the table (about the most expensive general family).
STATIC_PER_VCPU = 0.065

_SIZE = re.compile(r"^(?P<family>[a-z0-9-]+)\.(?P<size>[a-z0-9]+)$")
_MULT = {"medium": 1, "large": 2, "xlarge": 4}


def vcpus(instance_type: str) -> int:
    """vCPUs of a Graviton type from its size name (medium 1, large 2, xlarge 4, Nxlarge 4N)."""
    m = _SIZE.match(instance_type)
    if not m:
        return 2
    size = m["size"]
    if size in _MULT:
        return _MULT[size]
    n = re.match(r"^(\d+)xlarge$", size)
    if n:
        return 4 * int(n[1])
    return 2


def scale_static(instance_type: str) -> float:
    """Static on-demand $/h: the table's ``.large`` price scaled by size, else per vCPU."""
    family = instance_type.split(".")[0]
    base = STATIC_ON_DEMAND.get(f"{family}.large")
    if base is not None:
        return base * vcpus(instance_type) / 2
    return STATIC_PER_VCPU * vcpus(instance_type)


def volume_hour(root_gib: int, throughput_mibps: int) -> float:
    extra = max(0, throughput_mibps - GP3_INCLUDED_MIBPS)
    return (root_gib * GP3_GB_MONTH + extra * GP3_MIBPS_MONTH) / HOURS_PER_MONTH


@dataclass(frozen=True)
class Rate:
    usd_per_hour: float
    compute: float
    source: str  # "spot", "on-demand", "static"


class Prices:
    """Cached price lookups. ``ec2`` and ``pricing`` clients are optional (static fallback)."""

    def __init__(
        self,
        *,
        region: str,
        ec2: Any | None = None,
        pricing: Any | None = None,
        clock: Any = time.time,
        spot_ttl_s: float = 3600,
        on_demand_ttl_s: float = 86400,
    ) -> None:
        self.region = region
        self.ec2 = ec2
        self.pricing = pricing
        self.clock = clock
        self.spot_ttl_s = spot_ttl_s
        self.on_demand_ttl_s = on_demand_ttl_s
        self._lock = threading.Lock()
        self._spot: dict[tuple[str, str], tuple[float, float | None]] = {}
        self._od: dict[str, tuple[float, float | None]] = {}

    # --- lookups ---------------------------------------------------------------------------

    def spot(self, instance_type: str, az: str) -> float | None:
        key = (instance_type, az)
        with self._lock:
            hit = self._spot.get(key)
        if hit and self.clock() - hit[0] < self.spot_ttl_s:
            return hit[1]
        price = self._fetch_spot(instance_type, az)
        with self._lock:
            self._spot[key] = (self.clock(), price)
        return price

    def on_demand(self, instance_type: str) -> tuple[float, str]:
        with self._lock:
            hit = self._od.get(instance_type)
        if hit and hit[1] is not None and self.clock() - hit[0] < self.on_demand_ttl_s:
            return hit[1], "on-demand"
        if hit and hit[1] is None and self.clock() - hit[0] < FAILED_LOOKUP_TTL_S:
            return scale_static(instance_type), "static"
        price = self._fetch_on_demand(instance_type)
        with self._lock:
            self._od[instance_type] = (self.clock(), price)
        if price is None:
            return scale_static(instance_type), "static"
        return price, "on-demand"

    def compute(self, instance_type: str, az: str, lifecycle: str) -> tuple[float, str]:
        """Compute $/h. Spot falls back to the on-demand price, Spot's ceiling, labelled so."""
        if lifecycle == "spot":
            p = self.spot(instance_type, az) if az else None
            if p is not None:
                return p, "spot"
        return self.on_demand(instance_type)

    def rate(
        self,
        instance_type: str,
        az: str,
        lifecycle: str,
        *,
        root_gib: int = 30,
        throughput_mibps: int = 125,
    ) -> Rate:
        compute, source = self.compute(instance_type, az, lifecycle)
        total = compute + volume_hour(root_gib, throughput_mibps) + IPV4_HOUR
        return Rate(usd_per_hour=total, compute=compute, source=source)

    def max_spot_per_vcpu(self, instance_types: Iterable[str], az: str = "") -> float:
        """The highest known $/vCPU-hour among ``instance_types`` (errs high, for admission)."""
        best = 0.0
        for t in instance_types:
            p, _ = self.compute(t, az, "spot") if az else self.on_demand(t)
            best = max(best, p / max(1, vcpus(t)))
        return best or STATIC_PER_VCPU

    # --- AWS ---------------------------------------------------------------------------------

    def _fetch_spot(self, instance_type: str, az: str) -> float | None:
        if self.ec2 is None:
            return None
        try:
            r = self.ec2.describe_spot_price_history(
                InstanceTypes=[instance_type],
                ProductDescriptions=["Linux/UNIX"],
                AvailabilityZone=az,
                StartTime=self.clock(),
                MaxResults=10,
            )
        except Exception:
            return None
        hist = sorted(r.get("SpotPriceHistory", []), key=lambda h: str(h.get("Timestamp", "")))
        return float(hist[-1]["SpotPrice"]) if hist else None

    def _fetch_on_demand(self, instance_type: str) -> float | None:
        if self.pricing is None:
            return None
        filters = {
            "instanceType": instance_type,
            "regionCode": self.region,
            "operatingSystem": "Linux",
            "tenancy": "Shared",
            "preInstalledSw": "NA",
            "capacitystatus": "Used",
        }
        try:
            r = self.pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()
                ],
                MaxResults=10,
            )
        except Exception:
            return None
        for item in r.get("PriceList", []):
            doc = json.loads(item) if isinstance(item, str) else item
            for term in doc.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if usd and float(usd) > 0:
                        return float(usd)
        return None


# --- accrual -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Accrued:
    usd: float
    instance_hours: float
    by_pool: dict[str, float]
    by_type: dict[str, float]
    sources: frozenset[str]


def accrue(records: Iterable[dict[str, Any]], start: float, end: float, prices: Prices) -> Accrued:
    """Dollars and instance-hours of ledger ``records`` overlapping ``[start, end)``.

    A record covers ``[launched_at, last_seen]``; records still running are extended to ``end``
    only if they were seen within the last 10 minutes (a worker writes every 5).
    """
    usd = hours = 0.0
    by_pool: dict[str, float] = {}
    by_type: dict[str, float] = {}
    sources: set[str] = set()
    for r in records:
        lo = max(start, float(r.get("launched_at", 0)))
        last = float(r.get("last_seen", lo))
        # Records are written every 5 min and read up to 5 min late: 20 min of slack.
        running = not r.get("exit_reason") and end - last < RUNNING_SLACK_S
        hi = min(end, end if running else last)
        if hi <= lo:
            continue
        h = (hi - lo) / 3600
        rate = prices.rate(
            str(r.get("instance_type", "")),
            str(r.get("availability_zone", "")),
            str(r.get("lifecycle", "spot")),
            root_gib=int(r.get("root_gib", 30)),
            throughput_mibps=int(r.get("throughput_mibps", 125)),
        )
        cost = h * rate.usd_per_hour
        usd += cost
        hours += h
        sources.add(rate.source)
        by_pool[str(r.get("pool", ""))] = by_pool.get(str(r.get("pool", "")), 0.0) + cost
        by_type[str(r.get("instance_type", ""))] = (
            by_type.get(str(r.get("instance_type", "")), 0.0) + cost
        )
    return Accrued(usd, hours, by_pool, by_type, frozenset(sources))
