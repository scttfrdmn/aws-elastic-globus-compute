"""Per-deployment and per-pool resources: ``aegc deploy`` and ``aegc destroy``.

Deployment: worker IAM role + instance profile (path /aegc/), a security group with no ingress, the
log group ``/aegc/<d>``, and the manifest ``/aegc/<d>/manifest`` that the engine reads.

Pool: a task queue with a dead-letter queue, a launch template (stock ECS-optimized AL2023 arm64 AMI
via ``resolve:ssm:``, IMDSv2 with hop limit 2, gp3 root), and a Spot Auto Scaling group with
attribute-based Graviton selection, protected from scale-in and with AZRebalance suspended, in the
subnets whose zones offer the pool's instance types. Workers scale the group in themselves.

Lambda pool (kind ``lambda``, :mod:`aegc.lambda_pools`): the task queues, a function role, a
function running the pool's image, and an event source mapping from the task queue.

Every create carries the ownership tags; ``destroy`` deletes only what carries them (see
:func:`aegc.naming.is_owned`) and is a dry run unless asked otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from aegc import __version__, images, lambda_pools, naming, protocol, sizes
from aegc.awsutil import error_code, is_not_found, paginate
from aegc.infra import Outcome, OwnershipError, Report, iam, userdata

AMI_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
DEFAULT_INSTANCE_TYPES = ("c8g.*", "m8g.*", "r8g.*", "c9g.*", "m9g.*", "r9g.*", "c7g.*", "m7g.*")
TASK_VISIBILITY_S = 300
RETENTION_S = 14 * 24 * 3600
MAX_RECEIVE_COUNT = 6  # the worker gives up at 4 deliveries; the DLQ is a backstop
LOG_RETENTION_DAYS = 14
#: gp3 limits aegc accepts. The ECS-optimized AL2023 AMI's snapshot is 30 GiB, so the root can't
#: be smaller. gp3 allows 0.25 MiB/s per provisioned IOPS, so above 750 MiB/s IOPS go up too.
ROOT_GIB_RANGE = (30, 16384)
THROUGHPUT_RANGE = (125, 1000)
BASE_IOPS = 3000
GROUP_METRICS_NOTE = "1-minute group metrics"
KINDS = ("ec2", lambda_pools.KIND)
#: PoolSpec fields only EC2 pools use; Lambda pools keep their defaults.
EC2_ONLY = (
    "min_vcpus", "max_vcpus", "vcpu_min", "vcpu_max", "memory_mib_min", "spot", "slots",
    "allowed_instance_types", "root_volume_gib", "idle_timeout_s", "throughput_mibps",
    "max_lifetime_s", "size_class", "task_cpus", "task_memory_gib", "memory_gib_per_vcpu_min",
)  # fmt: skip
#: PoolSpec fields only Lambda pools use; EC2 pools keep them 0.
LAMBDA_ONLY = ("memory_mib", "timeout_s", "max_concurrency", "ephemeral_mib")
#: AWS recommends a queue visibility of 6x the function's timeout for an SQS mapping, so a
#: throttled delivery isn't redelivered while it waits; the handler shortens each record's own
#: visibility to its invocation's deadline plus a minute once it starts.
LAMBDA_VISIBILITY_FACTOR = 6


@dataclass(frozen=True)
class PoolSpec:
    name: str  # e.g. "sci-py314"
    env: str  # e.g. "geospatial"
    python_minor: str  # e.g. "3.14", read from the image at build time
    image: str  # <registry>/<repo>@sha256:<digest>
    # Capacity is counted in vCPUs (Auto Scaling DesiredCapacityType=vcpu), so a pool's cost
    # bound doesn't depend on which instance sizes price-capacity-optimized picks (#29).
    min_vcpus: int = 0
    max_vcpus: int = 8
    vcpu_min: int = 2
    vcpu_max: int = 8  # <= max_vcpus: the largest worker Auto Scaling may pick
    memory_mib_min: int = 4096
    spot: bool = True
    slots: str = "auto"
    allowed_instance_types: tuple[str, ...] = DEFAULT_INSTANCE_TYPES
    root_volume_gib: int = 30
    idle_timeout_s: int = 300
    throughput_mibps: int = 125  # gp3 root volume; 125 MiB/s is included in the GiB price
    walltime_s: int = 0  # default per-task walltime (0: protocol.DEFAULT_WALLTIME_S)
    max_walltime_s: int = 0  # ceiling for requests (0: protocol.MAX_WALLTIME_S)
    max_lifetime_s: int = 24 * 3600  # a worker stops claiming after this
    # Size class (aegc.sizes): empty for a pool deployed without size=, which runs one task per
    # vCPU as before.
    size_class: str = ""
    task_cpus: int = 1  # vCPUs per task (0: the whole instance)
    task_memory_gib: float = 0  # memory per task (0: not a slot limit)
    memory_gib_per_vcpu_min: float = 0  # InstanceRequirements MemoryGiBPerVCpu.Min (0: unset)
    # Task processes run as uid 57439 without instance-role access, with the task role's
    # credentials (#32). Needs an image built with aegc 0.4 or later.
    isolate_tasks: bool = False
    # Lambda pools (#21, aegc.lambda_pools): "ec2" (an Auto Scaling group) or "lambda" (a
    # function fed by an SQS event source mapping). The fields below apply to Lambda pools only.
    kind: str = "ec2"
    memory_mib: int = 0  # the function's memory (0: lambda_pools.DEFAULT_MEMORY_MIB)
    timeout_s: int = 0  # the function's timeout (0: lambda_pools.DEFAULT_TIMEOUT_S)
    max_concurrency: int = 0  # the mapping's MaximumConcurrency (0: DEFAULT_CONCURRENCY)
    ephemeral_mib: int = 0  # /tmp (0: Lambda's 512)

    @property
    def is_lambda(self) -> bool:
        return self.kind == "lambda"

    def __post_init__(self) -> None:
        naming.validate_name("pool", self.name)
        if self.kind not in KINDS:
            raise ValueError(f"pool kind must be one of {', '.join(KINDS)}, not {self.kind!r}")
        if self.is_lambda:
            self._check_lambda()
            return
        for f in LAMBDA_ONLY:
            if getattr(self, f):
                raise ValueError(f"{f} applies to Lambda pools only (kind=lambda)")
        if self.size_class:
            sizes.get(self.size_class)  # a known class
            if self.slots != "auto":
                raise ValueError(
                    f"size={self.size_class} sets each worker's slots from its vCPUs and memory; "
                    f"drop slots={self.slots}"
                )
        if self.size_class == "whole" and self.vcpu_min != self.vcpu_max:
            # Auto Scaling counts vCPUs: with a range it may meet N tasks' worth of the largest
            # size with many small instances, most of them idle.
            raise ValueError(
                f"size=whole needs one worker size, e.g. vcpu={self.vcpu_max}; "
                f"not vcpu={self.vcpu_min}-{self.vcpu_max}"
            )
        if self.task_cpus and self.vcpu_min < self.task_cpus:
            raise ValueError(
                f"size={self.size_class or 'small'} gives each task {self.task_cpus} vCPUs, so "
                f"workers need at least that many: vcpu={self.vcpu_min}-{self.vcpu_max} is too "
                "small"
            )
        if not 0 <= self.min_vcpus <= self.max_vcpus:
            raise ValueError("need 0 <= min_vcpus <= max_vcpus")
        if not 3600 <= self.max_lifetime_s <= 72 * 3600:
            raise ValueError("lifetime= must be 3600-259200 s (1-72 h)")
        if self.max_vcpus and self.vcpu_max > self.max_vcpus:
            raise ValueError(
                f"vcpu={self.vcpu_min}-{self.vcpu_max} allows workers larger than "
                f"max_vcpus={self.max_vcpus}; Auto Scaling may launch one that size, so lower "
                "the upper vcpu bound or raise max_vcpus"
            )
        if self.max_vcpus and self.max_vcpus < self.vcpu_min:
            raise ValueError(
                f"max_vcpus={self.max_vcpus} is smaller than one worker (vcpu={self.vcpu_min}-...)"
            )
        if self.walltime_s < 0 or self.max_walltime_s < 0:
            raise ValueError("walltime_s and max_walltime_s must be >= 0")
        if self.walltime_s and self.max_walltime_s and self.walltime_s > self.max_walltime_s:
            raise ValueError("walltime_s must not exceed max_walltime_s")
        lo, hi = ROOT_GIB_RANGE
        if not lo <= self.root_volume_gib <= hi:
            raise ValueError(f"root volume (disk=) must be {lo}-{hi} GiB")
        lo, hi = THROUGHPUT_RANGE
        if not lo <= self.throughput_mibps <= hi:
            raise ValueError(f"gp3 throughput (throughput=) must be {lo}-{hi} MiB/s")

    def _check_lambda(self) -> None:
        """Lambda's limits; fills in the defaults (0) so the manifest records what's deployed."""
        defaults = {f.name: f.default for f in fields(self)}
        for f in EC2_ONLY:
            if getattr(self, f) != defaults[f]:
                raise ValueError(f"{f} applies to EC2 pools only, not to Lambda pools")
        if self.isolate_tasks:
            # Lambda gives the process the role's credentials in environment variables, with no
            # uid to drop to: task code always runs with the function role (and its grants).
            raise ValueError(
                "Lambda pools can't isolate tasks: task code runs with the function role's "
                "credentials (the pool's queue, payloads and grants)"
            )

        def put(key: str, value: Any) -> None:
            object.__setattr__(self, key, value)  # frozen

        put("memory_mib", self.memory_mib or lambda_pools.DEFAULT_MEMORY_MIB)
        put("timeout_s", self.timeout_s or lambda_pools.DEFAULT_TIMEOUT_S)
        put("max_concurrency", self.max_concurrency or lambda_pools.DEFAULT_CONCURRENCY)
        lo, hi = lambda_pools.MEMORY_MIB_RANGE
        if not lo <= self.memory_mib <= hi:
            raise ValueError(f"Lambda memory (memory=) must be {lo}-{hi} MiB")
        if not 60 <= self.timeout_s <= lambda_pools.MAX_TIMEOUT_S:
            raise ValueError(f"Lambda timeout (timeout=) must be 60-{lambda_pools.MAX_TIMEOUT_S} s")
        lo, hi = lambda_pools.CONCURRENCY_RANGE
        if not lo <= self.max_concurrency <= hi:
            raise ValueError(f"Lambda concurrency (concurrency=) must be {lo}-{hi}")
        lo, hi = lambda_pools.EPHEMERAL_MIB_RANGE
        if self.ephemeral_mib and not lo <= self.ephemeral_mib <= hi:
            raise ValueError(f"Lambda /tmp (tmp=) must be {lo}-{hi} MiB")
        # Each invocation keeps MARGIN_S for posting the result.
        ceiling = self.timeout_s - lambda_pools.MARGIN_S
        if self.walltime_s < 0 or self.max_walltime_s < 0:
            raise ValueError("walltime_s and max_walltime_s must be >= 0")
        if self.max_walltime_s > ceiling:
            raise ValueError(
                f"max_walltime={self.max_walltime_s} doesn't fit timeout={self.timeout_s} (at "
                f"most {ceiling} s: {lambda_pools.MARGIN_S} s are kept for posting the result)"
            )
        put("max_walltime_s", self.max_walltime_s or ceiling)
        put("walltime_s", self.walltime_s or min(protocol.DEFAULT_WALLTIME_S, self.max_walltime_s))
        if self.walltime_s > self.max_walltime_s:
            raise ValueError(
                f"walltime={self.walltime_s} exceeds max_walltime_s={self.max_walltime_s} "
                f"(timeout={self.timeout_s} less {lambda_pools.MARGIN_S} s)"
            )

    @classmethod
    def from_manifest(cls, name: str, entry: dict[str, Any]) -> PoolSpec:
        """The PoolSpec a manifest pool entry was deployed from (for redeploy merges)."""
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in entry.items() if k in known}
        kw["name"] = name
        if "allowed_instance_types" in kw:
            kw["allowed_instance_types"] = tuple(kw["allowed_instance_types"])
        if "max_vcpus" not in entry and "max_workers" in entry:  # a v0.1 manifest
            # The same bound v0.1 had: that many workers, each up to the largest size.
            kw["max_vcpus"] = int(entry["max_workers"]) * int(entry.get("vcpu_max", 16))
            kw["min_vcpus"] = int(entry.get("min_workers", 0)) * int(entry.get("vcpu_min", 2))
        return cls(**kw)


@dataclass(frozen=True)
class DeploymentSpec:
    deployment: str
    account: str
    region: str
    bucket: str
    pools: tuple[PoolSpec, ...]
    subnet_ids: tuple[str, ...] = ()  # default: the default VPC's default subnets
    extra_tags: dict[str, str] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)  # aegc.state.Limits.to_dict()
    lease_s: int = 48 * 3600  # freeze the deployment if no endpoint renews it (0: no lease)
    ttl_at: float = 0.0  # hard expiry, UTC epoch seconds (0: none)
    grants: tuple[iam.Grant, ...] = ()  # worker access to the user's S3 prefixes (#53)

    def __post_init__(self) -> None:
        naming.validate_name("deployment", self.deployment)
        if len({p.name for p in self.pools}) != len(self.pools):
            raise ValueError("pool names must be unique")
        grants = tuple(
            dict.fromkeys(
                g if isinstance(g, iam.Grant) else iam.Grant.from_dict(g) for g in self.grants
            )
        )
        object.__setattr__(self, "grants", grants)
        for p in self.pools:
            if p.is_lambda:  # Lambda allows 64-character function names, IAM 64 for roles
                for what, name in (
                    ("function", lambda_pools.function_name(self.deployment, p.name)),
                    (
                        "function role",
                        lambda_pools.function_role(self.deployment, p.name, self.region),
                    ),
                ):
                    if len(name) > 64:
                        raise ValueError(
                            f"Lambda pool {p.name}: its {what} name {name} is longer than 64 "
                            "characters; use a shorter deployment or env name"
                        )
        self.grants_policy()  # too many grants fail here, before anything changes

    def worker_policy(self) -> dict[str, Any]:
        return iam.worker_policy(
            deployment=self.deployment, region=self.region, account=self.account, bucket=self.bucket
        )

    def task_policy(self) -> dict[str, Any]:
        return iam.task_policy(deployment=self.deployment, region=self.region, account=self.account)

    def function_policy(self, pool: PoolSpec) -> dict[str, Any]:
        return iam.function_policy(
            deployment=self.deployment, pool=pool.name, env=pool.env, region=self.region,
            account=self.account, bucket=self.bucket,
        )  # fmt: skip

    @property
    def unisolated_pools(self) -> tuple[str, ...]:
        """EC2 pools whose tasks still run with the worker role (images older than aegc 0.4).
        Lambda pools' tasks run with their own function role, which holds the grants."""
        return tuple(p.name for p in self.pools if not p.isolate_tasks and not p.is_lambda)

    def grants_policy(self) -> dict[str, Any] | None:
        """The ``aegc-grants`` document, or None without grants. It goes on the task role, on
        the worker role too while any EC2 pool isn't isolated, and on each Lambda pool's
        function role; it must fit next to any of them."""
        if not self.grants:
            return None
        room = iam.INLINE_POLICY_LIMIT - max(
            iam.policy_size(self.worker_policy()),
            iam.policy_size(self.task_policy()),
            *(iam.policy_size(self.function_policy(p)) for p in self.pools if p.is_lambda),
        )
        return iam.grants_policy(
            self.grants, deployment=self.deployment, budget=room, account=self.account
        )


