"""S3-event triggers (#83): freezing and resuming their EventBridge rules.

A trigger is an EventBridge rule ``aegc-<d>-trigger-<name>`` (S3 ``Object Created`` on one
bucket and key prefix) whose target is a small Lambda function that starts a Globus flow with
the user's stored login. The deployer side (create, delete, destroy) is :mod:`aegc.infra.triggers`.

Freezing a deployment (caps, ``aegc stop``, the lease and TTL) disables every trigger rule;
resuming enables them again, and the evaluator makes the rules follow the state, as it does for
Lambda pools' mappings (:func:`aegc.lambda_pools.reconcile`). Rules are found by name prefix
(``ListRules``); the IAM policies only allow enabling and disabling rules tagged as the
deployment's.
"""

from __future__ import annotations

import time
from typing import Any

from aegc import naming
from aegc.awsutil import error_code, is_not_found

#: DescribeRule/ListRules ``State`` values that deliver events.
ENABLED_STATES = frozenset({"ENABLED", "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"})


def rules(events: Any, deployment: str) -> dict[str, str]:
    """Rule name -> ``State`` for the deployment's trigger rules (by name prefix)."""
    out: dict[str, str] = {}
    kw: dict[str, Any] = {"NamePrefix": naming.trigger_prefix(deployment)}
    while True:
        r = events.list_rules(**kw)
        for rule in r.get("Rules", []):
            out[str(rule["Name"])] = str(rule.get("State") or "")
        if not r.get("NextToken"):
            return out
        kw["NextToken"] = r["NextToken"]


def set_all(
    events: Any, names: list[str] | dict[str, str], enabled: bool, *, sleep: Any = None
) -> dict[str, Exception]:
    """Enable or disable each rule. One failing doesn't stop the rest; returns the errors by
    rule. A concurrent change (ConcurrentModificationException) is retried a few times."""
    sleep = time.sleep if sleep is None else sleep
    errors: dict[str, Exception] = {}
    for name in sorted(names):
        for attempt in range(5):
            try:
                (events.enable_rule if enabled else events.disable_rule)(Name=name)
                errors.pop(name, None)
                break
            except Exception as e:
                errors[name] = e
                if (
                    is_not_found(e)
                    or error_code(e) != "ConcurrentModificationException"
                    or attempt == 4
                ):
                    break
                sleep(1.0)
    return errors


def freeze(events: Any, deployment: str, enabled: bool) -> tuple[list[str], dict[str, Exception]]:
    """Enable or disable all of the deployment's trigger rules: (rules, errors by rule)."""
    names = sorted(rules(events, deployment))
    return names, set_all(events, names, enabled) if names else {}


def reconcile(
    events: Any,
    deployment: str,
    running: bool,
    *,
    confirm: Any = None,
    dry_run: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    """Make each trigger rule follow the deployment state: enabled while it's running, disabled
    otherwise (as :func:`aegc.lambda_pools.reconcile` does for mappings, with the same
    ``confirm``). Returns (rule state by rule, what changed by rule: ``enabled``, ``disabled``
    or the error)."""
    states = rules(events, deployment)
    if running:
        wrong = [n for n, s in states.items() if s == "DISABLED"]
    else:
        wrong = [n for n, s in states.items() if s in ENABLED_STATES]
    if not wrong or (confirm is not None and bool(confirm()) != running):
        return states, {}
    verb = "enabled" if running else "disabled"
    if dry_run:
        return states, dict.fromkeys(wrong, verb)
    errors = set_all(events, wrong, running)
    changes = {n: str(errors[n]) if n in errors else verb for n in wrong}
    for n in wrong:
        if n not in errors:
            states[n] = "ENABLED" if running else "DISABLED"
    if running and confirm is not None and not confirm():
        # Stopped or capped while we enabled: freeze again now, not on the next pass.
        done = [n for n in wrong if n not in errors]
        set_all(events, done, False)
        for n in done:
            states[n], changes[n] = "DISABLED", "disabled"
    return states, changes
