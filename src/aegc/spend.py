"""Spend caps: how much a deployment has spent, and how much more capacity it may add.

Admission follows the user's ASBB budget service: before scaling out, hold the projected cost at
1.2 x the expected rate over the next hour (or the rest of the period, if shorter) and admit only
what fits under every cap. A cap that's already reached trips the deployment to ``capped``.

Spend comes from the cost ledger (:mod:`aegc.ledger`) priced by :func:`aegc.pricing.accrue`, the
same numbers ``aegc cost`` shows.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aegc import ledger, pricing
from aegc.state import Limits, period_end, period_start

HOLD = 1.2
HORIZON_S = 3600.0


@dataclass(frozen=True)
class Trip:
    reason: str
    until: float


class SpendGuard:
    def __init__(
        self,
        *,
        s3: Any,
        bucket: str,
        deployment: str,
        prices: pricing.Prices,
        clock: Callable[[], float] = time.time,
        ledger_ttl_s: float = 300.0,
    ) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.deployment = deployment
        self.prices = prices
        self.clock = clock
        self.ledger_ttl_s = ledger_ttl_s
        self._records: list[dict[str, Any]] = []
        self._read_at = -math.inf

    def records(self, now: float) -> list[dict[str, Any]]:
        """This month's ledger records (re-read at most every ``ledger_ttl_s``)."""
        if now - self._read_at >= self.ledger_ttl_s:
            start = period_start("month", now)
            self._records = list(ledger.read(self.s3, self.bucket, self.deployment, start, now))
            self._read_at = now
        return self._records

    def accrued(self, kind: str, now: float) -> pricing.Accrued:
        return pricing.accrue(self.records(now), period_start(kind, now), now, self.prices)

    def check(self, limits: Limits, now: float) -> Trip | None:
        """A trip if any cap is already reached."""
        if limits.max_usd_day is not None:
            a = self.accrued("day", now)
            if a.usd >= limits.max_usd_day:
                return Trip(f"${a.usd:.4f} of ${limits.max_usd_day:g}/day", period_end("day", now))
        if limits.max_usd_month is not None:
            a = self.accrued("month", now)
            if a.usd >= limits.max_usd_month:
                return Trip(
                    f"${a.usd:.2f} of ${limits.max_usd_month:g}/month", period_end("month", now)
                )
        if limits.max_instance_hours_day is not None:
            a = self.accrued("day", now)
            if a.instance_hours >= limits.max_instance_hours_day:
                return Trip(
                    f"{a.instance_hours:.2f} of {limits.max_instance_hours_day:g} "
                    "instance-hours/day",
                    period_end("day", now),
                )
        return None

    def admit(
        self,
        limits: Limits,
        now: float,
        *,
        rate_now: float,
        instances_now: float,
        want_units: int,
        unit_usd_per_hour: float,
        unit_instances: float,
    ) -> int:
        """How many of ``want_units`` more capacity units fit under every cap.

        A unit is an instance or a vCPU (vCPU-weighted groups); ``unit_usd_per_hour`` errs high.
        """
        allowed = want_units
        for kind, cap in (("day", limits.max_usd_day), ("month", limits.max_usd_month)):
            if cap is None:
                continue
            # A full hour even near the end of a period: a shorter horizon would admit a burst
            # whose rate carries into the next period.
            horizon_h = HORIZON_S / 3600
            headroom = cap - self.accrued(kind, now).usd
            extra_rate = headroom / (horizon_h * HOLD) - rate_now
            allowed = min(allowed, _floor_units(extra_rate, unit_usd_per_hour))
        if limits.max_instance_hours_day is not None:
            horizon_h = HORIZON_S / 3600
            headroom_h = limits.max_instance_hours_day - self.accrued("day", now).instance_hours
            extra_instances = headroom_h / (horizon_h * HOLD) - instances_now
            allowed = min(allowed, _floor_units(extra_instances, unit_instances))
        return max(0, allowed)


def _floor_units(budget: float, per_unit: float) -> int:
    if per_unit <= 0:
        return 0 if budget <= 0 else 10**9
    return max(0, math.floor(budget / per_unit))