@dataclass
class Clients:
    iam: Any
    ec2: Any
    autoscaling: Any
    sqs: Any
    logs: Any
    ssm: Any
    ecr: Any = None  # optional: without it, deploy and destroy leave ECR in-use tags alone
    scheduler: Any = None  # optional: without it, deploy and destroy leave the lease alone
    # The cloud endpoint (#22); optional: without them, destroy leaves its secret and alarm.
    secretsmanager: Any = None
    cloudwatch: Any = None
    sns: Any = None
    # Lambda pools (#21); optional: without it, deploy refuses Lambda pools.
    lambda_: Any = None


ACCESS_DENIED = {
    "AccessDenied",
    "AccessDeniedException",
    "AuthorizationError",
    "UnauthorizedOperation",
}


def _retry(fn: Callable[[], Any], *, codes: set[str], attempts: int, delay_s: float) -> Any:
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if error_code(e) not in codes or attempt == attempts - 1:
                raise
            time.sleep(delay_s)
    return None


# --- deployment-level --------------------------------------------------------------------------


def _set_grants(
    c: Clients,
    role: str,
    grants: dict[str, Any] | None,
    n: int,
    report: Report,
    *,
    role_kind: str,
    dry_run: bool,
) -> None:
    label = f"{iam.GRANTS_POLICY} ({role_kind} role)"
    if grants is not None:
        _put_role_policy(
            c, role, iam.GRANTS_POLICY, grants, report, dry_run=dry_run, detail=f"{n} grant(s)",
            label=label,
        )  # fmt: skip
    else:
        _delete_role_policy(c, role, iam.GRANTS_POLICY, report, dry_run=dry_run, label=label)


#: Instance tag on workers of isolated pools (#32).
TAG_ISOLATED = "aegc:isolated"


def _unisolated_instances(c: Clients, d: str) -> list[str]:
    """This deployment's live workers whose tasks still run with the worker role."""
    out = []
    for r in paginate(
        c.ec2, "describe_instances", "Reservations",
        Filters=[{"Name": f"tag:{naming.TAG_DEPLOYMENT}", "Values": [d]},
                 {"Name": "instance-state-name", "Values": ["pending", "running"]}],
    ):  # fmt: skip
        for i in r.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
            if tags.get(naming.TAG_ROLE) == "worker" and tags.get(TAG_ISOLATED) != "true":
                out.append(str(i["InstanceId"]))
    return out


def ensure_task_role(c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool) -> str:
    """The task role (#32): trusted only by the worker role, with the grants and the manifest
    read. Returns its ARN."""
    d, name = spec.deployment, naming.task_role(spec.deployment, spec.region)
    arn = f"arn:aws:iam::{spec.account}:role{naming.IAM_PATH}{name}"
    worker_arn = (
        f"arn:aws:iam::{spec.account}:role{naming.IAM_PATH}"
        f"{naming.worker_role(spec.deployment, spec.region)}"
    )
    trust = json.dumps(iam.task_trust_policy(worker_arn))
    try:
        found = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
        outcome: Outcome = "exists"
        if not dry_run:
            _retry(
                lambda: c.iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust),
                codes={"MalformedPolicyDocument"}, attempts=10, delay_s=3,
            )  # fmt: skip
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        outcome = "would-create" if dry_run else "created"
        if not dry_run:
            # A worker role created seconds ago isn't a valid principal yet everywhere in IAM.
            _retry(
                lambda: c.iam.create_role(
                    Path=naming.IAM_PATH,
                    RoleName=name,
                    AssumeRolePolicyDocument=trust,
                    Description=f"aegc task code for deployment {d} (its grants)",
                    MaxSessionDuration=3600,
                    Tags=naming.tag_list(naming.tags(d, "task-role", extra=spec.extra_tags)),
                ),
                codes={"MalformedPolicyDocument"}, attempts=10, delay_s=3,
            )  # fmt: skip
    report.add("iam role", name, outcome)
    _put_role_policy(c, name, iam.TASK_POLICY, spec.task_policy(), report, dry_run=dry_run)
    _set_grants(
        c, name, spec.grants_policy(), len(spec.grants), report, role_kind="task", dry_run=dry_run
    )
    return arn


