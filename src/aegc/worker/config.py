"""Worker configuration, passed by user data as base64 JSON in AEGC_WORKER_CONFIG_B64."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class WorkerConfig:
    deployment: str
    pool: str
    region: str
    task_queue_url: str
    bucket: str
    slots: str = "auto"
    idle_timeout_s: int = 300
    visibility_s: int = 300
    max_deliveries: int = 4
    scratch: str = "/scratch"
    instance_id: str = ""
    aegc_version: str = ""
    state_parameter: str = ""  # /aegc/<d>/state; empty: don't watch the deployment state
    state_poll_s: float = 60.0
    max_lifetime_s: int = 24 * 3600  # stop claiming after this; the watchdog backs it up
    ledger: bool = True  # write the per-instance cost ledger to s3://<bucket>/ledger/<d>/
    instance_type: str = ""  # filled from IMDS at start
    availability_zone: str = ""
    lifecycle: str = ""  # "spot" or "on-demand", from IMDS
    root_volume_gib: int = 30
    throughput_mibps: int = 125
    #: The deployment's data-access grants, ``{"access", "bucket", "prefix"}`` each (#53).
    grants: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "grants", tuple(dict(g) for g in self.grants))

    @property
    def slot_count(self) -> int:
        if self.slots == "auto":
            return max(1, os.cpu_count() or 1)
        n = int(self.slots)
        if n < 1:
            raise ValueError("slots must be >= 1")
        return n

    @property
    def heartbeat_s(self) -> float:
        return self.visibility_s / 3

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkerConfig:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkerConfig:
        env = dict(os.environ if env is None else env)
        raw = env.get("AEGC_WORKER_CONFIG_B64")
        if not raw:
            raise RuntimeError("AEGC_WORKER_CONFIG_B64 is not set")
        d = json.loads(base64.b64decode(raw))
        if env.get("AEGC_INSTANCE_ID"):
            d["instance_id"] = env["AEGC_INSTANCE_ID"]
        if env.get("AWS_REGION"):
            d.setdefault("region", env["AWS_REGION"])
        return cls.from_dict(d)
