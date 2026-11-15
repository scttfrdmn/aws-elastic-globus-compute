"""Deployment state (running, capped, stopped, expired) and spend limits.

The state lives in the SSM parameter ``/aegc/<d>/state``. `aegc deploy` creates it, tagged, as
``running``. Engines write ``capped`` when a spend cap trips. `aegc stop` writes ``stopped``. The
deployment lease (an EventBridge Scheduler target) overwrites it with ``expired`` if no engine
renews the lease. Engines refuse new tasks and stop scaling whenever the state isn't running.
Workers stop claiming. A missing parameter means running.

Limits live in the deployment manifest under ``limits``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from aegc import naming
from aegc.awsutil import is_not_found
from aegc.infra import OwnershipError

MODES = ("running", "capped", "stopped", "expired")

#: Globus Compute result codes for tasks refused or failed because of the state.
FAILURE_CODES = {
    "capped": "AEGC_BUDGET_EXCEEDED",
    "stopped": "AEGC_STOPPED",
    "expired": "AEGC_EXPIRED",
}


@dataclass(frozen=True)
class State:
    mode: str = "running"
    reason: str = ""
    since: float = 0.0
    until: float = 0.0  # capped only: when the cap's period ends (0: until resumed)
    by: str = ""  # who set it: "engine", "aegc stop", "lease", ...
    kill: bool = False  # in-flight tasks are failed now, not finished (stop --hard, on_cap=kill)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown deployment state {self.mode!r}; expected one of {MODES}")

    @property
    def running(self) -> bool:
        return self.mode == "running"

    @property
    def failure_code(self) -> str:
        return FAILURE_CODES.get(self.mode, "")

    def explain(self, deployment: str) -> str:
        """Why tasks are refused, and what to do about it."""
        d = deployment
        if self.mode == "capped":
            until = (
                f" until {_utc(self.until)}" if self.until else " until the cap is raised or reset"
            )
            return (
                f"deployment {d!r} has reached a spend limit ({self.reason}); new tasks are "
                f"refused{until}. Raise the limit with `aegc limits {d} --max-usd-day N` (or "
                f"--max-usd-month), or resume now with `aegc limits {d} --resume`."
            )
        if self.mode == "stopped":
            why = f" ({self.reason})" if self.reason else ""
            return f"deployment {d!r} is stopped{why}; resume it with `aegc resume {d}`."
        if self.mode == "expired":
            return (
                f"deployment {d!r}'s lease expired: no endpoint renewed it ({self.reason}); "
                f"resume it with `aegc resume {d}`."
            )
        return f"deployment {d!r} is running"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> State:
        raw = json.loads(text)
        known = {k: raw[k] for k in ("mode", "reason", "since", "until", "by", "kill") if k in raw}
        return cls(**known)


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M UTC")


def read_state(ssm: Any, deployment: str, *, now: float | None = None) -> State:
    """The current state. A cap whose period has ended reads as running (nothing to write)."""
    try:
        value = ssm.get_parameter(Name=naming.state_parameter(deployment))["Parameter"]["Value"]
    except Exception as e:
        if is_not_found(e):
            return State()
        raise
    st = State.from_json(value)
    now = time.time() if now is None else now
    if st.mode == "capped" and st.until and now >= st.until:
        return State(reason=f"cap period ended at {_utc(st.until)}", since=st.until, by="reset")
    return st


def write_state(
    ssm: Any, deployment: str, state: State, *, extra_tags: dict[str, str] | None = None
) -> None:
    """Create (tagged) or overwrite the state parameter; refuses one aegc didn't create."""
    d, name = deployment, naming.state_parameter(deployment)
    try:
        ssm.get_parameter(Name=name)
    except Exception as e:
        if not is_not_found(e):
            raise
        ssm.put_parameter(
            Name=name,
            Value=state.to_json(),
            Type="String",
            Tags=naming.tag_list(naming.tags(d, "state", extra=extra_tags)),
        )
        return
    found = naming.tag_dict(
        ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
    )
    if not naming.is_owned(f"{naming.PREFIX}-{d}", found, d):
        raise OwnershipError(f"SSM parameter {name} exists but isn't tagged as {d}'s")
    ssm.put_parameter(Name=name, Value=state.to_json(), Type="String", Overwrite=True)


# --- limits ------------------------------------------------------------------------------------

ON_CAP = ("fail", "kill")


@dataclass(frozen=True)
class Limits:
    """Spend limits for a deployment. ``None`` means no limit."""

    max_usd_day: float | None = None
    max_usd_month: float | None = None
    max_instance_hours_day: float | None = None
    on_cap: str = "fail"  # in-flight tasks when a cap trips: finish ("fail") or are killed
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("max_usd_day", "max_usd_month", "max_instance_hours_day"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0, not {v}")
        if self.on_cap not in ON_CAP:
            raise ValueError(f"on_cap must be one of {ON_CAP}, not {self.on_cap!r}")

    @property
    def any(self) -> bool:
        return any(
            v is not None
            for v in (self.max_usd_day, self.max_usd_month, self.max_instance_hours_day)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_usd_day": self.max_usd_day,
            "max_usd_month": self.max_usd_month,
            "max_instance_hours_day": self.max_instance_hours_day,
            "on_cap": self.on_cap,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Limits:
        raw = raw or {}
        return cls(
            max_usd_day=_num(raw.get("max_usd_day")),
            max_usd_month=_num(raw.get("max_usd_month")),
            max_instance_hours_day=_num(raw.get("max_instance_hours_day")),
            on_cap=str(raw.get("on_cap") or "fail"),
        )

    def merged(self, **changes: Any) -> Limits:
        """A copy with ``changes`` applied; a value of ``"none"`` removes that limit."""
        d = self.to_dict()
        for k, v in changes.items():
            if v is None:
                continue
            d[k] = None if v == "none" else v
        return Limits.from_dict(d)


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def period_end(kind: str, now: float) -> float:
    """End of the current UTC day or month."""
    t = datetime.fromtimestamp(now, UTC)
    if kind == "day":
        start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp() + 86400
    nxt = t.replace(year=t.year + (t.month == 12), month=t.month % 12 + 1, day=1)
    return nxt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def period_start(kind: str, now: float) -> float:
    t = datetime.fromtimestamp(now, UTC)
    if kind == "day":
        return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
