"""Instance metadata (IMDSv2): Spot interruption, rebalance recommendations, ASG lifecycle."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Literal, Protocol

Notice = Literal["interruption", "rebalance", "lifecycle-terminating"]

BASE = "http://169.254.169.254/latest"


class Metadata(Protocol):
    def notice(self) -> Notice | None: ...


class Imds:
    """Polls IMDSv2. Missing endpoints (404) mean "no notice"."""

    def __init__(self, base: str = BASE, timeout_s: float = 2.0) -> None:
        self.base = base
        self.timeout_s = timeout_s
        self._token = ""
        self._token_expires = 0.0

    def _get_token(self) -> str:
        if time.monotonic() < self._token_expires:
            return self._token
        req = urllib.request.Request(
            f"{self.base}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            self._token = r.read().decode()
        self._token_expires = time.monotonic() + 21000
        return self._token

    def get(self, path: str) -> str | None:
        req = urllib.request.Request(
            f"{self.base}/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": self._get_token()},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return str(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def identity(self) -> dict[str, str]:
        """Instance type, AZ and lifecycle ("spot" or "on-demand") for the cost ledger."""
        out = {}
        for key, path in (
            ("instance_type", "instance-type"),
            ("availability_zone", "placement/availability-zone"),
            ("lifecycle", "instance-life-cycle"),
        ):
            try:
                v = self.get(path)
            except Exception:
                v = None
            if v:
                out[key] = v.strip()
        return out

    def notice(self) -> Notice | None:
        if self.get("spot/instance-action") is not None:
            return "interruption"
        state = self.get("autoscaling/target-lifecycle-state")
        if state is not None and state.strip() == "Terminated":
            return "lifecycle-terminating"
        if self.get("events/recommendations/rebalance") is not None:
            return "rebalance"
        return None


class NoMetadata:
    """For running outside EC2 (tests, local runs)."""

    def identity(self) -> dict[str, str]:
        return {}

    def notice(self) -> Notice | None:
        return None
