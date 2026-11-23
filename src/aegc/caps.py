"""Tripping a spend cap: shared by the engine and the cloud-side evaluator (#51)."""

from __future__ import annotations

import logging
import time
from typing import Any

from aegc import lambda_pools
from aegc.spend import Trip
from aegc.state import Limits, State, write_state

log = logging.getLogger("aegc.caps")


def trip(
    *,
    ssm: Any,
    autoscaling: Any,
    deployment: str,
    cap: Trip,
    limits: Limits,
    groups: dict[str, int],
    by: str,
    now: float | None = None,
    lam: Any = None,
    mappings: dict[str, str] | None = None,
) -> tuple[State, Exception | None]:
    """Set the deployment ``capped``, drop group minimums to 0 and disable Lambda pools.

    ``groups`` maps Auto Scaling group names to their current MinSize, ``mappings`` Lambda pools
    to their event source mapping UUIDs (:func:`aegc.lambda_pools.mappings`). Returns the state
    and the error from writing it, if that failed (the caller still stops its own spending).
    """
    st = State(
        mode="capped",
        reason=cap.reason,
        since=time.time() if now is None else now,
        until=cap.until,
        by=by,
        kill=limits.on_cap == "kill",
    )
    error: Exception | None = None
    try:
        write_state(ssm, deployment, st)
    except Exception as e:
        log.error("couldn't write the capped state (workers won't see it; check the policy): %s", e)
        error = e
    log.error("deployment %s capped: %s", deployment, cap.reason)
    # Workers at the group's minimum can't leave it: drop the minimum to 0 too.
    for asg, min_size in groups.items():
        if min_size > 0:
            try:
                autoscaling.update_auto_scaling_group(AutoScalingGroupName=asg, MinSize=0)
            except Exception as e:
                log.warning("%s: couldn't lower the minimum: %s", asg, e)
    if mappings:
        if lam is None:
            log.error("no Lambda client: Lambda pools %s stay enabled", sorted(mappings))
        else:
            for pool, err in lambda_pools.set_all(lam, mappings, False).items():
                log.warning("pool %s: couldn't disable its event source mapping: %s", pool, err)
    return st, error
