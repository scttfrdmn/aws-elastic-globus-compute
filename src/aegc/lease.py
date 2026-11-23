"""Deployment lease and TTL with EventBridge Scheduler (no Lambda).

If no endpoint renews a deployment for ``lease_s`` (48 h by default), one-shot schedules
freeze it:

- ``lease-state`` overwrites ``/aegc/<d>/state`` with ``expired`` through the universal target
  ``arn:aws:scheduler:::aws-sdk:ssm:putParameter``. Workers see it, fail anything queued, and
  leave their groups.
- ``lease-<pool>`` sets each group's minimum to 0 through ``autoscaling:updateAutoScalingGroup``,
  so ``min>0`` workers can leave too.

Engines push the lease forward every hour (:func:`renew`). ``--ttl`` adds ``ttl-*`` schedules
that nothing renews: a hard end date. Everything lives in a tagged schedule group ``aegc-<d>``
and runs as the tagged role ``/aegc/aegc-<d>-scheduler-<region>``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aegc import naming
from aegc.awsutil import error_code, is_not_found, paginate
from aegc.infra import Outcome, OwnershipError, Report
from aegc.state import State, write_state

DEFAULT_LEASE_S = 48 * 3600
RENEW_EVERY_S = 3600
MIN_LEASE_S = 3 * 3600  # renewed at least three times per lease, with margin for outages


def renew_interval(lease_s: int) -> float:
    """How often engines renew: hourly, or a third of a short lease."""
    return min(RENEW_EVERY_S, lease_s / 3)


SSM_TARGET = "arn:aws:scheduler:::aws-sdk:ssm:putParameter"
ASG_TARGET = "arn:aws:scheduler:::aws-sdk:autoscaling:updateAutoScalingGroup"
POLICY = "aegc-lease"


def group_name(deployment: str) -> str:
    return f"{naming.PREFIX}-{deployment}"


def role_name(deployment: str, region: str) -> str:
    return f"{naming.PREFIX}-{deployment}-scheduler-{region}"


def at(t: float) -> str:
    return "at(" + datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S") + ")"


def parse_at(expr: str) -> float:
    inner = expr.removeprefix("at(").removesuffix(")")
    return datetime.strptime(inner, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()


def parse_duration(text: str) -> int:
    """``48h``, ``7d``, ``90m``; ``0`` or ``none`` disables."""
    if text in ("0", "none", "off"):
        return 0
    units = {"m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    raise ValueError(f"expected a duration like 48h or 7d, not {text!r}")


def check_lease(lease_s: int) -> int:
    if lease_s and lease_s < MIN_LEASE_S:
        raise ValueError(
            f"a lease shorter than {MIN_LEASE_S // 3600} h could expire between renewals; "
            "use --lease 3h or more (or --ttl for a hard end)"
        )
    return lease_s


@dataclass(frozen=True)
class LeaseSpec:
    deployment: str
    region: str
    account: str
    groups: dict[str, str]  # pool name -> Auto Scaling group name
    #: Lambda pools (#21): pool name -> event source mapping UUID (disabled when the lease ends).
    mappings: dict[str, str] = field(default_factory=dict)
    lease_s: int = DEFAULT_LEASE_S
    ttl_at: float = 0.0  # absolute UTC time; 0: no TTL


def _expired_value(reason: str, by: str) -> str:
    return State(mode="expired", reason=reason, since=0.0, by=by).to_json()


def schedules(spec: LeaseSpec, role_arn: str, *, now: float) -> dict[str, dict[str, Any]]:
    """The schedules a deployment should have: name -> CreateSchedule arguments."""
    d = spec.deployment
    out: dict[str, dict[str, Any]] = {}

    def add(name: str, when: float, target: dict[str, Any]) -> None:
        out[name] = {
            "Name": name,
            "GroupName": group_name(d),
            "ScheduleExpression": at(when),
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "ActionAfterCompletion": "NONE",
            "Description": f"aegc {d}: {name}",
            "Target": {**target, "RoleArn": role_arn, "RetryPolicy": {"MaximumRetryAttempts": 10}},
        }

    def kind(prefix: str, when: float, reason: str) -> None:
        add(
            f"{prefix}-state",
            when,
            {
                "Arn": SSM_TARGET,
                "Input": json.dumps(
                    {
                        "Name": naming.state_parameter(d),
                        "Value": _expired_value(reason, prefix),
                        "Type": "String",
                        "Overwrite": True,
                    }
                ),
            },
        )
        for pool, group in sorted(spec.groups.items()):
            add(
                f"{prefix}-{pool}",
                when,
                {
                    "Arn": ASG_TARGET,
                    "Input": json.dumps({"AutoScalingGroupName": group, "MinSize": 0}),
                },
            )

    if spec.lease_s:
        kind(
            "lease",
            now + spec.lease_s,
            f"no endpoint renewed the lease for {spec.lease_s // 3600} h",
        )
    if spec.ttl_at and spec.ttl_at > now:  # a TTL that has passed already fired (or was resumed)
        kind("ttl", spec.ttl_at, f"TTL reached at {at(spec.ttl_at)[3:-1]} UTC")
    return out


# --- ensure / destroy ----------------------------------------------------------------------------


def _role_documents(spec: LeaseSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    d, r, a = spec.deployment, spec.region, spec.account
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": a}},
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "ssm:PutParameter",
                "Resource": f"arn:aws:ssm:{r}:{a}:parameter{naming.state_parameter(d)}",
            },
            {
                "Effect": "Allow",
                "Action": "autoscaling:UpdateAutoScalingGroup",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {f"autoscaling:ResourceTag/{naming.TAG_DEPLOYMENT}": d}
                },
            },
        ],
    }
    return trust, policy


def ensure_role(iam: Any, spec: LeaseSpec, report: Report, *, dry_run: bool) -> str:
    d, name = spec.deployment, role_name(spec.deployment, spec.region)
    trust, policy = _role_documents(spec)
    try:
        role = iam.get_role(RoleName=name)["Role"]
        tags = naming.tag_dict(iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, tags, d):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
        report.add("iam role", name, "exists")
        arn = str(role["Arn"])
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if dry_run:
            report.add("iam role", name, "would-create")
            return f"arn:aws:iam::{spec.account}:role{naming.IAM_PATH}{name}"
        arn = str(
            iam.create_role(
                RoleName=name,
                Path=naming.IAM_PATH,
                AssumeRolePolicyDocument=json.dumps(trust),
                Tags=naming.tag_list(naming.tags(d, "scheduler")),
            )["Role"]["Arn"]
        )
        report.add("iam role", name, "created")
    if not dry_run:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        iam.put_role_policy(RoleName=name, PolicyName=POLICY, PolicyDocument=json.dumps(policy))
    return arn


def ensure_group(scheduler: Any, deployment: str, report: Report, *, dry_run: bool) -> None:
    d, name = deployment, group_name(deployment)
    try:
        g = scheduler.get_schedule_group(Name=name)
        tags = naming.tag_dict(scheduler.list_tags_for_resource(ResourceArn=g["Arn"])["Tags"])
        if not naming.is_owned(name, tags, d):
            raise OwnershipError(f"schedule group {name} exists but isn't tagged as {d}'s")
        report.add("schedule group", name, "exists")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if dry_run:
            report.add("schedule group", name, "would-create")
            return
        scheduler.create_schedule_group(
            Name=name, Tags=naming.tag_list(naming.tags(d, "schedules"))
        )
        report.add("schedule group", name, "created")


def ensure_lease(
    *,
    scheduler: Any,
    iam: Any,
    spec: LeaseSpec,
    report: Report,
    dry_run: bool,
    now: float | None = None,
) -> None:
    """Create or reset the lease (and TTL) schedules; remove ones no longer wanted."""
    now = time.time() if now is None else now
    if not spec.lease_s and not spec.ttl_at:
        destroy_lease(scheduler=scheduler, iam=iam, deployment=spec.deployment,
                      region=spec.region, report=report, dry_run=dry_run)  # fmt: skip
        return
    arn = ensure_role(iam, spec, report, dry_run=dry_run)
    ensure_group(scheduler, spec.deployment, report, dry_run=dry_run)
    want = schedules(spec, arn, now=now)
    have = (
        set()
        if dry_run and not _group_exists(scheduler, spec.deployment)
        else _names(scheduler, spec.deployment)
    )
    for name, kw in want.items():
        if dry_run:
            report.add(
                "schedule",
                name,
                "would-update" if name in have else "would-create",
                kw["ScheduleExpression"],
            )
            continue
        if name in have:
            _with_role_retry(lambda kw=kw: scheduler.update_schedule(**kw))
            report.add("schedule", name, "updated", kw["ScheduleExpression"])
        else:
            _with_role_retry(lambda kw=kw: scheduler.create_schedule(**kw))
            report.add("schedule", name, "created", kw["ScheduleExpression"])
    for name in sorted(have - set(want)):
        if not dry_run:
            scheduler.delete_schedule(Name=name, GroupName=group_name(spec.deployment))
        report.add("schedule", name, "would-delete" if dry_run else "deleted")


ROLE_RETRIES = 12
ROLE_RETRY_S = 5.0


def _with_role_retry(call: Any, *, sleep: Any = time.sleep) -> Any:
    """A just-created role takes a few seconds before Scheduler can assume it ("The execution
    role you provide must allow AWS EventBridge Scheduler to assume the role")."""
    for attempt in range(ROLE_RETRIES):
        try:
            return call()
        except Exception as e:
            if error_code(e) != "ValidationException" or "assume the role" not in str(e):
                raise
            if attempt == ROLE_RETRIES - 1:
                raise
            sleep(ROLE_RETRY_S)
    return None


def _group_exists(scheduler: Any, deployment: str) -> bool:
    try:
        scheduler.get_schedule_group(Name=group_name(deployment))
        return True
    except Exception as e:
        if is_not_found(e):
            return False
        raise


def _names(scheduler: Any, deployment: str) -> set[str]:
    return {
        str(s["Name"])
        for s in paginate(
            scheduler, "list_schedules", "Schedules", GroupName=group_name(deployment)
        )
    }


def renew(scheduler: Any, deployment: str, lease_s: int, *, now: float | None = None) -> int:
    """Push every ``lease-*`` schedule to now + ``lease_s``. Returns how many were renewed."""
    now = time.time() if now is None else now
    n = 0
    for name in sorted(_names(scheduler, deployment)):
        if not name.startswith("lease-"):
            continue
        s = scheduler.get_schedule(Name=name, GroupName=group_name(deployment))
        kw = {
            k: s[k]
            for k in (
                "Name",
                "GroupName",
                "ScheduleExpressionTimezone",
                "FlexibleTimeWindow",
                "Target",
                "ActionAfterCompletion",
                "Description",
            )
            if k in s
        }
        scheduler.update_schedule(**kw, ScheduleExpression=at(now + lease_s))
        n += 1
    return n


def expiry(scheduler: Any, deployment: str) -> float | None:
    """When the lease expires (earliest ``lease-state``), or None."""
    try:
        s = scheduler.get_schedule(Name="lease-state", GroupName=group_name(deployment))
    except Exception as e:
        if is_not_found(e):
            return None
        raise
    return parse_at(str(s["ScheduleExpression"]))


def destroy_lease(
    *, scheduler: Any, iam: Any, deployment: str, region: str, report: Report, dry_run: bool
) -> None:
    """Delete the deployment's schedules, schedule group and scheduler role (owned only)."""
    d = deployment
    verb: Outcome = "would-delete" if dry_run else "deleted"
    gname = group_name(d)
    if scheduler is not None:
        try:
            g = scheduler.get_schedule_group(Name=gname)
            tags = naming.tag_dict(scheduler.list_tags_for_resource(ResourceArn=g["Arn"])["Tags"])
            if not naming.is_owned(gname, tags, d):
                report.add("schedule group", gname, "kept", "not tagged as aegc's")
            else:
                for name in sorted(_names(scheduler, d)):
                    if not dry_run:
                        scheduler.delete_schedule(Name=name, GroupName=gname)
                    report.add("schedule", name, verb)
                if not dry_run:
                    scheduler.delete_schedule_group(Name=gname)  # also deletes any left
                report.add("schedule group", gname, verb)
        except Exception as e:
            if not is_not_found(e):
                raise
    if iam is None:
        return
    rname = role_name(d, region)
    try:
        tags = naming.tag_dict(iam.list_role_tags(RoleName=rname)["Tags"])
    except Exception as e:
        if is_not_found(e):
            return
        raise
    if not naming.is_owned(rname, tags, d):
        report.add("iam role", rname, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        for p in list(paginate(iam, "list_role_policies", "PolicyNames", RoleName=rname)):
            iam.delete_role_policy(RoleName=rname, PolicyName=p)
        iam.delete_role(RoleName=rname)
    report.add("iam role", rname, verb)


# --- what the schedules do (for tests: substrate stores schedules but never fires them) -----------


def fire(
    ssm: Any, autoscaling: Any, deployment: str, groups: dict[str, str], *, prefix: str = "lease"
) -> None:
    """Do exactly what the ``<prefix>-*`` schedules do when they fire."""
    write_state(
        ssm,
        deployment,
        State.from_json(_expired_value("no endpoint renewed the lease (fired by hand)", prefix)),
    )
    for group in groups.values():
        autoscaling.update_auto_scaling_group(AutoScalingGroupName=group, MinSize=0)