def ensure_worker_role(c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool) -> str:
    d, name = spec.deployment, naming.worker_role(spec.deployment, spec.region)
    tags = naming.tag_list(naming.tags(d, "worker-role", extra=spec.extra_tags))
    profile_arn = f"arn:aws:iam::{spec.account}:instance-profile{naming.IAM_PATH}{name}"
    try:
        found = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
        role_outcome: Outcome = "exists"
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        role_outcome = "would-create" if dry_run else "created"
        if not dry_run:
            c.iam.create_role(
                Path=naming.IAM_PATH,
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(iam.ec2_trust_policy()),
                Description=f"aegc worker instances for deployment {d}",
                Tags=tags,
            )
    report.add("iam role", name, role_outcome)
    _put_role_policy(c, name, iam.WORKER_POLICY, spec.worker_policy(), report, dry_run=dry_run)
    # Tasks of isolated pools get grants through the task role; unisolated pools' tasks still
    # run with this role, so it keeps them while there are any.
    old = [] if spec.unisolated_pools else _unisolated_instances(c, d)
    # Pools may all be isolated now while instances launched before still use this role: they
    # keep the deployment's current grants (a change or revoke reaches them too) until they leave.
    grants = spec.grants_policy() if (spec.unisolated_pools or old) else None
    _set_grants(c, name, grants, len(spec.grants), report, role_kind="worker", dry_run=dry_run)
    if old and grants is not None:
        report.add(
            "iam role policy", f"{iam.GRANTS_POLICY} (worker role)", "kept",
            f"for {len(old)} pre-0.4 instance(s) still running; deploy again after they leave",
        )  # fmt: skip
    if dry_run:
        return profile_arn

    try:
        prof = c.iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
        found = naming.tag_dict(c.iam.list_instance_profile_tags(InstanceProfileName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"instance profile {name} exists but isn't tagged as {d}'s")
        report.add("iam instance profile", name, "exists")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        prof = c.iam.create_instance_profile(
            Path=naming.IAM_PATH, InstanceProfileName=name, Tags=tags
        )["InstanceProfile"]
        report.add("iam instance profile", name, "created")
    if name not in [r["RoleName"] for r in prof.get("Roles", [])]:
        c.iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    return str(prof.get("Arn", profile_arn))


def _policy_document(raw: Any) -> Any:
    """An inline policy as GetRolePolicy returns it: a dict (boto3 decodes it), or JSON text,
    URL-encoded as IAM sends it. Unreadable text comes back as is (so it compares unequal)."""
    if isinstance(raw, dict):
        return raw
    text = str(raw)
    for candidate in (text, urllib.parse.unquote(text)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return text


def _put_role_policy(
    c: Clients,
    role: str,
    policy_name: str,
    doc: dict[str, Any],
    report: Report,
    *,
    dry_run: bool,
    detail: str = "",
    label: str = "",
) -> Outcome:
    """Put an inline role policy only if it's missing or different, and report which.

    An unchanged redeploy reports ``exists``; a dry run reports what a deploy would do.
    """
    try:
        raw = c.iam.get_role_policy(RoleName=role, PolicyName=policy_name)["PolicyDocument"]
        current = _policy_document(raw)
    except Exception as e:
        if not is_not_found(e):  # also the role itself, in a dry run before it exists
            raise
        current = None
    outcome: Outcome
    if current == json.loads(json.dumps(doc)):
        outcome = "exists"
    elif current is None:
        outcome = "would-create" if dry_run else "created"
    else:
        outcome = "would-update" if dry_run else "updated"
    if outcome in ("created", "updated"):
        c.iam.put_role_policy(RoleName=role, PolicyName=policy_name, PolicyDocument=json.dumps(doc))
    report.add("iam role policy", label or policy_name, outcome, detail)
    return outcome


def _delete_role_policy(
    c: Clients, role: str, policy_name: str, report: Report, *, dry_run: bool, label: str = ""
) -> Outcome | None:
    """Delete an inline role policy if it's there (reported); None if there was none."""
    try:
        c.iam.get_role_policy(RoleName=role, PolicyName=policy_name)
    except Exception as e:
        if not is_not_found(e):
            raise
        return None
    outcome: Outcome = "would-delete" if dry_run else "deleted"
    if not dry_run:
        try:
            c.iam.delete_role_policy(RoleName=role, PolicyName=policy_name)
        except Exception as e:
            if not is_not_found(e):
                raise
            return None
    report.add("iam role policy", label or policy_name, outcome)
    return outcome


def default_subnets(ec2: Any) -> tuple[str, list[str]]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("no default VPC in this region; pass subnet ids explicitly")
    vpc = vpcs[0]["VpcId"]
    subnets = paginate(
        ec2,
        "describe_subnets",
        "Subnets",
        Filters=[
            {"Name": "vpc-id", "Values": [vpc]},
            {"Name": "default-for-az", "Values": ["true"]},
        ],
    )
    return vpc, sorted(s["SubnetId"] for s in subnets)


def resolve_network(ec2: Any, spec: DeploymentSpec) -> tuple[str, list[str]]:
    if not spec.subnet_ids:
        return default_subnets(ec2)
    subnets = ec2.describe_subnets(SubnetIds=list(spec.subnet_ids))["Subnets"]
    vpcs = {s["VpcId"] for s in subnets}
    if len(vpcs) != 1:
        raise ValueError("all subnets must be in one VPC")
    return vpcs.pop(), sorted(s["SubnetId"] for s in subnets)


@dataclass
class OfferingCache:
    """One deploy's answers from EC2 about subnets' zones and the zones' instance types."""

    zones: dict[str, tuple[str, str]] = field(default_factory=dict)  # subnet -> (name, id)
    offered: dict[tuple[str, ...], set[str] | None] = field(default_factory=dict)


def subnet_zones(ec2: Any, subnets: list[str], cache: OfferingCache) -> dict[str, tuple[str, str]]:
    """Each subnet's Availability Zone, as (ZoneName, ZoneId); the id may be empty."""
    missing = [s for s in subnets if s not in cache.zones]
    if missing:
        for s in ec2.describe_subnets(SubnetIds=missing)["Subnets"]:
            cache.zones[s["SubnetId"]] = (s["AvailabilityZone"], s.get("AvailabilityZoneId", ""))
    return {s: cache.zones[s] for s in subnets if s in cache.zones}


def offered_zones(ec2: Any, patterns: tuple[str, ...], cache: OfferingCache) -> set[str] | None:
    """The zone names that offer at least one instance type matching ``patterns`` (globs, as in
    AllowedInstanceTypes). None if EC2 won't say, e.g. to a deployer policy from before 0.6."""
    key = tuple(sorted(patterns))
    if key not in cache.offered:
        try:
            cache.offered[key] = {
                str(o["Location"])
                for o in paginate(
                    ec2, "describe_instance_type_offerings", "InstanceTypeOfferings",
                    LocationType="availability-zone",
                    Filters=[{"Name": "instance-type", "Values": list(key)}],
                )
            }  # fmt: skip
        except Exception as e:
            if not error_code(e):
                raise
            cache.offered[key] = None
    return cache.offered[key]


def pool_subnets(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    subnets: list[str],
    report: Report,
    cache: OfferingCache,
) -> list[str]:
    """The subnets a pool's group uses: those in zones that offer one of its instance types.

    Not every zone has Graviton (us-east-1's use1-az3 has none), and a group with such a subnet
    can fail to place or keep reporting launch errors. Default subnets in such zones are left
    out; explicit ones (``subnet_ids``) are kept, with a warning. If no zone offers any of the
    types, or EC2 won't say, every subnet is kept and Auto Scaling reports what it can't launch.
    """
    patterns = ", ".join(pool.allowed_instance_types)
    offered = offered_zones(c.ec2, pool.allowed_instance_types, cache)
    if offered is None:
        report.add(
            "subnet offerings", pool.name, "kept",
            "all subnets: DescribeInstanceTypeOfferings failed (deployer policy before 0.6?)",
        )  # fmt: skip
        return subnets
    if not offered:
        report.add("subnet offerings", pool.name, "kept", f"all subnets: no zone offers {patterns}")
        return subnets
    zones = subnet_zones(c.ec2, subnets, cache)
    keep = []
    for s in subnets:
        name, zid = zones.get(s, ("", ""))
        if not name or name in offered:
            keep.append(s)
            continue
        where = f"{s} ({name}{f', {zid}' if zid else ''})"
        why = f"pool {pool.name}: the zone offers none of {patterns}"
        if spec.subnet_ids:  # the user chose it: use it, but say so
            keep.append(s)
            report.add("subnet", where, "kept", why)
        else:
            report.add("subnet", where, "skipped", why)
    return keep


def _owned_ec2_filters(d: str) -> list[dict[str, Any]]:
    # Ownership via tag filters: equally valid on AWS, and works where tags aren't echoed back.
    return [
        {"Name": f"tag:{naming.TAG_MANAGED}", "Values": ["true"]},
        {"Name": f"tag:{naming.TAG_DEPLOYMENT}", "Values": [d]},
    ]


def ensure_security_group(
    c: Clients, spec: DeploymentSpec, vpc: str, report: Report, *, dry_run: bool
) -> str:
    d, name = spec.deployment, naming.security_group(spec.deployment)
    by_name = list(
        paginate(
            c.ec2,
            "describe_security_groups",
            "SecurityGroups",
            Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc]}],
        )
    )
    if by_name:
        owned = c.ec2.describe_security_groups(
            Filters=[
                {"Name": "group-id", "Values": [by_name[0]["GroupId"]]},
                *_owned_ec2_filters(d),
            ]
        )["SecurityGroups"]
        if not owned:
            raise OwnershipError(f"security group {name} exists but isn't tagged as {d}'s")
        report.add("security group", name, "exists", by_name[0]["GroupId"])
        return str(by_name[0]["GroupId"])
    if dry_run:
        report.add("security group", name, "would-create")
        return "sg-dry-run"
    gid = c.ec2.create_security_group(
        GroupName=name,
        Description=f"aegc workers for {d}: no ingress",
        VpcId=vpc,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": naming.tag_list(naming.tags(d, "sg", extra=spec.extra_tags)),
            }
        ],
    )["GroupId"]
    report.add("security group", name, "created", gid)
    return str(gid)


def ensure_log_group(c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool) -> None:
    d, name = spec.deployment, naming.log_group(spec.deployment)
    groups = paginate(c.logs, "describe_log_groups", "logGroups", logGroupNamePrefix=name)
    match = [g for g in groups if g["logGroupName"] == name]
    if match:
        arn = match[0]["arn"].removesuffix(":*")
        if not naming.is_owned(
            f"{naming.PREFIX}-{d}", c.logs.list_tags_for_resource(resourceArn=arn)["tags"], d
        ):
            raise OwnershipError(f"log group {name} exists but isn't tagged as {d}'s")
        report.add("log group", name, "exists")
    elif dry_run:
        report.add("log group", name, "would-create")
        return
    else:
        c.logs.create_log_group(
            logGroupName=name, tags=naming.tags(d, "logs", extra=spec.extra_tags)
        )
        report.add("log group", name, "created")
    c.logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)


# --- pool-level --------------------------------------------------------------------------------


def _queue_tags(c: Clients, url: str) -> dict[str, str]:
    """A queue's tags; {} if it's gone. ListQueues keeps returning a deleted queue for up to a
    minute, so a listed queue can already be gone."""
    try:
        return dict(c.sqs.list_queue_tags(QueueUrl=url).get("Tags", {}))
    except Exception as e:
        if is_not_found(e):
            return {}
        raise


def ensure_queue(
    c: Clients,
    name: str,
    tags: dict[str, str],
    attributes: dict[str, str],
    deployment: str,
    report: Report,
    *,
    dry_run: bool,
) -> str:
    try:
        url = c.sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except Exception as e:
        if not is_not_found(e):
            raise
        if dry_run:
            report.add("sqs queue", name, "would-create")
            return f"https://sqs.dry-run/{name}"
        url = c.sqs.create_queue(QueueName=name, Attributes=attributes, tags=tags)["QueueUrl"]
        report.add("sqs queue", name, "created")
        return str(url)
    if not naming.is_owned(name, _queue_tags(c, url), deployment):
        raise OwnershipError(f"queue {name} exists but isn't tagged as {deployment}'s")
    if not dry_run:
        c.sqs.set_queue_attributes(QueueUrl=url, Attributes=attributes)
    report.add("sqs queue", name, "exists")
    return str(url)


def task_visibility_s(pool: PoolSpec) -> int:
    """The task queue's visibility timeout. An SQS event source mapping needs at least the
    function's timeout (AWS recommends 6x; SQS allows 12 h at most)."""
    if pool.is_lambda:
        return min(43_200, max(TASK_VISIBILITY_S, pool.timeout_s * LAMBDA_VISIBILITY_FACTOR))
    return TASK_VISIBILITY_S


def ensure_pool_queues(
    c: Clients, spec: DeploymentSpec, pool: PoolSpec, report: Report, *, dry_run: bool
) -> tuple[str, str]:
    d = spec.deployment
    base = {
        "MessageRetentionPeriod": str(RETENTION_S),
        "SqsManagedSseEnabled": "true",
    }
    dlq_url = ensure_queue(
        c,
        naming.dead_letter_queue(d, pool.name),
        naming.tags(d, "dlq", pool=pool.name, extra=spec.extra_tags),
        base,
        d,
        report,
        dry_run=dry_run,
    )
    if dry_run and "dry-run" in dlq_url:
        dlq_arn = (
            f"arn:aws:sqs:{spec.region}:{spec.account}:{naming.dead_letter_queue(d, pool.name)}"
        )
    else:
        dlq_arn = c.sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
    task_url = ensure_queue(
        c,
        naming.task_queue(d, pool.name),
        naming.tags(d, "task-queue", pool=pool.name, extra=spec.extra_tags),
        {
            **base,
            "VisibilityTimeout": str(task_visibility_s(pool)),
            "ReceiveMessageWaitTimeSeconds": "20",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(MAX_RECEIVE_COUNT)}
            ),
        },
        d,
        report,
        dry_run=dry_run,
    )
    return task_url, dlq_url


def worker_config(spec: DeploymentSpec, pool: PoolSpec, task_queue_url: str) -> dict[str, Any]:
    return {
        "deployment": spec.deployment,
        "pool": pool.name,
        "region": spec.region,
        "task_queue_url": task_queue_url,
        "bucket": spec.bucket,
        "slots": pool.slots,
        "idle_timeout_s": pool.idle_timeout_s,
        "visibility_s": task_visibility_s(pool),
        "max_deliveries": 4,
        "aegc_version": __version__,
        "state_parameter": naming.state_parameter(spec.deployment),
        "root_volume_gib": pool.root_volume_gib,
        "throughput_mibps": pool.throughput_mibps,
        "max_lifetime_s": pool.max_lifetime_s,
        # Only for isolated pools (#32), so other pools' user data doesn't change.
        **(
            {
                "task_role_arn": f"arn:aws:iam::{spec.account}:role{naming.IAM_PATH}"
                f"{naming.task_role(spec.deployment, spec.region)}",
                "task_uid": userdata.DEFAULT_WORKER_UID,
                "state_dir": userdata.STATE_DIR,
            }
            if pool.isolate_tasks
            else {}
        ),
        # Only for sized pools, so other pools' user data (and launch templates) don't change.
        **(
            {
                "size_class": pool.size_class,
                "task_cpus": pool.task_cpus,
                "task_memory_gib": pool.task_memory_gib,
            }
            if pool.size_class
            else {}
        ),
        # Only with grants, so the launch templates of deployments without any don't change.
        **({"grants": [g.to_dict() for g in spec.grants]} if spec.grants else {}),
    }


