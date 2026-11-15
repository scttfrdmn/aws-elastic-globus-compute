"""Operational views and housekeeping: ``aegc status``, ``aegc footprint`` and ``aegc reap``."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from aegc import naming, quotas
from aegc.awsutil import is_not_found, paginate
from aegc.infra import Outcome, Report
from aegc.infra.deployment import _owned_ec2_filters, read_manifest
from aegc.quotas import QuotaCheck

#: Result queues untouched this long, and empty, belong to endpoint processes that are gone.
RESULT_QUEUE_IDLE_S = 14 * 24 * 3600


@dataclass
class PoolStatus:
    name: str
    env: str
    python_minor: str
    queued: int = 0
    running: int = 0
    dead_letters: int = 0
    desired: int = 0
    in_service: int = 0
    max_capacity: int = 0
    unit: str = "vCPUs"
    last_activity: str = ""
    workers: dict[str, int] = field(default_factory=dict)  # "c8g.large spot" -> count
    oldest_task_s: float | None = None
    usd_per_hour: float = 0.0
    exposure_usd: float = 0.0  # worst case for running tasks: walltime x their share of $/h


@dataclass
class DeploymentStatus:
    deployment: str
    region: str
    pools: list[PoolStatus] = field(default_factory=list)
    result_queues: dict[str, int] = field(default_factory=dict)  # name -> waiting results
    quotas: list[QuotaCheck] = field(default_factory=list)
    state: str = "running"
    state_detail: str = ""
    limits: str = "no spend limits"
    spend_today: float | None = None
    spend_month: float | None = None
    usd_per_hour: float = 0.0
    lease_expires: float | None = None
    ttl_expires: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quotas"] = [
            {"kind": q.kind, "quota": q.quota, "used": q.used, "worst_case": q.worst_case,
             "warnings": [*q.errors, *q.warnings]}
            for q in self.quotas
        ]  # fmt: skip
        return d

    def render(self) -> str:
        lines = [f"deployment {self.deployment} ({self.region}): {self.state}"]
        if self.state_detail:
            lines.append(f"  {self.state_detail}")
        spend = f"now ${self.usd_per_hour:.4f}/h"
        if self.spend_today is not None:
            spend += f", today ${self.spend_today:.4f}"
        if self.spend_month is not None:
            spend += f", this month ${self.spend_month:.4f}"
        lines.append(f"  spend: {spend} (estimates) | {self.limits}")
        if self.lease_expires or self.ttl_expires:
            parts = []
            if self.lease_expires:
                parts.append(f"lease until {_utc(self.lease_expires)} (renewed by endpoints)")
            if self.ttl_expires:
                parts.append(f"TTL {_utc(self.ttl_expires)}")
            lines.append("  " + "; ".join(parts))
        for p in self.pools:
            lines.append(
                f"  pool {p.name}: env {p.env}, Python {p.python_minor} | tasks queued {p.queued}, "
                f"running {p.running}, dead-lettered {p.dead_letters} | workers {p.in_service} "
                f"in service, {p.desired} {p.unit} desired, max {p.max_capacity}"
            )
            extra = []
            if p.workers:
                extra.append(", ".join(f"{n}x {k}" for k, n in sorted(p.workers.items())))
            if p.usd_per_hour:
                extra.append(f"${p.usd_per_hour:.4f}/h")
            if p.oldest_task_s is not None and p.queued:
                extra.append(f"oldest task waiting {p.oldest_task_s / 60:.0f} min")
            if p.exposure_usd:
                extra.append(f"running tasks could cost up to ${p.exposure_usd:.2f} more")
            if extra:
                lines.append("    " + " | ".join(extra))
            if p.last_activity:
                lines.append(f"    last scaling activity: {p.last_activity}")
        for q in self.quotas:
            lines.append(q.render())
            lines.extend(f"    warning: {m}" for m in (*q.errors, *q.warnings))
        for name, n in sorted(self.result_queues.items()):
            note = "  (undelivered; if this persists, the endpoint is stopped)" if n else ""
            lines.append(f"  result queue {name}: {n} result(s){note}")
        return "\n".join(lines)


def _counts(sqs: Any, url: str) -> tuple[int, int]:
    a = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(a.get("ApproximateNumberOfMessages", 0)), int(
        a.get("ApproximateNumberOfMessagesNotVisible", 0)
    )


def status(
    *,
    ssm: Any,
    sqs: Any,
    autoscaling: Any,
    deployment: str,
    region: str,
    sq: Any | None = None,
    cw: Any | None = None,
    s3: Any | None = None,
    prices: Any | None = None,
    scheduler: Any | None = None,
    now: float | None = None,
) -> DeploymentStatus:
    from aegc import pricing, protocol
    from aegc.control import describe_limits
    from aegc.state import Limits, read_state

    now = time.time() if now is None else now
    m = read_manifest(ssm, deployment)
    out = DeploymentStatus(deployment=deployment, region=region)
    st = read_state(ssm, deployment, now=now)
    out.state = st.mode
    if not st.running:
        out.state_detail = st.explain(deployment)
    limits = Limits.from_dict(m.get("limits"))
    out.limits = describe_limits(limits)
    for name, pool in sorted(m.get("pools", {}).items()):
        ps = PoolStatus(
            name=name,
            env=str(pool.get("env")),
            python_minor=str(pool.get("python_minor")),
            max_capacity=int(pool.get("max_vcpus", pool.get("max_workers", 0))),
            unit="vCPUs" if pool.get("capacity_type") == "vcpu" else "workers",
        )
        ps.queued, ps.running = _counts(sqs, pool["task_queue_url"])
        ps.dead_letters, _ = _counts(sqs, pool["dlq_url"])
        groups = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[pool["auto_scaling_group"]]
        )["AutoScalingGroups"]
        if groups:
            g = groups[0]
            ps.desired = int(g.get("DesiredCapacity", 0))
            ps.in_service = sum(
                1 for i in g.get("Instances", []) if i.get("LifecycleState") == "InService"
            )
            lifecycle = "spot" if pool.get("spot", True) else "on-demand"
            for i in g.get("Instances", []):
                if i.get("LifecycleState", "").startswith("Terminat"):
                    continue
                key = f"{i.get('InstanceType', '?')} {lifecycle}"
                ps.workers[key] = ps.workers.get(key, 0) + 1
                if prices is not None:
                    ps.usd_per_hour += prices.rate(
                        str(i.get("InstanceType", "")),
                        str(i.get("AvailabilityZone", "")),
                        lifecycle,
                        root_gib=int(pool.get("root_volume_gib", 30)),
                        throughput_mibps=int(pool.get("throughput_mibps", 125)),
                    ).usd_per_hour
            if ps.running and ps.usd_per_hour and ps.in_service:
                slots = max(1, sum(pricing.vcpus(str(i.get("InstanceType", "")))
                                   for i in g.get("Instances", [])))  # fmt: skip
                walltime = int(pool.get("walltime_s") or protocol.DEFAULT_WALLTIME_S)
                ps.exposure_usd = ps.running * walltime / 3600 * ps.usd_per_hour / slots
            acts = autoscaling.describe_scaling_activities(
                AutoScalingGroupName=pool["auto_scaling_group"], MaxRecords=1
            ).get("Activities", [])
            if acts:
                a = acts[0]
                ps.last_activity = f"{a.get('StatusCode', '')}: {a.get('Description', '')}".strip()
        if cw is not None:
            ps.oldest_task_s = _oldest_age(cw, pool["task_queue_url"].rsplit("/", 1)[-1], now)
        out.usd_per_hour += ps.usd_per_hour
        out.pools.append(ps)
    if s3 is not None and prices is not None:
        from aegc.spend import SpendGuard

        guard = SpendGuard(s3=s3, bucket=str(m["bucket"]), deployment=deployment, prices=prices)
        try:
            out.spend_today = round(guard.accrued("day", now).usd, 4)
            out.spend_month = round(guard.accrued("month", now).usd, 4)
        except Exception:
            pass
    if scheduler is not None:
        from aegc import lease

        try:
            out.lease_expires = lease.expiry(scheduler, deployment)
        except Exception:
            out.lease_expires = None
        ttl_at = float((m.get("lease") or {}).get("ttl_at") or 0)
        out.ttl_expires = ttl_at or None
    if sq is not None and cw is not None:
        out.quotas = quotas.check(
            [{"name": n, **p} for n, p in m.get("pools", {}).items()], sq=sq, cw=cw
        )
    for url in _result_queues(sqs, deployment):
        visible, inflight = _counts(sqs, url)
        out.result_queues[url.rsplit("/", 1)[-1]] = visible + inflight
    return out


def _utc(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _oldest_age(cw: Any, queue_name: str, now: float) -> float | None:
    """CloudWatch ApproximateAgeOfOldestMessage (seconds) over the last 10 minutes."""
    try:
        r = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "a",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/SQS",
                            "MetricName": "ApproximateAgeOfOldestMessage",
                            "Dimensions": [{"Name": "QueueName", "Value": queue_name}],
                        },
                        "Period": 60,
                        "Stat": "Maximum",
                    },
                }
            ],
            StartTime=now - 600,
            EndTime=now,
        )
    except Exception:
        return None
    values = [v for res in r.get("MetricDataResults", []) for v in res.get("Values", [])]
    return float(max(values)) if values else None


def _result_queues(sqs: Any, deployment: str) -> list[str]:
    prefix = f"{naming.PREFIX}-{deployment}-res-"
    urls = paginate(sqs, "list_queues", "QueueUrls", page_size=1000, QueueNamePrefix=prefix)
    out = []
    for u in urls:
        try:
            tags = dict(sqs.list_queue_tags(QueueUrl=u).get("Tags", {}))
        except Exception as e:  # listed but already deleted (ListQueues lags)
            if is_not_found(e):
                continue
            raise
        if naming.is_owned(u.rsplit("/", 1)[-1], tags, deployment):
            out.append(u)
    return out


def reap(
    *,
    sqs: Any,
    deployment: str,
    dry_run: bool = True,
    idle_s: float = RESULT_QUEUE_IDLE_S,
    now: float | None = None,
) -> Report:
    """Delete result queues of endpoint processes that are long gone: aegc-owned, empty, idle."""
    now = time.time() if now is None else now
    report = Report()
    verb: Outcome = "would-delete" if dry_run else "deleted"
    for url in _result_queues(sqs, deployment):
        name = url.rsplit("/", 1)[-1]
        a = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "LastModifiedTimestamp",
                "CreatedTimestamp",
            ],
        )["Attributes"]
        waiting = int(a.get("ApproximateNumberOfMessages", 0)) + int(
            a.get("ApproximateNumberOfMessagesNotVisible", 0)
        )
        last = max(int(a.get("LastModifiedTimestamp", 0)), int(a.get("CreatedTimestamp", 0)))
        if waiting:
            report.add("sqs queue", name, "kept", f"{waiting} result(s) waiting")
        elif now - last < idle_s:
            report.add("sqs queue", name, "kept", "recently active")
        else:
            if not dry_run:
                try:
                    sqs.delete_queue(QueueUrl=url)
                except Exception as e:
                    if not is_not_found(e):
                        raise
            report.add("sqs queue", name, verb, "idle result queue")
    return report


# --- footprint (#43) ---------------------------------------------------------------------------


@dataclass
class FootprintItem:
    kind: str
    name: str
    owned: bool  # aegc made it for this deployment (name and tags); else an untagged look-alike
    found_by: set[str] = field(default_factory=set)  # "tags" and/or "name"
    detail: str = ""

    def __str__(self) -> str:
        status = "owned" if self.owned else "look-alike"
        tail = f" ({self.detail})" if self.detail else ""
        return f"{status:>10}  {self.kind:<26} {self.name}{tail}"


@dataclass
class Footprint:
    deployment: str
    region: str
    items: dict[tuple[str, str], FootprintItem] = field(default_factory=dict)

    def add(
        self, kind: str, key: str, name: str, *, owned: bool, found_by: str, detail: str = ""
    ) -> None:
        item = self.items.get((kind, key))
        if item is None:
            self.items[(kind, key)] = FootprintItem(kind, name, owned, {found_by}, detail)
            return
        item.found_by.add(found_by)
        item.owned = item.owned and owned
        if found_by == "name":  # the name scan knows the resource's name, not just its id
            item.name = name
        item.detail = item.detail or detail

    @property
    def owned(self) -> list[FootprintItem]:
        return [i for i in self.items.values() if i.owned]

    @property
    def look_alikes(self) -> list[FootprintItem]:
        return [i for i in self.items.values() if not i.owned]

    def render(self) -> str:
        lines = [f"footprint of {self.deployment} ({self.region}); nothing was changed"]
        for i in sorted(self.items.values(), key=lambda i: (not i.owned, i.kind, i.name)):
            lines.append(f"  {i}")
        lines.append(f"  {len(self.owned)} owned, {len(self.look_alikes)} look-alike(s)")
        return "\n".join(lines)


def _arn_resource(arn: str) -> tuple[str, str]:
    """``(kind, key)`` for a tagging-API ARN, in the same form the name scan uses."""
    parts = arn.split(":", 5)
    service, resource = parts[2], parts[5] if len(parts) > 5 else ""
    cut = min(
        (i for i in (resource.find("/"), resource.find(":")) if i >= 0), default=len(resource)
    )
    rtype, rest = resource[:cut], resource[cut + 1 :]
    if service == "sqs":
        return "sqs queue", resource
    if service == "ssm" and rtype == "parameter":
        return "ssm parameter", "/" + rest
    if service == "logs" and rtype == "log-group":
        return "log group", rest.removesuffix(":*")
    if service == "iam":
        return {"role": "iam role", "instance-profile": "iam instance profile"}.get(
            rtype, f"iam {rtype}"
        ), rest.rsplit("/", 1)[-1]
    if service == "autoscaling":
        return "auto scaling group", resource.rsplit("autoScalingGroupName/", 1)[-1]
    if service == "ec2":
        return {
            "instance": "ec2 instance",
            "volume": "ebs volume",
            "network-interface": "network interface",
            "security-group": "security group",
            "launch-template": "launch template",
        }.get(rtype, f"ec2 {rtype}"), rest
    return f"{service} {rtype}".strip(), rest or resource


_EC2_KINDS = {
    "ec2 instance",
    "ebs volume",
    "network interface",
    "security group",
    "launch template",
}


def _owned_by_name_and_tags(kind: str, name: str, tags: dict[str, str], d: str) -> bool:
    if name == naming.log_group(d) or name.startswith(f"/{naming.PREFIX}/{d}/"):
        return naming.is_owned(f"{naming.PREFIX}-{d}", tags, d)  # path-named: /aegc/<d>[/...]
    if kind in _EC2_KINDS:
        return naming.is_owned(f"{naming.PREFIX}-{d}", tags, d)  # ids carry no name: tags decide
    return naming.is_owned(name, tags, d)


def footprint(
    *,
    tagging: Any,
    sqs: Any,
    autoscaling: Any,
    ec2: Any,
    ssm: Any,
    deployment: str,
    region: str,
) -> Footprint:
    """Everything that exists for ``deployment``. A report only: it changes nothing.

    Two sources: the Resource Groups Tagging API (everything tagged ``aegc:deployment=<d>``), and
    a name-prefix scan of queues, Auto Scaling groups, launch templates and ``/aegc/<d>/``
    parameters, which also finds look-alikes that carry aegc's names but not its tags. Each
    resource is listed as owned (name and tags both match, :func:`naming.is_owned`) or as a
    look-alike.
    """
    d = naming.validate_name("deployment", deployment)
    prefix = f"{naming.PREFIX}-{d}-"
    out = Footprint(deployment=d, region=region)

    for m in paginate(
        tagging,
        "get_resources",
        "ResourceTagMappingList",
        TagFilters=[{"Key": naming.TAG_DEPLOYMENT, "Values": [d]}],
    ):
        kind, key = _arn_resource(m["ResourceARN"])
        tags = naming.tag_dict(m.get("Tags", []))
        out.add(kind, key, key, owned=_owned_by_name_and_tags(kind, key, tags, d), found_by="tags")

    for u in paginate(sqs, "list_queues", "QueueUrls", page_size=1000, QueueNamePrefix=prefix):
        name = u.rsplit("/", 1)[-1]
        try:
            tags = dict(sqs.list_queue_tags(QueueUrl=u).get("Tags", {}))
        except Exception as e:  # listed but already deleted (ListQueues lags)
            if is_not_found(e):
                continue
            raise
        out.add("sqs queue", name, name, owned=naming.is_owned(name, tags, d), found_by="name")

    for g in paginate(autoscaling, "describe_auto_scaling_groups", "AutoScalingGroups"):
        name = g["AutoScalingGroupName"]
        if name.startswith(prefix):
            tags = naming.tag_dict(g.get("Tags", []))
            owned = naming.is_owned(name, tags, d)
            out.add("auto scaling group", name, name, owned=owned, found_by="name")

    # Ownership by tag filter: works where describe calls don't echo tags back.
    tagged = {
        lt["LaunchTemplateId"]
        for lt in paginate(
            ec2, "describe_launch_templates", "LaunchTemplates", Filters=_owned_ec2_filters(d)
        )
    }
    for lt in paginate(ec2, "describe_launch_templates", "LaunchTemplates"):
        name, lid = lt["LaunchTemplateName"], lt["LaunchTemplateId"]
        if name.startswith(prefix):
            out.add("launch template", lid, name, owned=lid in tagged, found_by="name", detail=lid)

    for p in paginate(
        ssm, "get_parameters_by_path", "Parameters", Path=f"/{naming.PREFIX}/{d}", Recursive=True
    ):
        name = str(p["Name"])
        try:
            tags = naming.tag_dict(
                ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
            )
        except Exception as e:
            if not is_not_found(e):
                raise
            tags = {}
        owned = naming.is_owned(f"{naming.PREFIX}-{d}", tags, d)
        out.add("ssm parameter", name, name, owned=owned, found_by="name")
    return out
