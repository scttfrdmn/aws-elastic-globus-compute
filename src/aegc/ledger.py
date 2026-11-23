"""Per-instance cost ledger: ``s3://<bucket>/ledger/<d>/<yyyy-mm-dd>/<instance-id>.json``.

Each worker writes its record at boot, every 5 minutes, and when it exits. The record says what
ran (type, AZ, Spot or on-demand, volume) and for how long; :func:`aegc.pricing.accrue` prices it.
The date in the key is the instance's launch date (UTC), so a period's records are found by
listing that period's dates plus the day before (workers live at most about a day).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aegc.awsutil import paginate

PREFIX = "ledger"
WRITE_EVERY_S = 300


@dataclass
class Record:
    instance_id: str
    deployment: str
    pool: str
    instance_type: str
    availability_zone: str
    lifecycle: str  # "spot" or "on-demand"
    vcpus: int
    launched_at: float
    last_seen: float
    root_gib: int = 30
    throughput_mibps: int = 125
    tasks: int = 0
    busy_slot_s: float = 0.0
    exit_reason: str = ""  # set by the final write

    def key(self) -> str:
        return record_key(self.deployment, self.launched_at, self.instance_id)


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")


def record_key(deployment: str, launched_at: float, instance_id: str) -> str:
    return f"{PREFIX}/{deployment}/{_day(launched_at)}/{instance_id}.json"


def write(s3: Any, bucket: str, rec: Record) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=rec.key(),
        Body=json.dumps(asdict(rec), sort_keys=True).encode(),
        ContentType="application/json",
    )


#: An instance lives at most lifetime (<= 72 h) + max walltime (11.5 h) + 15 min, so its record
#: can overlap a period that starts up to 4 days after its launch date.
LOOKBACK_DAYS = 4


def days(start: float, end: float) -> list[str]:
    """Launch dates whose records can overlap ``[start, end)``."""
    d = datetime.fromtimestamp(start, UTC).date() - timedelta(days=LOOKBACK_DAYS)
    last = datetime.fromtimestamp(end, UTC).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def read(
    s3: Any, bucket: str, deployment: str, start: float, end: float
) -> Iterator[dict[str, Any]]:
    """Every record that may overlap ``[start, end)``."""
    for day in days(start, end):
        prefix = f"{PREFIX}/{deployment}/{day}/"
        for obj in paginate(s3, "list_objects_v2", "Contents", Bucket=bucket, Prefix=prefix):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            try:
                yield json.loads(body)
            except ValueError:
                continue