def resolve_ami(ssm: Any) -> str:
    """The current stock ECS-optimized AL2023 arm64 AMI.

    Resolved at deploy time: Auto Scaling rejects ``resolve:ssm:`` image ids in launch templates
    used with attribute-based instance selection ("expecting ami-..."). Redeploying picks up a
    newer AMI as a new launch template version.
    """
    return str(ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"])


def launch_template_data(
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    ami: str,
    sg: str,
    profile_arn: str,
    task_queue_url: str,
) -> dict[str, Any]:
    d = spec.deployment
    boot = userdata.WorkerBoot(
        deployment=d,
        pool=pool.name,
        image=pool.image,
        log_group=naming.log_group(d),
        worker_config=worker_config(spec, pool, task_queue_url),
        isolate=pool.isolate_tasks,
        max_age_s=pool.max_lifetime_s + (pool.max_walltime_s or protocol.MAX_WALLTIME_S) + 900,
    )
    worker_tags = naming.tags(d, "worker", pool=pool.name, extra=spec.extra_tags)
    if pool.isolate_tasks:
        worker_tags[TAG_ISOLATED] = "true"
    instance_tags = naming.tag_list(worker_tags)
    return {
        "ImageId": ami,
        "IamInstanceProfile": {"Arn": profile_arn},
        "SecurityGroupIds": [sg],
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 2,
            "HttpEndpoint": "enabled",
            "HttpProtocolIpv6": "disabled",  # tasks' IMDS block covers IPv4; keep IPv6 off
            "InstanceMetadataTags": "enabled",
        },
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": pool.root_volume_gib,
                    "VolumeType": "gp3",
                    "Throughput": pool.throughput_mibps,
                    "Iops": max(BASE_IOPS, 4 * pool.throughput_mibps),
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "UserData": userdata.encode(userdata.render(boot)),
        "TagSpecifications": [
            {"ResourceType": rt, "Tags": instance_tags}
            for rt in ("instance", "volume", "network-interface")
        ],
    }


def template_digest(data: dict[str, Any]) -> str:
    """A short content hash of the launch template data aegc asked for.

    Recorded in each version's description. Comparing read-back data is unreliable: AWS normalizes
    and adds defaults, and emulators may not round-trip every field (substrate#1471).
    """
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def _lt_description(digest: str) -> str:
    return f"aegc {__version__} sha256:{digest}"


def ensure_launch_template(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec | None,
    data: dict[str, Any],
    report: Report,
    *,
    dry_run: bool,
) -> str:
    """A pool's launch template (or, with ``pool=None``, the cloud endpoint host's)."""
    pool_name = pool.name if pool is not None else naming.ENDPOINT
    d, name = spec.deployment, naming.launch_template(spec.deployment, pool_name)
    digest = template_digest(data)
    try:
        found = c.ec2.describe_launch_templates(LaunchTemplateNames=[name])["LaunchTemplates"]
    except Exception as e:
        if not is_not_found(e):
            raise
        found = []
    if not found:
        if dry_run:
            report.add("launch template", name, "would-create")
            return name
        c.ec2.create_launch_template(
            LaunchTemplateName=name,
            VersionDescription=_lt_description(digest),
            LaunchTemplateData=data,
            TagSpecifications=[
                {
                    "ResourceType": "launch-template",
                    "Tags": naming.tag_list(
                        naming.tags(d, "lt", pool=pool_name, extra=spec.extra_tags)
                    ),
                }
            ],
        )
        report.add("launch template", name, "created")
        return name
    owned = c.ec2.describe_launch_templates(
        Filters=[{"Name": "launch-template-name", "Values": [name]}, *_owned_ec2_filters(d)]
    )["LaunchTemplates"]
    if not owned:
        raise OwnershipError(f"launch template {name} exists but isn't tagged as {d}'s")
    current = c.ec2.describe_launch_template_versions(
        LaunchTemplateName=name, Versions=["$Default"]
    )["LaunchTemplateVersions"][0]
    if current.get("VersionDescription", "").endswith(f"sha256:{digest}"):
        report.add("launch template", name, "exists", f"version {current.get('VersionNumber')}")
        return name
    if dry_run:
        report.add("launch template", name, "updated", "would add a version")
        return name
    v = c.ec2.create_launch_template_version(
        LaunchTemplateName=name,
        VersionDescription=_lt_description(digest),
        LaunchTemplateData=data,
    )["LaunchTemplateVersion"]["VersionNumber"]
    c.ec2.modify_launch_template(LaunchTemplateName=name, DefaultVersion=str(v))
    report.add("launch template", name, "updated", f"version {v}")
    return name


def _asg_request(
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    lt_name: str,
    subnets: list[str],
    halted: bool = False,
) -> dict[str, Any]:
    d = spec.deployment
    return {
        # A stopped, capped or expired deployment keeps minimum 0 (resume restores it), or a
        # redeploy would launch workers that can't take tasks and can't leave.
        "MinSize": 0 if halted else pool.min_vcpus,
        "MaxSize": pool.max_vcpus,
        "DesiredCapacityType": "vcpu",
        "VPCZoneIdentifier": ",".join(subnets),
        "CapacityRebalance": pool.spot,
        "NewInstancesProtectedFromScaleIn": True,
        "HealthCheckType": "EC2",
        "MixedInstancesPolicy": {
            "LaunchTemplate": {
                "LaunchTemplateSpecification": {
                    "LaunchTemplateName": lt_name,
                    "Version": "$Default",
                },
                "Overrides": [
                    {
                        "InstanceRequirements": {
                            "VCpuCount": {"Min": pool.vcpu_min, "Max": pool.vcpu_max},
                            "MemoryMiB": {"Min": pool.memory_mib_min},
                            # Only when set: pools without a size class keep their template.
                            **(
                                {"MemoryGiBPerVCpu": {"Min": pool.memory_gib_per_vcpu_min}}
                                if pool.memory_gib_per_vcpu_min
                                else {}
                            ),
                            "CpuManufacturers": ["amazon-web-services"],
                            "BurstablePerformance": "excluded",
                            "AllowedInstanceTypes": list(pool.allowed_instance_types),
                        }
                    }
                ],
            },
            "InstancesDistribution": {
                "OnDemandBaseCapacity": 0,
                "OnDemandPercentageAboveBaseCapacity": 0 if pool.spot else 100,
                "SpotAllocationStrategy": "price-capacity-optimized",
            },
        },
        "_tags": naming.tags(d, "asg", pool=pool.name, extra=spec.extra_tags),
    }


def ensure_auto_scaling_group(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    lt_name: str,
    subnets: list[str],
    report: Report,
    dry_run: bool,
    halted: bool = False,
) -> str:
    d, name = spec.deployment, naming.auto_scaling_group(spec.deployment, pool.name)
    req = _asg_request(spec, pool, lt_name=lt_name, subnets=subnets, halted=halted)
    tags = req.pop("_tags")
    groups = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
        "AutoScalingGroups"
    ]
    asg_tags = [
        {
            "Key": k,
            "Value": v,
            # Instances keep the launch template's aegc:role=worker.
            "PropagateAtLaunch": k != naming.TAG_ROLE,
            "ResourceId": name,
            "ResourceType": "auto-scaling-group",
        }
        for k, v in tags.items()
    ]
    if not groups:
        if dry_run:
            report.add("auto scaling group", name, "would-create")
            return name
        c.autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=name,
            DesiredCapacity=req["MinSize"],
            Tags=asg_tags,
            **req,
        )
        outcome: Outcome = "created"
    else:
        if not naming.is_owned(name, naming.tag_dict(groups[0].get("Tags", [])), d):
            raise OwnershipError(f"auto scaling group {name} exists but isn't tagged as {d}'s")
        if dry_run:
            report.add("auto scaling group", name, "exists")
            if not groups[0].get("EnabledMetrics"):
                report.add("auto scaling group metrics", name, "would-update", GROUP_METRICS_NOTE)
            return name
        # Desired capacity is the engine's and the workers' to manage; don't reset it.
        c.autoscaling.update_auto_scaling_group(AutoScalingGroupName=name, **req)
        c.autoscaling.create_or_update_tags(Tags=asg_tags)
        outcome = "exists"
    # Rebalancing would terminate instances that are running tasks.
    c.autoscaling.suspend_processes(AutoScalingGroupName=name, ScalingProcesses=["AZRebalance"])
    report.add("auto scaling group", name, outcome)
    if not (groups and groups[0].get("EnabledMetrics")):
        enable_group_metrics(c, name, report)
    return name


def enable_group_metrics(c: Clients, name: str, report: Report) -> bool:
    """Turn on the group's Auto Scaling metrics, every minute (free), for ``aegc metrics`` (#39).

    Best effort: a deployer policy from before 0.4 lacks ``EnableMetricsCollection``, and that
    shouldn't stop a deploy. Returns whether it worked.
    """
    try:
        c.autoscaling.enable_metrics_collection(AutoScalingGroupName=name, Granularity="1Minute")
    except Exception as e:
        report.add("auto scaling group metrics", name, "kept", f"not enabled: {error_code(e) or e}")
        return False
    report.add("auto scaling group metrics", name, "updated", GROUP_METRICS_NOTE)
    return True


# --- Lambda pools (#21) ------------------------------------------------------------------------

#: The function's command. The image's entrypoint stays: conda images' ``_entrypoint.sh``
#: activates the environment, and pip images have ``ENTRYPOINT []``.
LAMBDA_COMMAND = ("aegc-worker", "lambda")
#: Lambda allows 4 KB of environment variables per function, names and values together.
LAMBDA_ENV_LIMIT = 4096
LAMBDA_DEFAULT_TMP_MIB = 512
#: How long deploy waits for a function to settle: Lambda optimizes the image after each change.
FUNCTION_WAIT_S = 900.0
FUNCTION_POLL_S = 5.0
#: A new role takes seconds before Lambda can assume it (and before its policy applies).
ROLE_RETRIES = 10
ROLE_RETRY_S = 3.0
#: Mapping states that mean it's on its way to delivering (or already is).
_MAPPING_ON = {"Enabled", "Enabling", "Creating", "Updating"}


def function_role_arn(spec: DeploymentSpec, pool: PoolSpec) -> str:
    name = lambda_pools.function_role(spec.deployment, pool.name, spec.region)
    return f"arn:aws:iam::{spec.account}:role{naming.IAM_PATH}{name}"


def function_arn(spec: DeploymentSpec, pool: PoolSpec) -> str:
    name = lambda_pools.function_name(spec.deployment, pool.name)
    return f"arn:aws:lambda:{spec.region}:{spec.account}:function:{name}"


def task_queue_arn(spec: DeploymentSpec, pool: PoolSpec) -> str:
    name = naming.task_queue(spec.deployment, pool.name)
    return f"arn:aws:sqs:{spec.region}:{spec.account}:{name}"


def function_environment(
    spec: DeploymentSpec, pool: PoolSpec, task_queue_url: str
) -> dict[str, str]:
    """``AEGC_WORKER_CONFIG_B64`` (the worker config, as user data passes it) and ``AEGC_IMAGE``."""
    cfg = json.dumps(
        worker_config(spec, pool, task_queue_url), sort_keys=True, separators=(",", ":")
    )
    env = {
        "AEGC_WORKER_CONFIG_B64": base64.b64encode(cfg.encode()).decode(),
        "AEGC_IMAGE": pool.image,
    }
    if (size := sum(len(k) + len(v) for k, v in env.items())) > LAMBDA_ENV_LIMIT:
        raise ValueError(
            f"Lambda pool {pool.name}: its worker config needs {size} bytes of environment "
            f"variables, and Lambda allows {LAMBDA_ENV_LIMIT}; use fewer grants "
            f"({len(spec.grants)} now)"
        )
    return env


def function_config(
    spec: DeploymentSpec, pool: PoolSpec, *, role_arn: str, task_queue_url: str
) -> dict[str, Any]:
    """The function's settings: the UpdateFunctionConfiguration request (CreateFunction's too)."""
    return {
        "Role": role_arn,
        "Description": f"aegc Lambda pool {pool.name} of deployment {spec.deployment}",
        "MemorySize": pool.memory_mib,
        "Timeout": pool.timeout_s,
        "EphemeralStorage": {"Size": pool.ephemeral_mib or LAMBDA_DEFAULT_TMP_MIB},
        "ImageConfig": {"Command": list(LAMBDA_COMMAND)},
        "Environment": {"Variables": function_environment(spec, pool, task_queue_url)},
        "LoggingConfig": {"LogFormat": "Text", "LogGroup": naming.log_group(spec.deployment)},
    }


