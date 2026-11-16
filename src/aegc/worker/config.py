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
    # Isolated pools (#32): tasks run as task_uid with the task role's credentials, and the
    # agent keeps its state (heartbeat, credentials token) in the root-owned state_dir.
    task_role_arn: str = ""
    task_uid: int = 0
    state_dir: str = ""
    size_class: str = ""  # aegc.sizes; empty: one task per vCPU
    task_cpus: int = 1
    task_memory_gib: float = 0
    #: The deployment's data-access grants, ``{"access", "bucket", "prefix"}`` each (#53).
    grants: tuple[dict[str, str], ...] = ()
    #: The worker image, ``<registry>/<repo>@sha256:<digest>`` (user data passes AEGC_IMAGE), for
    #: provenance (#40).
    image: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "grants", tuple(dict(g) for g in self.grants))

    @property
    def slot_count(self) -> int:
        if self.slots == "auto":
            if not self.size_class:
                return max(1, os.cpu_count() or 1)
            from aegc import sizes

            return sizes.slots(
                self.task_cpus, self.task_memory_gib, os.cpu_count() or 1, host_memory_gib()
            )
        n = int(self.slots)
        if n < 1:
            raise ValueError("slots must be >= 1")
        return n

    @property
    def heartbeat_s(self) -> float:
        return self.visibility_s / 3

    @property
    def host_vcpus(self) -> int:
        return os.cpu_count() or 0

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
        if env.get("AEGC_IMAGE"):
            d["image"] = env["AEGC_IMAGE"]
        if env.get("AWS_REGION"):
            d.setdefault("region", env["AWS_REGION"])
        return cls.from_dict(d)


def host_memory_gib() -> float:
    """The host's memory (a container without a memory limit sees all of it)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return 0.0