def _config_changes(current: dict[str, Any], want: dict[str, Any]) -> list[str]:
    """The settings in ``want`` that GetFunction's ``Configuration`` differs on."""
    logging_ = current.get("LoggingConfig") or {}
    got = {
        "Role": current.get("Role"),
        "Description": current.get("Description", ""),
        "MemorySize": current.get("MemorySize"),
        "Timeout": current.get("Timeout"),
        "EphemeralStorage": current.get("EphemeralStorage"),
        "ImageConfig": (current.get("ImageConfigResponse") or {}).get("ImageConfig"),
        "Environment": {"Variables": (current.get("Environment") or {}).get("Variables", {})},
        "LoggingConfig": {k: logging_.get(k) for k in ("LogFormat", "LogGroup")},
    }
    return [k for k in want if got.get(k) != want[k]]


def check_lambda_pools(c: Clients, spec: DeploymentSpec) -> None:
    """What would stop a Lambda pool's deploy halfway: raises before anything changes."""
    lam = [p for p in spec.pools if p.is_lambda]
    if lam and c.lambda_ is None:
        raise ValueError(f"pool(s) {', '.join(p.name for p in lam)} need a Lambda client")
    for p in lam:
        url = f"https://sqs.{spec.region}.amazonaws.com/{spec.account}/"
        function_environment(spec, p, url + naming.task_queue(spec.deployment, p.name))
        if c.ecr is None:
            continue
        # ECR reports the compressed size; Lambda's limit is uncompressed, which only pulling
        # the layers would tell. An image over the limit compressed is certainly too large; one
        # under it may still fail CreateFunction or UpdateFunctionCode, which say so.
        size = images.image_size(c.ecr, p.image)
        if size is not None and size > lambda_pools.MAX_IMAGE_BYTES:
            raise ValueError(
                f"Lambda pool {p.name}: image {p.image} is {size / 2**30:.1f} GiB compressed; "
                f"Lambda allows {lambda_pools.MAX_IMAGE_BYTES / 2**30:.0f} GiB uncompressed. "
                "Use an EC2 pool for this env"
            )


def _retry_new_role(fn: Callable[[], Any]) -> Any:
    """``fn``, retried while Lambda can't use a role created moments ago yet."""
    for attempt in range(ROLE_RETRIES):
        try:
            return fn()
        except Exception as e:
            if (
                error_code(e) != "InvalidParameterValueException"
                or "role" not in str(e).lower()
                or attempt == ROLE_RETRIES - 1
            ):
                raise
            time.sleep(ROLE_RETRY_S)
    return None


def _wait_function(lam: Any, name: str) -> dict[str, Any]:
    """Wait until the function isn't Pending or mid-update (bounded); its configuration."""
    deadline = time.monotonic() + FUNCTION_WAIT_S
    while True:
        cfg = dict(lam.get_function(FunctionName=name)["Configuration"])
        state = cfg.get("State", "Active")
        update = cfg.get("LastUpdateStatus", "Successful")
        if state == "Failed" or update == "Failed":
            why = cfg.get("StateReason") or cfg.get("LastUpdateStatusReason") or "no reason given"
            raise RuntimeError(f"Lambda function {name} failed: {why}")
        if state != "Pending" and update != "InProgress":
            return cfg
        if time.monotonic() > deadline:
            raise TimeoutError(f"Lambda function {name} still {state}/{update} after "
                               f"{FUNCTION_WAIT_S:.0f} s")  # fmt: skip
        time.sleep(FUNCTION_POLL_S)


def ensure_function_role(
    c: Clients, spec: DeploymentSpec, pool: PoolSpec, report: Report, *, dry_run: bool
) -> str:
    """The pool's function role (path /aegc/, trusted by Lambda), with its own policy and the
    deployment's grants. Returns its ARN."""
    d = spec.deployment
    name = lambda_pools.function_role(d, pool.name, spec.region)
    try:
        found = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
        outcome: Outcome = "exists"
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        outcome = "would-create" if dry_run else "created"
        if not dry_run:
            c.iam.create_role(
                Path=naming.IAM_PATH,
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(iam.lambda_trust_policy()),
                Description=f"aegc Lambda pool {pool.name} of deployment {d}",
                Tags=naming.tag_list(
                    naming.tags(d, "function-role", pool=pool.name, extra=spec.extra_tags)
                ),
            )
    report.add("iam role", name, outcome)
    _put_role_policy(
        c, name, iam.FUNCTION_POLICY, spec.function_policy(pool), report, dry_run=dry_run,
        label=f"{iam.FUNCTION_POLICY} ({pool.name})",
    )  # fmt: skip
    _set_grants(
        c, name, spec.grants_policy(), len(spec.grants), report,
        role_kind=f"{pool.name} function", dry_run=dry_run,
    )  # fmt: skip
    return function_role_arn(spec, pool)


def _function(lam: Any, name: str) -> dict[str, Any] | None:
    try:
        return dict(lam.get_function(FunctionName=name))
    except Exception as e:
        if is_not_found(e):
            return None
        raise


def ensure_function(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    role_arn: str,
    task_queue_url: str,
    report: Report,
    dry_run: bool,
) -> str:
    """The pool's function: created, or updated in place (never replaced). Returns its ARN."""
    d, lam = spec.deployment, c.lambda_
    name = lambda_pools.function_name(d, pool.name)
    want = function_config(spec, pool, role_arn=role_arn, task_queue_url=task_queue_url)
    got = _function(lam, name)
    if got is None:
        if dry_run:
            report.add("lambda function", name, "would-create")
            return function_arn(spec, pool)
        made = _retry_new_role(
            lambda: lam.create_function(
                FunctionName=name,
                PackageType="Image",
                Code={"ImageUri": pool.image},
                Architectures=["arm64"],
                Publish=False,
                Tags=naming.tags(d, "function", pool=pool.name, extra=spec.extra_tags),
                **want,
            )
        )
        _wait_function(lam, name)
        report.add("lambda function", name, "created")
        return str(made["FunctionArn"])
    cfg = dict(got["Configuration"])
    arn = str(cfg["FunctionArn"])
    if not naming.is_owned(name, naming.tag_dict(lam.list_tags(Resource=arn).get("Tags")), d):
        raise OwnershipError(f"Lambda function {name} exists but isn't tagged as {d}'s")
    new_image = (got.get("Code") or {}).get("ImageUri") != pool.image
    new_arch = cfg.get("Architectures", ["arm64"]) != ["arm64"]
    changes = (["image"] if new_image or new_arch else []) + _config_changes(cfg, want)
    if not changes:
        report.add("lambda function", name, "exists")
        return arn
    detail = ", ".join(changes)
    if dry_run:
        report.add("lambda function", name, "would-update", detail)
        return arn
    _wait_function(lam, name)
    if new_image or new_arch:
        lam.update_function_code(FunctionName=name, ImageUri=pool.image, Architectures=["arm64"])
        _wait_function(lam, name)
    if changes != ["image"]:
        _retry_new_role(lambda: lam.update_function_configuration(FunctionName=name, **want))
        _wait_function(lam, name)
    report.add("lambda function", name, "updated", detail)
    return arn


def _mapping_arn(m: dict[str, Any], function_arn_: str) -> str:
    """substrate#1659: no EventSourceMappingArn; it's built from the function's region and
    account, as AWS forms it."""
    if m.get("EventSourceMappingArn"):
        return str(m["EventSourceMappingArn"])
    region, account = function_arn_.split(":")[3:5]
    return f"arn:aws:lambda:{region}:{account}:event-source-mapping:{m['UUID']}"


def _mapping_owned(lam: Any, m: dict[str, Any], d: str) -> bool:
    """aegc's mapping for ``d``: its function has aegc's name, and it carries the tags."""
    fn_arn = str(m.get("FunctionArn", ""))
    function = fn_arn.rsplit(":", 1)[-1]
    try:
        tags = naming.tag_dict(lam.list_tags(Resource=_mapping_arn(m, fn_arn)).get("Tags"))
    except Exception as e:
        if is_not_found(e):
            return False
        raise
    return naming.is_owned(function, tags, d)


def ensure_mapping(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    report: Report,
    dry_run: bool,
    halted: bool,
) -> str:
    """The event source mapping from the pool's task queue to its function. Returns its UUID.

    A halted deployment (stopped, capped or expired) gets it disabled; resume enables it. A
    redeploy never enables it: a running deployment's disabled mapping is reported, as kept.
    """
    d, lam = spec.deployment, c.lambda_
    name = lambda_pools.function_name(d, pool.name)
    queue = task_queue_arn(spec, pool)
    try:
        found = list(
            paginate(lam, "list_event_source_mappings", "EventSourceMappings",
                     EventSourceArn=queue, FunctionName=name)
        )  # fmt: skip
    except Exception as e:
        if not is_not_found(e):  # the function doesn't exist yet (a dry run)
            raise
        found = []
    found = [m for m in found if m.get("State") != "Deleting"]
    want = {
        "BatchSize": 1,
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
        "ScalingConfig": {"MaximumConcurrency": pool.max_concurrency},
    }
    label = f"{name} <- {naming.task_queue(d, pool.name)}"
    if not found:
        if dry_run:
            report.add("lambda event source mapping", label, "would-create")
            return ""
        m = _retry_new_role(
            lambda: lam.create_event_source_mapping(
                EventSourceArn=queue,
                FunctionName=name,
                Enabled=not halted,
                Tags=naming.tags(d, "mapping", pool=pool.name, extra=spec.extra_tags),
                **want,
            )
        )
        report.add("lambda event source mapping", label, "created", "disabled" if halted else "")
        return str(m["UUID"])
    for m in found:
        if not _mapping_owned(lam, m, d):
            raise OwnershipError(
                f"event source mapping {m['UUID']} ({label}) exists but isn't tagged as {d}'s"
            )
    m = found[0]
    uuid = str(m["UUID"])
    got = {
        "BatchSize": m.get("BatchSize"),
        "FunctionResponseTypes": m.get("FunctionResponseTypes", []),
        "ScalingConfig": {
            "MaximumConcurrency": (m.get("ScalingConfig") or {}).get("MaximumConcurrency")
        },
    }
    on = m.get("State") in _MAPPING_ON
    changes = [k for k in want if got[k] != want[k]]
    if halted and on:
        changes.append("disable")
    if not changes:
        if on or halted:
            report.add("lambda event source mapping", label, "exists", uuid)
        else:
            report.add("lambda event source mapping", label, "kept",
                       f"{uuid} is disabled; engines and the evaluator enable it while the "
                       "deployment runs")  # fmt: skip
        return uuid
    detail = ", ".join(changes)
    if dry_run:
        report.add("lambda event source mapping", label, "would-update", detail)
        return uuid
    _retry(
        lambda: lam.update_event_source_mapping(
            UUID=uuid, **want, **({"Enabled": False} if halted else {})
        ),
        codes={"ResourceInUseException"}, attempts=10, delay_s=3,
    )  # fmt: skip
    report.add("lambda event source mapping", label, "updated", detail)
    return uuid


def ensure_lambda_pool(
    c: Clients,
    spec: DeploymentSpec,
    pool: PoolSpec,
    *,
    task_queue_url: str,
    report: Report,
    dry_run: bool,
    halted: bool,
) -> dict[str, str]:
    """A Lambda pool's function role, function and mapping: its manifest entry's resources."""
    if c.ecr is not None:
        images.allow_lambda_pulls(
            c.ecr, pool.env, region=spec.region, account=spec.account, report=report,
            dry_run=dry_run,
        )  # fmt: skip
    role_arn = ensure_function_role(c, spec, pool, report, dry_run=dry_run)
    fn_arn = ensure_function(
        c, spec, pool, role_arn=role_arn, task_queue_url=task_queue_url, report=report,
        dry_run=dry_run,
    )  # fmt: skip
    uuid = ensure_mapping(c, spec, pool, report=report, dry_run=dry_run, halted=halted)
    return {
        "function_name": lambda_pools.function_name(spec.deployment, pool.name),
        "function_arn": fn_arn,
        "event_source_mapping": uuid,
        "function_role_arn": role_arn,
        # The engine accepts this pool's results only from this role (SQS SenderId, #80).
        "function_role_id": role_id(
            c, lambda_pools.function_role(spec.deployment, pool.name, spec.region)
        ),
    }


def role_id(c: Clients, name: str) -> str:
    """A role's unique id (``AROA...``), which SQS reports as a message's sender; empty if the
    role isn't there (a dry run)."""
    try:
        return str(c.iam.get_role(RoleName=name)["Role"]["RoleId"])
    except Exception as e:
        if is_not_found(e) or error_code(e) == "NoSuchEntity":
            return ""
        raise


def _owned_functions(lam: Any, d: str) -> list[dict[str, Any]]:
    """The deployment's functions: aegc's name prefix and its tags."""
    prefix = f"{naming.PREFIX}-{d}-"
    out = []
    for f in paginate(lam, "list_functions", "Functions"):
        name = str(f["FunctionName"])
        if not name.startswith(prefix):
            continue
        try:
            tags = naming.tag_dict(lam.list_tags(Resource=f["FunctionArn"]).get("Tags"))
        except Exception as e:
            if is_not_found(e):
                continue
            raise
        if naming.is_owned(name, tags, d):
            out.append(dict(f))
    return out


def _owned_mappings(lam: Any, d: str) -> list[dict[str, Any]]:
    """The deployment's event source mappings (also those whose function is already gone)."""
    prefix = f"{naming.PREFIX}-{d}-"
    return [
        dict(m)
        for m in paginate(lam, "list_event_source_mappings", "EventSourceMappings")
        if str(m.get("FunctionArn", "")).rsplit(":", 1)[-1].startswith(prefix)
        and m.get("State") != "Deleting"  # DeleteEventSourceMapping completes asynchronously
        and _mapping_owned(lam, m, d)
    ]


def _function_roles(c: Clients, d: str, region: str) -> list[str]:
    """Function roles of ``d`` (tagged ``aegc:role=function-role``), found under /aegc/."""
    out = []
    for r in paginate(c.iam, "list_roles", "Roles", PathPrefix=naming.IAM_PATH):
        name = str(r["RoleName"])
        if not (name.startswith(f"{naming.PREFIX}-{d}-") and name.endswith(f"-{region}")):
            continue
        try:
            tags = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        except Exception as e:
            if is_not_found(e):
                continue
            raise
        if naming.is_owned(name, tags, d) and tags.get(naming.TAG_ROLE) == "function-role":
            out.append(name)
    return out


def _delete_mapping(lam: Any, m: dict[str, Any], report: Report, *, dry_run: bool) -> None:
    label = f"{m['UUID']} ({str(m.get('FunctionArn', '')).rsplit(':', 1)[-1]})"
    if not dry_run:
        try:
            _retry(
                lambda: lam.delete_event_source_mapping(UUID=m["UUID"]),
                codes={"ResourceInUseException"}, attempts=10, delay_s=3,
            )  # fmt: skip
        except Exception as e:
            if not is_not_found(e):
                raise
    report.add("lambda event source mapping", label, "would-delete" if dry_run else "deleted")


def _delete_function(lam: Any, name: str, report: Report, *, dry_run: bool) -> None:
    if not dry_run:
        try:
            lam.delete_function(FunctionName=name)
        except Exception as e:
            if not is_not_found(e):
                raise
    report.add("lambda function", name, "would-delete" if dry_run else "deleted")


def destroy_lambda(c: Clients, d: str, region: str, report: Report, *, dry_run: bool) -> None:
    """The deployment's mappings, functions and function roles, each ownership-checked."""
    lam = c.lambda_
    functions = _owned_functions(lam, d)
    roles = {str(f.get("Role", "")).rsplit("/", 1)[-1] for f in functions} - {""}
    try:
        roles |= set(_function_roles(c, d, region))
    except Exception as e:
        if error_code(e) not in ACCESS_DENIED:
            raise
    for m in _owned_mappings(lam, d):
        _delete_mapping(lam, m, report, dry_run=dry_run)
    for f in functions:
        _delete_function(lam, str(f["FunctionName"]), report, dry_run=dry_run)
    for role in sorted(roles):
        _destroy_role(c, d, role, report, dry_run=dry_run)


def _prune_lambda(
    c: Clients, d: str, entry: dict[str, Any], report: Report, *, dry_run: bool
) -> None:
    lam = c.lambda_
    name = str(entry.get("function_name", ""))
    uuid = str(entry.get("event_source_mapping", ""))
    if uuid:
        try:
            m = dict(lam.get_event_source_mapping(UUID=uuid))
        except Exception as e:
            if not is_not_found(e):
                raise
            m = {}
        if m and m.get("State") != "Deleting":
            if _mapping_owned(lam, m, d):
                _delete_mapping(lam, m, report, dry_run=dry_run)
            else:
                report.add("lambda event source mapping", uuid, "kept", "not tagged as aegc's")
    if name and (got := _function(lam, name)) is not None:
        arn = got["Configuration"]["FunctionArn"]
        if naming.is_owned(name, naming.tag_dict(lam.list_tags(Resource=arn).get("Tags")), d):
            _delete_function(lam, name, report, dry_run=dry_run)
        else:
            report.add("lambda function", name, "kept", "not tagged as aegc's")
    role = str(entry.get("function_role_arn", "")).rsplit("/", 1)[-1]
    if role:
        _destroy_role(c, d, role, report, dry_run=dry_run)


def lambda_leftovers(c: Clients, d: str, region: str) -> list[tuple[str, str]]:
    """``(kind, name)`` of the deployment's mappings, functions and function roles still there."""
    lam = c.lambda_
    left = [("lambda event source mapping", str(m["UUID"])) for m in _owned_mappings(lam, d)]
    left += [("lambda function", str(f["FunctionName"])) for f in _owned_functions(lam, d)]
    left += [("iam role", r) for r in _function_roles(c, d, region)]
    return left


def lambda_footprint(lam: Any, fp: Any, d: str) -> None:
    """Add the deployment's functions and mappings to an :class:`aegc.ops.Footprint`, with
    look-alikes (aegc's names without its tags). The tagging API finds mappings by id only, so
    their ownership is decided here (name of the function and the mapping's tags)."""
    prefix = f"{naming.PREFIX}-{d}-"
    for f in paginate(lam, "list_functions", "Functions"):
        name = str(f["FunctionName"])
        if not name.startswith(prefix):
            continue
        try:
            tags = naming.tag_dict(lam.list_tags(Resource=f["FunctionArn"]).get("Tags"))
        except Exception as e:
            if is_not_found(e):
                continue
            raise
        fp.add("lambda function", name, name, owned=naming.is_owned(name, tags, d),
               found_by="name")  # fmt: skip
    for m in paginate(lam, "list_event_source_mappings", "EventSourceMappings"):
        function = str(m.get("FunctionArn", "")).rsplit(":", 1)[-1]
        if not function.startswith(prefix) or m.get("State") == "Deleting":
            continue
        uuid, owned = str(m["UUID"]), _mapping_owned(lam, m, d)
        kind = "lambda event-source-mapping"  # as aegc.ops names tagging-API mapping ARNs
        fp.add(kind, uuid, uuid, owned=owned, found_by="name", detail=function)
        fp.items[(kind, uuid)].owned = owned  # the name scan decides, not the uuid


# --- manifest ----------------------------------------------------------------------------------


def manifest(
    spec: DeploymentSpec,
    pools: dict[str, dict[str, str]],
    *,
    profile_arn: str,
    sg: str,
    subnets: list[str],
    worker_role_id: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 1,
        "aegc_version": __version__,
        "deployment": spec.deployment,
        "region": spec.region,
        "account": spec.account,
        "bucket": spec.bucket,
        "instance_profile_arn": profile_arn,
        # EC2 pools' results come from the worker role (SQS SenderId, #80).
        "worker_role_id": worker_role_id,
        "security_group": sg,
        "subnets": subnets,
        "pools": {
            p.name: {
                **{k: v for k, v in asdict(p).items() if k != "allowed_instance_types"},
                "allowed_instance_types": list(p.allowed_instance_types),
                **pools[p.name],
            }
            for p in spec.pools
        },
        "limits": dict(spec.limits),
        "lease": {"lease_s": spec.lease_s, "ttl_at": spec.ttl_at},
        "grants": [g.to_dict() for g in spec.grants],
    }
    body["generation"] = manifest_generation(body)
    return body


def manifest_generation(body: dict[str, Any]) -> str:
    """A short hash of the manifest's content: engines reload when it changes."""
    content = {k: v for k, v in body.items() if k != "generation"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:12]


def write_manifest(
    c: Clients, spec: DeploymentSpec, body: dict[str, Any], report: Report, *, dry_run: bool
) -> None:
    d, name = spec.deployment, naming.deployment_manifest_parameter(spec.deployment)
    value = json.dumps(body, sort_keys=True)
    try:
        c.ssm.get_parameter(Name=name)
        found = naming.tag_dict(
            c.ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
        )
        if not naming.is_owned(f"{naming.PREFIX}-{d}", found, d):
            raise OwnershipError(f"SSM parameter {name} exists but isn't tagged as {d}'s")
        if not dry_run:
            c.ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        report.add("ssm parameter", name, "updated")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if dry_run:
            report.add("ssm parameter", name, "would-create")
            return
        c.ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Tags=naming.tag_list(naming.tags(d, "manifest", extra=spec.extra_tags)),
        )
        report.add("ssm parameter", name, "created")


def update_manifest(ssm: Any, deployment: str, body: dict[str, Any]) -> None:
    """Overwrite an existing, aegc-owned manifest (e.g. new limits)."""
    d, name = deployment, naming.deployment_manifest_parameter(deployment)
    found = naming.tag_dict(
        ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
    )
    if not naming.is_owned(f"{naming.PREFIX}-{d}", found, d):
        raise OwnershipError(f"SSM parameter {name} exists but isn't tagged as {d}'s")
    ssm.put_parameter(
        Name=name, Value=json.dumps(body, sort_keys=True), Type="String", Overwrite=True
    )


def pool_tasks(c: Clients, entry: dict[str, Any]) -> int:
    """Queued plus running tasks in a pool's task queue (0 if it's gone)."""
    url = str(entry.get("task_queue_url", ""))
    if not url:
        return 0
    try:
        a = c.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
    except Exception as e:
        if is_not_found(e):
            return 0
        raise
    return int(a.get("ApproximateNumberOfMessages", 0)) + int(
        a.get("ApproximateNumberOfMessagesNotVisible", 0)
    )


def prune_pool(
    c: Clients,
    deployment: str,
    name: str,
    entry: dict[str, Any],
    report: Report,
    *,
    dry_run: bool,
    force: bool = False,
    wait_s: float = 600,
    poll_s: float = 10,
) -> None:
    """Remove a pool a redeploy no longer lists: its group and launch template (a Lambda pool:
    its mapping, function and function role) and its queues.

    Refuses while its task queue has queued or running tasks, unless ``force``.
    """
    d = deployment
    verb: Outcome = "would-delete" if dry_run else "deleted"
    if not force and (n := pool_tasks(c, entry)):
        raise RuntimeError(f"pool {name} has {n} queued or running task(s); use --force")
    if lambda_pools.is_lambda(entry):
        if c.lambda_ is None:
            raise ValueError(f"pool {name} is a Lambda pool: pruning it needs a Lambda client")
        _prune_lambda(c, d, entry, report, dry_run=dry_run)
    group = str(entry.get("auto_scaling_group", ""))
    found = (
        c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group])[
            "AutoScalingGroups"
        ]
        if group
        else []
    )
    if found:
        if not naming.is_owned(group, naming.tag_dict(found[0].get("Tags", [])), d):
            report.add("auto scaling group", group, "kept", "not tagged as aegc's")
        else:
            if not dry_run:
                c.autoscaling.delete_auto_scaling_group(
                    AutoScalingGroupName=group, ForceDelete=True
                )
                _wait_gone(c, group, wait_s=wait_s, poll_s=poll_s)
            report.add("auto scaling group", group, verb)
    lt = str(entry.get("launch_template", ""))
    if lt:
        owned = c.ec2.describe_launch_templates(
            Filters=[{"Name": "launch-template-name", "Values": [lt]}, *_owned_ec2_filters(d)]
        )["LaunchTemplates"]
        if owned:
            if not dry_run:
                c.ec2.delete_launch_template(LaunchTemplateName=lt)
            report.add("launch template", lt, verb)
    for key in ("task_queue_url", "dlq_url"):
        q = str(entry.get(key, ""))
        if q and naming.is_owned(q.rsplit("/", 1)[-1], _queue_tags(c, q), d):
            if not dry_run:
                c.sqs.delete_queue(QueueUrl=q)
            report.add("sqs queue", q.rsplit("/", 1)[-1], verb)


def read_manifest(ssm: Any, deployment: str) -> dict[str, Any]:
    value = ssm.get_parameter(Name=naming.deployment_manifest_parameter(deployment))["Parameter"][
        "Value"
    ]
    return dict(json.loads(value))


# --- deploy ------------------------------------------------------------------------------------


def deploy(c: Clients, spec: DeploymentSpec, *, dry_run: bool = False) -> Report:
    """Create or reconcile a deployment and its pools. Safe to run repeatedly."""
    from aegc import state

    report = Report()
    halted = not state.read_state(c.ssm, spec.deployment).running
    if halted:
        report.add(
            "ssm parameter",
            naming.state_parameter(spec.deployment),
            "kept",
            "not running: group minimums stay 0 and Lambda mappings disabled until `aegc resume`",
        )
    check_lambda_pools(c, spec)
    inuse = _protect_images(c, spec, report, dry_run=dry_run)
    profile_arn = ensure_worker_role(c, spec, report, dry_run=dry_run)
    ensure_task_role(c, spec, report, dry_run=dry_run)
    vpc, subnets = resolve_network(c.ec2, spec)
    if not subnets:
        raise RuntimeError(f"no subnets found in {vpc}")
    sg = ensure_security_group(c, spec, vpc, report, dry_run=dry_run)
    ensure_log_group(c, spec, report, dry_run=dry_run)
    ami = resolve_ami(c.ssm)
    offerings = OfferingCache()
    pools: dict[str, dict[str, str]] = {}
    for pool in spec.pools:
        task_url, dlq_url = ensure_pool_queues(c, spec, pool, report, dry_run=dry_run)
        if pool.is_lambda:
            pools[pool.name] = {
                "task_queue_url": task_url,
                "dlq_url": dlq_url,
                **ensure_lambda_pool(
                    c, spec, pool, task_queue_url=task_url, report=report, dry_run=dry_run,
                    halted=halted,
                ),
            }  # fmt: skip
            continue
        data = launch_template_data(
            spec, pool, ami=ami, sg=sg, profile_arn=profile_arn, task_queue_url=task_url
        )
        lt = ensure_launch_template(c, spec, pool, data, report, dry_run=dry_run)
        usable = pool_subnets(c, spec, pool, subnets, report, offerings)

        # A new instance profile takes a few seconds to become usable by Auto Scaling.
        def make_group(pool: PoolSpec = pool, lt: str = lt, usable: list[str] = usable) -> str:
            return ensure_auto_scaling_group(
                c, spec, pool, lt_name=lt, subnets=usable, report=report, dry_run=dry_run,
                halted=halted,
            )  # fmt: skip

        asg = _retry(
            make_group,
            codes={"ValidationError"},
            attempts=6,
            delay_s=5.0,
        )
        pools[pool.name] = {
            "task_queue_url": task_url,
            "dlq_url": dlq_url,
            "launch_template": lt,
            "auto_scaling_group": asg,
            "capacity_type": "vcpu",
        }
    write_manifest(
        c,
        spec,
        manifest(
            spec,
            pools,
            profile_arn=profile_arn,
            sg=sg,
            subnets=subnets,
            worker_role_id=role_id(c, naming.worker_role(spec.deployment, spec.region)),
        ),
        report,
        dry_run=dry_run,
    )
    ensure_state(c, spec, report, dry_run=dry_run)
    if c.scheduler is not None:
        from aegc import lease

        lease.ensure_lease(
            scheduler=c.scheduler,
            iam=c.iam,
            spec=lease.LeaseSpec(
                deployment=spec.deployment,
                region=spec.region,
                account=spec.account,
                groups={
                    n: str(p["auto_scaling_group"])
                    for n, p in pools.items()
                    if "auto_scaling_group" in p
                },
                mappings={
                    n: str(p["event_source_mapping"])
                    for n, p in pools.items()
                    if "event_source_mapping" in p
                },
                lease_s=spec.lease_s,
                ttl_at=spec.ttl_at,
            ),
            report=report,
            dry_run=dry_run,
        )
    if inuse is not None:
        for env, keep in inuse.items():
            images.release(c.ecr, env, spec.deployment, keep, report=report)
    return report


def ensure_state(c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool) -> None:
    """Create the deployment state parameter as running. A redeploy never changes the state."""
    from aegc import state

    name = naming.state_parameter(spec.deployment)
    try:
        c.ssm.get_parameter(Name=name)
        report.add("ssm parameter", name, "exists")
        return
    except Exception as e:
        if not is_not_found(e):
            raise
    if dry_run:
        report.add("ssm parameter", name, "would-create")
        return
    state.write_state(
        c.ssm,
        spec.deployment,
        state.State(reason="deployed", since=time.time(), by="aegc deploy"),
        extra_tags=spec.extra_tags,
    )
    report.add("ssm parameter", name, "created")


def _protect_images(
    c: Clients, spec: DeploymentSpec, report: Report, *, dry_run: bool
) -> dict[str, set[str]] | None:
    """Tag each pool's pinned image ``inuse-<d>-...`` (#27). Returns ``{env: tags to keep}``.

    Includes envs the previous manifest used, so a redeploy that drops one releases its tags.
    """
    if c.ecr is None or dry_run:
        return None
    d = spec.deployment
    keep: dict[str, set[str]] = {}
    try:
        keep = {str(p["env"]): set() for p in read_manifest(c.ssm, d).get("pools", {}).values()}
    except Exception as e:
        if not is_not_found(e):
            raise
    for pool in spec.pools:
        tag = images.protect(c.ecr, pool.env, pool.image, d, report=report)
        keep.setdefault(pool.env, set()).add(tag)
    return keep


# --- destroy -----------------------------------------------------------------------------------


def destroy(
    c: Clients,
    *,
    deployment: str,
    region: str,
    dry_run: bool = True,
    force: bool = False,
    wait_s: float = 600,
    poll_s: float = 10,
    verify: bool = True,
) -> Report:
    """Delete everything aegc created for ``deployment``. Dry run unless ``dry_run=False``.

    Refuses while tasks are queued or in flight unless ``force``. Order: Auto Scaling groups
    (forced, and waited for), launch templates, security group, Lambda pools' mappings,
    functions and function roles, queues, instance profile and role,
    log group, the deployment's ECR in-use tags, and its parameters (``/aegc/<d>/...``). Payload
    objects expire through the bucket's lifecycle rule.

    After deleting, ``verify`` re-lists everything (see :func:`verify_destroyed`) and reports
    what's left as ``remaining``.
    """
    d = naming.validate_name("deployment", deployment)
    report = Report()
    verb: Outcome = "would-delete" if dry_run else "deleted"
    prefix = f"{naming.PREFIX}-{d}-"
    try:
        manifest_pools = dict(read_manifest(c.ssm, d).get("pools", {}))
    except Exception as e:
        if not is_not_found(e):
            raise
        manifest_pools = {}
    envs = sorted({str(p["env"]) for p in manifest_pools.values()})
    had_lambda = any(lambda_pools.is_lambda(p) for p in manifest_pools.values())

    # Queues first, to check for in-flight work before deleting anything.
    queues = [
        u
        for u in paginate(c.sqs, "list_queues", "QueueUrls", page_size=1000, QueueNamePrefix=prefix)
        if naming.is_owned(u.rsplit("/", 1)[-1], _queue_tags(c, u), d)
    ]
    if not force:
        busy = []
        for u in queues:
            if u.endswith("-tasks"):
                a = c.sqs.get_queue_attributes(
                    QueueUrl=u,
                    AttributeNames=[
                        "ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible",
                    ],
                )["Attributes"]
                n = int(a.get("ApproximateNumberOfMessages", 0)) + int(
                    a.get("ApproximateNumberOfMessagesNotVisible", 0)
                )
                if n:
                    busy.append(f"{u.rsplit('/', 1)[-1]} ({n})")
        if busy:
            raise RuntimeError(f"tasks are queued or running: {', '.join(busy)}; use force")

    groups = [
        g
        for g in paginate(c.autoscaling, "describe_auto_scaling_groups", "AutoScalingGroups")
        if g["AutoScalingGroupName"].startswith(prefix)
    ]
    for g in groups:
        name = g["AutoScalingGroupName"]
        if not naming.is_owned(name, naming.tag_dict(g.get("Tags", [])), d):
            report.add("auto scaling group", name, "kept", "not tagged as aegc's")
            continue
        if not dry_run:
            c.autoscaling.delete_auto_scaling_group(AutoScalingGroupName=name, ForceDelete=True)
            _wait_gone(c, name, wait_s=wait_s, poll_s=poll_s)
        report.add("auto scaling group", name, verb)

    for lt in list(
        paginate(
            c.ec2, "describe_launch_templates", "LaunchTemplates", Filters=_owned_ec2_filters(d)
        )
    ):
        name = lt["LaunchTemplateName"]
        if not name.startswith(prefix):
            continue
        if not dry_run:
            c.ec2.delete_launch_template(LaunchTemplateName=name)
        report.add("launch template", name, verb)

    sg_name = naming.security_group(d)
    for g in list(
        paginate(
            c.ec2,
            "describe_security_groups",
            "SecurityGroups",
            Filters=[{"Name": "group-name", "Values": [sg_name]}, *_owned_ec2_filters(d)],
        )
    ):
        if not dry_run:
            # Network interfaces of terminated instances can take a minute to go away.
            def delete_group(gid: str = g["GroupId"]) -> Any:
                return c.ec2.delete_security_group(GroupId=gid)

            _retry(
                delete_group,
                codes={"DependencyViolation"},
                attempts=max(1, int(wait_s // max(poll_s, 1))),
                delay_s=poll_s,
            )
        report.add("security group", sg_name, verb, g["GroupId"])

    # Lambda pools (#21): mappings before their queues. They need the lambda-deployer policy;
    # a deployer without it still destroys everything else.
    if c.lambda_ is not None:
        try:
            destroy_lambda(c, d, region, report, dry_run=dry_run)
        except Exception as e:
            if error_code(e) not in ACCESS_DENIED:
                raise
            # With Lambda pools in the manifest, they're still there: say so (and fail).
            report.add("lambda pools", d, "remaining" if had_lambda else "kept", LAMBDA_DENIED)

    for u in queues:
        if not dry_run:
            c.sqs.delete_queue(QueueUrl=u)
        report.add("sqs queue", u.rsplit("/", 1)[-1], verb)

    if c.scheduler is not None:
        from aegc import lease

        lease.destroy_lease(
            scheduler=c.scheduler, iam=c.iam, deployment=d, region=region, report=report,
            dry_run=dry_run,
        )  # fmt: skip
    _destroy_worker_role(c, d, region, report, dry_run=dry_run)
    _destroy_role(c, d, naming.task_role(d, region), report, dry_run=dry_run)
    # The cloud endpoint (#22): its group and template went with the others above. Its alarm,
    # topic and secret need the endpoint-deployer policy; a deployer without it still destroys
    # everything else.
    from aegc.infra import endpoint_host, secrets

    report.actions.extend(
        endpoint_host.undeploy(
            c, d, region, dry_run=dry_run, wait_s=wait_s, poll_s=poll_s, host=False
        ).actions
    )
    try:
        if c.secretsmanager is not None:
            secrets.delete_login(c.secretsmanager, d, report, dry_run=dry_run)
    except Exception as e:
        if error_code(e) not in ACCESS_DENIED:
            raise
        report.add("cloud endpoint", d, "kept",
                   "not checked: needs the endpoint-deployer policy (aegc iam-policy "
                   "endpoint-deployer)")  # fmt: skip

    log_name = naming.log_group(d)
    for g in list(
        paginate(c.logs, "describe_log_groups", "logGroups", logGroupNamePrefix=log_name)
    ):
        if g["logGroupName"] != log_name:
            continue
        tags = c.logs.list_tags_for_resource(resourceArn=g["arn"].removesuffix(":*"))["tags"]
        if not naming.is_owned(f"{naming.PREFIX}-{d}", tags, d):
            report.add("log group", log_name, "kept", "not tagged as aegc's")
            continue
        if not dry_run:
            c.logs.delete_log_group(logGroupName=log_name)
        report.add("log group", log_name, verb)

    if c.ecr is not None:
        for env in envs:
            images.release(c.ecr, env, d, report=report, dry_run=dry_run)

    for name, tags in _owned_parameters(c, d):
        if not naming.is_owned(f"{naming.PREFIX}-{d}", tags, d):
            report.add("ssm parameter", name, "kept", "not tagged as aegc's")
            continue
        if not dry_run:
            c.ssm.delete_parameter(Name=name)
        report.add("ssm parameter", name, verb)

    if verify and not dry_run:
        report.actions.extend(
            verify_destroyed(c, deployment=d, region=region, wait_s=wait_s, poll_s=poll_s).actions
        )
    return report


LAMBDA_DENIED = "not checked: needs the lambda-deployer policy (aegc iam-policy lambda-deployer)"


def _owned_parameters(c: Clients, d: str) -> list[tuple[str, dict[str, str]]]:
    """Every parameter under ``/aegc/<d>/`` with its tags (ownership is the caller's check)."""
    out = []
    for p in list(
        paginate(
            c.ssm, "get_parameters_by_path", "Parameters", Path=f"/{naming.PREFIX}/{d}",
            Recursive=True,
        )
    ):  # fmt: skip
        name = str(p["Name"])
        try:
            tags = naming.tag_dict(
                c.ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
            )
        except Exception as e:
            # SSM reports a missing parameter to ListTagsForResource as InvalidResourceId.
            if is_not_found(e) or error_code(e) == "InvalidResourceId":
                continue
            raise
        out.append((name, tags))
    return out


#: Instance and volume states that mean "still there" (terminated and deleted are gone).
_LIVE_INSTANCE = ["pending", "running", "shutting-down", "stopping", "stopped"]
_LIVE_VOLUME = ["creating", "available", "in-use", "deleting", "error"]
#: States that resolve on their own after a group is deleted, so are worth waiting out. A running
#: instance or an available volume outside any group stays: waiting for it would only delay.
_SETTLING = {"pending", "shutting-down", "stopping", "creating", "in-use", "deleting"}


def _leftover_ec2(c: Clients, d: str) -> list[tuple[str, str, str]]:
    owned = _owned_ec2_filters(d)
    left = [
        ("ec2 instance", i["InstanceId"], i["State"]["Name"])
        for r in paginate(
            c.ec2, "describe_instances", "Reservations",
            Filters=[*owned, {"Name": "instance-state-name", "Values": _LIVE_INSTANCE}],
        )
        for i in r.get("Instances", [])
    ]  # fmt: skip
    left += [
        ("ebs volume", v["VolumeId"], v["State"])
        for v in paginate(
            c.ec2, "describe_volumes", "Volumes",
            Filters=[*owned, {"Name": "status", "Values": _LIVE_VOLUME}],
        )
    ]  # fmt: skip
    left += [
        ("network interface", n["NetworkInterfaceId"], n.get("Status", ""))
        for n in paginate(c.ec2, "describe_network_interfaces", "NetworkInterfaces", Filters=owned)
    ]
    return left


def verify_destroyed(
    c: Clients, *, deployment: str, region: str, wait_s: float = 600, poll_s: float = 10
) -> Report:
    """Re-list everything ``destroy`` is responsible for; each thing still there is ``remaining``.

    Changes nothing. Instances, volumes and network interfaces tagged with the deployment take a
    while to go after their group is deleted, so this waits up to ``wait_s`` while any of them is
    still terminating, detaching or deleting. Then it
    checks the Auto Scaling groups, launch templates, security group, queues, log group,
    ``/aegc/<d>/...`` parameters, worker role and instance profile, (given an ECR client) the
    deployment's in-use image tags, and (given a Lambda client) its Lambda pools' mappings,
    functions and function roles. An empty report means the deployment is gone.
    """
    d = naming.validate_name("deployment", deployment)
    prefix = f"{naming.PREFIX}-{d}-"
    report = Report()
    deadline = time.monotonic() + wait_s
    while (left := _leftover_ec2(c, d)) and time.monotonic() < deadline:
        if not any(k == "network interface" or st in _SETTLING for k, _, st in left):
            break
        time.sleep(poll_s)
    for kind, rid, state in left:
        report.add(kind, rid, "remaining", state)

    for g in paginate(c.autoscaling, "describe_auto_scaling_groups", "AutoScalingGroups"):
        name = g["AutoScalingGroupName"]
        if name.startswith(prefix) and naming.is_owned(name, naming.tag_dict(g.get("Tags")), d):
            report.add("auto scaling group", name, "remaining")
    for lt in paginate(
        c.ec2, "describe_launch_templates", "LaunchTemplates", Filters=_owned_ec2_filters(d)
    ):
        if lt["LaunchTemplateName"].startswith(prefix):
            report.add("launch template", lt["LaunchTemplateName"], "remaining")
    for g in paginate(
        c.ec2, "describe_security_groups", "SecurityGroups",
        Filters=[{"Name": "group-name", "Values": [naming.security_group(d)]},
                 *_owned_ec2_filters(d)],
    ):  # fmt: skip
        report.add("security group", naming.security_group(d), "remaining", g["GroupId"])
    for u in paginate(c.sqs, "list_queues", "QueueUrls", page_size=1000, QueueNamePrefix=prefix):
        if naming.is_owned(u.rsplit("/", 1)[-1], _queue_tags(c, u), d):
            report.add("sqs queue", u.rsplit("/", 1)[-1], "remaining")
    log_name = naming.log_group(d)
    for g in paginate(c.logs, "describe_log_groups", "logGroups", logGroupNamePrefix=log_name):
        if g["logGroupName"] == log_name:
            tags = c.logs.list_tags_for_resource(resourceArn=g["arn"].removesuffix(":*"))["tags"]
            if naming.is_owned(f"{naming.PREFIX}-{d}", tags, d):
                report.add("log group", log_name, "remaining")
    for name, tags in _owned_parameters(c, d):
        if naming.is_owned(f"{naming.PREFIX}-{d}", tags, d):
            report.add("ssm parameter", name, "remaining")

    role = naming.worker_role(d, region)
    checks: list[tuple[str, Callable[[], Any]]] = [
        (
            "iam instance profile",
            lambda: c.iam.list_instance_profile_tags(InstanceProfileName=role),
        ),
        ("iam role", lambda: c.iam.list_role_tags(RoleName=role)),
    ]
    task = naming.task_role(d, region)
    for kind, list_tags in checks:
        try:
            if naming.is_owned(role, naming.tag_dict(list_tags()["Tags"]), d):
                report.add(kind, role, "remaining")
        except Exception as e:
            if not is_not_found(e):
                raise
    for extra in (naming.endpoint_role(d, region),):  # the cloud endpoint's role (#22)
        try:
            if naming.is_owned(
                extra, naming.tag_dict(c.iam.list_role_tags(RoleName=extra)["Tags"]), d
            ):
                report.add("iam role", extra, "remaining")
        except Exception as e:
            if not is_not_found(e):
                raise
    try:  # the cloud endpoint's secret, alarm and topic (endpoint-deployer policy)
        if c.secretsmanager is not None:
            from aegc.infra import secrets

            if secrets.exists(c.secretsmanager, d):
                report.add("secret", naming.endpoint_secret(d), "remaining")
        if c.cloudwatch is not None and c.cloudwatch.describe_alarms(
            AlarmNames=[naming.endpoint_alarm(d)]
        ).get("MetricAlarms"):
            report.add("cloudwatch alarm", naming.endpoint_alarm(d), "remaining")
        from aegc.infra import endpoint_host

        if endpoint_host._topic_arn(c, d):
            report.add("sns topic", naming.endpoint_topic(d), "remaining")
    except Exception as e:
        if error_code(e) not in ACCESS_DENIED:
            raise
    try:  # the task role (#32)
        if naming.is_owned(task, naming.tag_dict(c.iam.list_role_tags(RoleName=task)["Tags"]), d):
            report.add("iam role", task, "remaining")
    except Exception as e:
        if not is_not_found(e):
            raise
    if c.ecr is not None:
        for repo, tags in images.inuse_tags(c.ecr, d).items():
            for t in tags:
                report.add("ecr image tag", f"{repo}:{t}", "remaining")
    if c.lambda_ is not None:  # Lambda pools (#21; lambda-deployer policy)
        try:
            for kind, name in lambda_leftovers(c, d, region):
                report.add(kind, name, "remaining")
        except Exception as e:
            if error_code(e) not in ACCESS_DENIED:
                raise
            report.add("lambda pools", d, "kept", LAMBDA_DENIED)  # shown, and named below
    if c.scheduler is not None:
        from aegc import lease

        try:
            g = c.scheduler.get_schedule_group(Name=lease.group_name(d))
            if g.get("State") != "DELETING":  # DeleteScheduleGroup completes asynchronously
                report.add("schedule group", lease.group_name(d), "remaining")
        except Exception as e:
            if not is_not_found(e):
                raise
        try:
            c.iam.get_role(RoleName=lease.role_name(d, region))
            report.add("iam role", lease.role_name(d, region), "remaining")
        except Exception as e:
            if not is_not_found(e):
                raise
    return report


def remaining(report: Report) -> list[str]:
    """The ``remaining`` entries of a destroy or verify report, as printable lines."""
    return [str(a) for a in report.actions if a.outcome == "remaining"]


def _wait_gone(c: Clients, name: str, *, wait_s: float, poll_s: float) -> None:
    deadline = time.monotonic() + wait_s
    while c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
        "AutoScalingGroups"
    ]:
        if time.monotonic() > deadline:
            raise TimeoutError(f"auto scaling group {name} still exists after {wait_s:.0f} s")
        time.sleep(poll_s)


def _destroy_worker_role(c: Clients, d: str, region: str, report: Report, *, dry_run: bool) -> None:
    name = naming.worker_role(d, region)
    verb: Outcome = "would-delete" if dry_run else "deleted"
    try:
        prof_tags = naming.tag_dict(
            c.iam.list_instance_profile_tags(InstanceProfileName=name)["Tags"]
        )
        if naming.is_owned(name, prof_tags, d):
            if not dry_run:
                prof = c.iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
                for r in prof.get("Roles", []):
                    c.iam.remove_role_from_instance_profile(
                        InstanceProfileName=name, RoleName=r["RoleName"]
                    )
                c.iam.delete_instance_profile(InstanceProfileName=name)
            report.add("iam instance profile", name, verb)
        else:
            report.add("iam instance profile", name, "kept", "not tagged as aegc's")
    except Exception as e:
        if not is_not_found(e):
            raise
    _destroy_role(c, d, name, report, dry_run=dry_run)


def _destroy_role(c: Clients, d: str, name: str, report: Report, *, dry_run: bool) -> None:
    verb: Outcome = "would-delete" if dry_run else "deleted"
    try:
        role_tags = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if naming.is_owned(name, role_tags, d):
            if not dry_run:
                for p in list(paginate(c.iam, "list_role_policies", "PolicyNames", RoleName=name)):
                    c.iam.delete_role_policy(RoleName=name, PolicyName=p)
                c.iam.delete_role(RoleName=name)
            report.add("iam role", name, verb)
        else:
            report.add("iam role", name, "kept", "not tagged as aegc's")
    except Exception as e:
        if not is_not_found(e):
            raise
