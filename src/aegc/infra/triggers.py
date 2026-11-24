"""S3-event triggers (#83): ``aegc trigger login / create / list / delete``, and destroy.

A trigger ``<name>`` of deployment ``<d>`` is, all named ``aegc-<d>-trigger-<name>`` and tagged
(``aegc:role``: ``trigger-rule``, ``trigger-function``, ``trigger-role``, ``trigger-login``,
``trigger-dlq``, ``trigger``):

- an EventBridge rule for S3 ``Object Created`` on one bucket (and key prefix/suffix), whose
  target is
- a Lambda function (zip, python3.13, arm64, handler ``aegc.trigger.handler``, reserved
  concurrency 2) that starts the Globus flow with
- the user's Globus login (secret ``aegc-<d>-trigger-<name>``, readable by the function's role
  only), its failed events going to
- a dead-letter queue ``aegc-<d>-trigger-<name>-dlq`` (the function's on-failure destination);
- its role ``/aegc/aegc-<d>-trigger-<name>-<region>``: read that secret, write logs, send to
  that queue. No S3 data access: the flow does the work.
- its spec in ``/aegc/<d>/triggers/<name>``.

The bucket isn't aegc's: create turns on its EventBridge delivery with a read-merge of its
notification configuration, and delete and destroy leave it on ("kept"). Everything else is
deleted only if its name and tags say aegc made it for the deployment.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aegc import __version__, naming, trigger, triggers
from aegc.awsutil import error_code, is_not_found, paginate
from aegc.infra import Outcome, OwnershipError, Report, iam, secrets

RUNTIME = "python3.13"
HANDLER = "aegc.trigger.handler"
MEMORY_MIB = 256
TIMEOUT_S = 60
#: Few runs at once: a burst of uploads starts flows a few at a time (EventBridge and Lambda's
#: async queue hold the rest), and a runaway can't start hundreds of flows a second.
RESERVED_CONCURRENCY = 2
#: Lambda's async retries, and how long an event may wait, before it goes to the DLQ.
MAX_RETRIES = 2
MAX_EVENT_AGE_S = 6 * 3600
DLQ_RETENTION_S = 14 * 24 * 3600
TARGET_ID = "aegc-trigger"
PERMISSION_SID = "aegc-trigger-events"
LAMBDA_ENV_LIMIT = 4096
#: Zip packages uploaded directly (CreateFunction ZipFile) are limited to 50 MB.
MAX_ZIP_BYTES = 50 * 2**20
#: Platforms for the vendored wheels (Lambda's python3.13 runtime is Amazon Linux 2023).
PLATFORMS = ("manylinux2014_aarch64", "manylinux_2_28_aarch64")


@dataclass(frozen=True)
class TriggerSpec:
    deployment: str
    region: str
    account: str
    name: str
    flow_id: str
    bucket: str
    prefix: str = ""
    suffix: str = ""
    input_template: dict[str, Any] = field(default_factory=dict)
    extra_tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        naming.validate_name("deployment", self.deployment)
        naming.validate_name("trigger", self.name)
        if len(self.role) > 64:
            raise naming.NameError_(f"trigger role {self.role} is over IAM's 64 characters")
        try:
            uuid.UUID(self.flow_id)
        except ValueError:
            raise ValueError(f"flow id {self.flow_id!r} isn't a UUID") from None
        if not iam._BUCKET_RE.fullmatch(self.bucket):
            raise ValueError(f"{self.bucket!r} isn't an S3 bucket name")
        trigger.check_template(self.input_template)
        if (n := sum(len(k) + len(v) for k, v in self.environment().items())) > LAMBDA_ENV_LIMIT:
            raise ValueError(
                f"the input template makes {n} bytes of environment variables; Lambda allows "
                f"{LAMBDA_ENV_LIMIT}"
            )

    @property
    def resource(self) -> str:
        return naming.trigger_resource(self.deployment, self.name)

    @property
    def role(self) -> str:
        return naming.trigger_role(self.deployment, self.name, self.region)

    @property
    def role_arn(self) -> str:
        return f"arn:aws:iam::{self.account}:role{naming.IAM_PATH}{self.role}"

    @property
    def rule_arn(self) -> str:
        return f"arn:aws:events:{self.region}:{self.account}:rule/{self.resource}"

    @property
    def function_arn(self) -> str:
        return f"arn:aws:lambda:{self.region}:{self.account}:function:{self.resource}"

    @property
    def dlq_arn(self) -> str:
        dlq = naming.trigger_dlq(self.deployment, self.name)
        return f"arn:aws:sqs:{self.region}:{self.account}:{dlq}"

    def environment(self) -> dict[str, str]:
        return {
            "AEGC_DEPLOYMENT": self.deployment,
            "AEGC_TRIGGER": self.name,
            "AEGC_TRIGGER_SECRET": self.resource,
            "AEGC_FLOW_ID": self.flow_id,
            "AEGC_TRIGGER_INPUT": json.dumps(
                self.input_template, sort_keys=True, separators=(",", ":")
            ),
        }

    def event_pattern(self) -> dict[str, Any]:
        """S3 ``Object Created`` on the bucket; prefix and suffix are ANDed (a wildcard when
        both are given: two patterns for one field would be ORed)."""
        detail: dict[str, Any] = {"bucket": {"name": [self.bucket]}}
        if self.prefix and self.suffix:
            esc = [s.replace("\\", "\\\\").replace("*", "\\*") for s in (self.prefix, self.suffix)]
            detail["object"] = {"key": [{"wildcard": f"{esc[0]}*{esc[1]}"}]}
        elif self.prefix:
            detail["object"] = {"key": [{"prefix": self.prefix}]}
        elif self.suffix:
            detail["object"] = {"key": [{"suffix": self.suffix}]}
        return {"source": ["aws.s3"], "detail-type": ["Object Created"], "detail": detail}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TriggerSpec:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TriggerClients:
    """The clients ``aegc trigger`` uses (``deployment.Clients`` has them all)."""

    iam: Any
    ssm: Any
    sqs: Any
    lambda_: Any
    events: Any
    s3: Any = None
    secretsmanager: Any = None
    scheduler: Any = None
    cloudwatch: Any = None


# --- login --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerLogin:
    """The stored login: the native app's client id, the flow, and globus-sdk token data by
    resource server (``TokenStorageData.to_dict()``: access and refresh tokens)."""

    client_id: str
    flow_id: str
    tokens: dict[str, dict[str, Any]]

    def to_json(self) -> str:
        return json.dumps(
            {"version": 1, "client_id": self.client_id, "flow_id": self.flow_id,
             "tokens": self.tokens},
            sort_keys=True,
        )  # fmt: skip


def globus_login(flow_id: str, client_id: str) -> TriggerLogin:
    """Log in (interactively: a URL to open, a code to paste) with a globus-sdk ``UserApp`` and
    consent to the flow's scope with refresh tokens; nothing is written locally."""
    from globus_sdk import GlobusAppConfig, SpecificFlowClient, UserApp
    from globus_sdk.token_storage import MemoryTokenStorage

    storage = MemoryTokenStorage()
    app = UserApp(
        "aegc-trigger",
        client_id=client_id,
        config=GlobusAppConfig(
            token_storage=storage,
            request_refresh_tokens=True,
        ),
    )
    flows = SpecificFlowClient(flow_id, app=app)  # adds the flow's scope requirement
    app.login(force=True)
    del flows
    data = storage.get_token_data_by_resource_server()
    if not data.get(flow_id) or not data[flow_id].refresh_token:
        raise RuntimeError(f"the login has no refresh token for flow {flow_id}")
    return TriggerLogin(client_id, flow_id, {rs: td.to_dict() for rs, td in data.items()})


def put_login(
    sm: Any, spec_deployment: str, name: str, region: str, account: str, login: TriggerLogin,
    report: Report, *, dry_run: bool, extra_tags: dict[str, str] | None = None,
) -> str:  # fmt: skip
    """Store the login in ``aegc-<d>-trigger-<name>``, readable by the trigger's role only (its
    ARN is known before the role exists: the policy's condition names it)."""
    naming.validate_name("trigger", name)
    role = naming.trigger_role(spec_deployment, name, region)
    return secrets.put_owned_secret(
        sm, name=naming.trigger_resource(spec_deployment, name), deployment=spec_deployment,
        value=login.to_json(), role="trigger-login",
        description=f"aegc: Globus login of trigger {name} of deployment {spec_deployment} "
        f"(flow {login.flow_id})",
        reader_role_arn=f"arn:aws:iam::{account}:role{naming.IAM_PATH}{role}",
        sid="OnlyTheTriggerFunction", report=report, dry_run=dry_run,
        detail=f"flow {login.flow_id}", extra_tags=extra_tags,
    )  # fmt: skip


# --- the package --------------------------------------------------------------------------------


def _pip_command(target: str, requirement: str) -> list[str]:
    plat = [x for p in PLATFORMS for x in ("--platform", p)]
    common = ["--only-binary=:all:", "--target", target, requirement]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
                "--implementation", "cp", "--python-version", RUNTIME.removeprefix("python"),
                *plat, *common]  # fmt: skip
    # A uv-made venv has no pip.
    uv = shutil.which("uv") or "uv"
    return [uv, "pip", "install", "--quiet", "--python-platform", "aarch64-manylinux_2_28",
            "--python-version", RUNTIME.removeprefix("python"), *common]  # fmt: skip


def build_package(*, run: Callable[..., Any] = subprocess.run, workdir: str | None = None) -> bytes:
    """The function's zip: ``aegc/__init__.py``, ``aegc/trigger.py`` and globus-sdk (at the
    version installed here) with its dependencies, as manylinux aarch64 wheels for Python 3.13.
    boto3 comes with the runtime. Deterministic, so an unchanged package isn't redeployed."""
    version = importlib.metadata.version("globus-sdk")
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        target = os.path.join(tmp, "pkg")
        os.makedirs(os.path.join(target, "aegc"))
        r = run(_pip_command(target, f"globus-sdk=={version}"), capture_output=True, text=True,
                timeout=600)  # fmt: skip
        if getattr(r, "returncode", 0) != 0:
            raise RuntimeError(f"vendoring globus-sdk failed: {getattr(r, 'stderr', '')[-2000:]}")
        src = Path(trigger.__file__)
        shutil.copyfile(src, os.path.join(target, "aegc", "trigger.py"))
        with open(os.path.join(target, "aegc", "__init__.py"), "w") as f:
            f.write(f'"""aegc: the S3-event trigger handler only."""\n\n__version__ = '
                    f'"{__version__}"\n')  # fmt: skip
        return _zip(target)


def _zip(root: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for fn in sorted(filenames):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(dirpath, fn)
                info = zipfile.ZipInfo(os.path.relpath(path, root), (1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as f:
                    z.writestr(info, f.read())
    data = buf.getvalue()
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError(f"the trigger package is {len(data)} bytes; Lambda allows {MAX_ZIP_BYTES}")
    return data


def code_sha256(package: bytes) -> str:
    """As Lambda reports it (``CodeSha256``)."""
    return base64.b64encode(hashlib.sha256(package).digest()).decode()


# --- ensure -------------------------------------------------------------------------------------


def _rule(events: Any, name: str) -> dict[str, Any] | None:
    try:
        return dict(events.describe_rule(Name=name))
    except Exception as e:
        if is_not_found(e):
            return None
        raise


def _rule_tags(events: Any, arn: str) -> dict[str, str]:
    return naming.tag_dict(events.list_tags_for_resource(ResourceARN=arn).get("Tags"))


def _queue_url(sqs: Any, name: str) -> str | None:
    try:
        return str(sqs.get_queue_url(QueueName=name)["QueueUrl"])
    except Exception as e:
        if is_not_found(e):
            return None
        raise


def _queue_tags(sqs: Any, url: str) -> dict[str, str]:
    try:
        return dict(sqs.list_queue_tags(QueueUrl=url).get("Tags") or {})
    except Exception as e:
        if is_not_found(e):
            return {}
        raise


def _function(lam: Any, name: str) -> dict[str, Any] | None:
    try:
        return dict(lam.get_function(FunctionName=name))
    except Exception as e:
        if is_not_found(e):
            return None
        raise


def _role_tags(iam_: Any, name: str) -> dict[str, str] | None:
    try:
        return naming.tag_dict(iam_.list_role_tags(RoleName=name)["Tags"])
    except Exception as e:
        if is_not_found(e):
            return None
        raise


TAG_TRIGGER = "aegc:trigger"


def _tags(spec: TriggerSpec, role: str) -> dict[str, str]:
    return {**naming.tags(spec.deployment, role, extra=spec.extra_tags), TAG_TRIGGER: spec.name}


def ensure_dlq(c: TriggerClients, spec: TriggerSpec, report: Report, *, dry_run: bool) -> str:
    d, name = spec.deployment, naming.trigger_dlq(spec.deployment, spec.name)
    attrs = {"MessageRetentionPeriod": str(DLQ_RETENTION_S), "SqsManagedSseEnabled": "true"}
    url = _queue_url(c.sqs, name)
    if url is None:
        if dry_run:
            report.add("sqs queue", name, "would-create")
            return name
        url = str(
            c.sqs.create_queue(QueueName=name, Attributes=attrs,
                               tags=_tags(spec, "trigger-dlq"))["QueueUrl"]
        )  # fmt: skip
        report.add("sqs queue", name, "created")
        return url
    if not naming.is_owned(name, _queue_tags(c.sqs, url), d):
        raise OwnershipError(f"queue {name} exists but isn't tagged as {d}'s")
    report.add("sqs queue", name, "exists")
    return url


def ensure_role(c: TriggerClients, spec: TriggerSpec, report: Report, *, dry_run: bool) -> str:
    d, name = spec.deployment, spec.role
    trust = iam.trigger_trust_policy(spec.account)
    found = _role_tags(c.iam, name)
    outcome: Outcome = "exists"
    if found is None:
        outcome = "would-create" if dry_run else "created"
        if not dry_run:
            c.iam.create_role(
                Path=naming.IAM_PATH, RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                Description=f"aegc trigger {spec.name} of deployment {d}",
                Tags=naming.tag_list(_tags(spec, "trigger-role")),
            )  # fmt: skip
    elif not naming.is_owned(name, found, d):
        raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
    if not dry_run:
        c.iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        policy = iam.trigger_policy(
            deployment=d, name=spec.name, region=spec.region, account=spec.account
        )
        c.iam.put_role_policy(
            RoleName=name, PolicyName=iam.TRIGGER_POLICY, PolicyDocument=json.dumps(policy)
        )
    report.add("iam role", name, outcome)
    return spec.role_arn


def _function_config(spec: TriggerSpec) -> dict[str, Any]:
    return {
        "Role": spec.role_arn,
        "Description": f"aegc trigger {spec.name} of deployment {spec.deployment}: S3 "
        f"{spec.bucket}/{spec.prefix}*{spec.suffix} -> flow {spec.flow_id}",
        "Runtime": RUNTIME,
        "Handler": HANDLER,
        "MemorySize": MEMORY_MIB,
        "Timeout": TIMEOUT_S,
        "Environment": {"Variables": spec.environment()},
        "LoggingConfig": {"LogFormat": "Text", "LogGroup": naming.log_group(spec.deployment)},
    }


def _retry_new_role(fn: Callable[[], Any], *, attempts: int = 10, delay_s: float = 3.0) -> Any:
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if (
                error_code(e) != "InvalidParameterValueException"
                or "role" not in str(e).lower()
                or attempt == attempts - 1
            ):
                raise
            time.sleep(delay_s)
    return None


def _wait(lam: Any, name: str, *, wait_s: float = 300, poll_s: float = 2) -> None:
    deadline = time.monotonic() + wait_s
    while True:
        cfg = lam.get_function(FunctionName=name)["Configuration"]
        state, update = cfg.get("State", "Active"), cfg.get("LastUpdateStatus", "Successful")
        if state == "Failed" or update == "Failed":
            raise RuntimeError(f"Lambda function {name} failed: {cfg.get('StateReason') or ''}")
        if (state != "Pending" and update != "InProgress") or time.monotonic() > deadline:
            return
        time.sleep(poll_s)


def ensure_function(
    c: TriggerClients, spec: TriggerSpec, package: bytes | None, report: Report, *, dry_run: bool
) -> str:
    """Created, or updated in place: the code when the package's hash differs, the settings when
    they do. ``package`` None (a dry run without a build) reports the code as unknown."""
    d, lam, name = spec.deployment, c.lambda_, spec.resource
    want = _function_config(spec)
    got = _function(lam, name)
    if got is None:
        if dry_run:
            report.add("lambda function", name, "would-create", f"{RUNTIME} arm64 zip")
            return spec.function_arn
        if package is None:
            raise ValueError("creating a trigger's function needs its package")
        _retry_new_role(
            lambda: lam.create_function(
                FunctionName=name, PackageType="Zip", Code={"ZipFile": package},
                Architectures=["arm64"], Publish=False, Tags=_tags(spec, "trigger-function"),
                **want,
            )
        )  # fmt: skip
        _wait(lam, name)
        report.add("lambda function", name, "created", f"{RUNTIME} arm64 zip")
    else:
        cfg = dict(got["Configuration"])
        if not naming.is_owned(name, naming.tag_dict(lam.list_tags(Resource=cfg["FunctionArn"])
                                                     .get("Tags")), d):  # fmt: skip
            raise OwnershipError(f"Lambda function {name} exists but isn't tagged as {d}'s")
        new_code = package is not None and cfg.get("CodeSha256") != code_sha256(package)
        logging_ = cfg.get("LoggingConfig") or {}
        have = {
            **{k: cfg.get(k) for k in ("Role", "Description", "Runtime", "Handler",
                                       "MemorySize", "Timeout")},
            "Environment": {"Variables": (cfg.get("Environment") or {}).get("Variables", {})},
            # substrate#1659: LoggingConfig isn't echoed; a missing one isn't a change.
            "LoggingConfig": {k: logging_.get(k) for k in ("LogFormat", "LogGroup")}
            if logging_ else want["LoggingConfig"],
        }  # fmt: skip
        changes = (["code"] if new_code else []) + [k for k in want if have.get(k) != want[k]]
        if not changes:
            report.add("lambda function", name, "exists")
        elif dry_run:
            report.add("lambda function", name, "would-update", ", ".join(changes))
        else:
            _wait(lam, name)
            if new_code:
                lam.update_function_code(FunctionName=name, ZipFile=package,
                                         Architectures=["arm64"])  # fmt: skip
                _wait(lam, name)
            if changes != ["code"]:
                _retry_new_role(lambda: lam.update_function_configuration(FunctionName=name,
                                                                          **want))  # fmt: skip
                _wait(lam, name)
            report.add("lambda function", name, "updated", ", ".join(changes))
    if not dry_run:
        lam.put_function_concurrency(
            FunctionName=name, ReservedConcurrentExecutions=RESERVED_CONCURRENCY
        )
        lam.put_function_event_invoke_config(
            FunctionName=name, MaximumRetryAttempts=MAX_RETRIES,
            MaximumEventAgeInSeconds=MAX_EVENT_AGE_S,
            DestinationConfig={"OnFailure": {"Destination": spec.dlq_arn}},
        )  # fmt: skip
    return spec.function_arn


def ensure_permission(
    c: TriggerClients, spec: TriggerSpec, report: Report, *, dry_run: bool
) -> None:
    """EventBridge may invoke the function for this rule only."""
    label = f"{spec.resource} <- events.amazonaws.com"
    try:
        policy = json.loads(c.lambda_.get_policy(FunctionName=spec.resource)["Policy"])
        have = {s.get("Sid") for s in policy.get("Statement", [])}
    except Exception as e:
        if not is_not_found(e):
            raise
        have = set()
    if PERMISSION_SID in have:
        report.add("lambda permission", label, "exists")
        return
    if not dry_run:
        c.lambda_.add_permission(
            FunctionName=spec.resource, StatementId=PERMISSION_SID,
            Action="lambda:InvokeFunction", Principal="events.amazonaws.com",
            SourceArn=spec.rule_arn, SourceAccount=spec.account,
        )  # fmt: skip
    report.add("lambda permission", label, "would-create" if dry_run else "created")


def ensure_rule(
    c: TriggerClients, spec: TriggerSpec, report: Report, *, dry_run: bool, halted: bool
) -> str:
    """The rule and its target. A new rule is enabled unless the deployment is halted (stopped,
    capped or expired); a redeploy keeps an existing rule's state (resume enables it)."""
    d, ev, name = spec.deployment, c.events, spec.resource
    got = _rule(ev, name)
    pattern = json.dumps(spec.event_pattern(), sort_keys=True)
    if got is None:
        state = "DISABLED" if halted else "ENABLED"
        outcome: Outcome = "would-create" if dry_run else "created"
        if not dry_run:
            ev.put_rule(
                Name=name, EventPattern=pattern, State=state,
                Description=f"aegc trigger {spec.name} of deployment {d}",
                Tags=naming.tag_list(_tags(spec, "trigger-rule")),
            )  # fmt: skip
        detail = state.lower()
    else:
        if not naming.is_owned(name, _rule_tags(ev, str(got["Arn"])), d):
            raise OwnershipError(f"EventBridge rule {name} exists but isn't tagged as {d}'s")
        state = str(got.get("State") or "ENABLED")
        same = json.loads(str(got.get("EventPattern") or "{}")) == spec.event_pattern()
        outcome = "exists" if same else ("would-update" if dry_run else "updated")
        if not same and not dry_run:
            ev.put_rule(Name=name, EventPattern=pattern, State=state,
                        Description=f"aegc trigger {spec.name} of deployment {d}")  # fmt: skip
        detail = state.lower() + ("" if same else ", new filter")
    report.add("eventbridge rule", name, outcome, detail)
    if not dry_run:
        r = ev.put_targets(Rule=name, Targets=[{"Id": TARGET_ID, "Arn": spec.function_arn}])
        if r.get("FailedEntryCount"):
            raise RuntimeError(f"rule {name}: couldn't set its target: {r.get('FailedEntries')}")
    return spec.rule_arn


def enable_bucket_delivery(s3: Any, bucket: str, report: Report, *, dry_run: bool) -> None:
    """Turn on the bucket's EventBridge delivery, keeping every other notification (read-merge).
    The bucket isn't aegc's: nothing else changes, and nothing ever turns it off."""
    current = dict(s3.get_bucket_notification_configuration(Bucket=bucket))
    current.pop("ResponseMetadata", None)
    if "EventBridgeConfiguration" in current:
        report.add("s3 bucket notifications", bucket, "exists", "EventBridge delivery on")
        return
    if not dry_run:
        s3.put_bucket_notification_configuration(
            Bucket=bucket,
            NotificationConfiguration={**current, "EventBridgeConfiguration": {}},
            # Only EventBridge is added: the bucket's other destinations aren't re-validated.
            SkipDestinationValidation=True,
        )
    kept = ", ".join(k.removesuffix("Configurations") for k in sorted(current)) or "none"
    report.add("s3 bucket notifications", bucket, "would-update" if dry_run else "updated",
               f"EventBridge delivery on; kept: {kept}")  # fmt: skip


def write_spec(c: TriggerClients, spec: TriggerSpec, report: Report, *, dry_run: bool) -> None:
    d, name = spec.deployment, naming.trigger_parameter(spec.deployment, spec.name)
    value = json.dumps(spec.to_dict(), sort_keys=True)
    try:
        c.ssm.get_parameter(Name=name)
        found = naming.tag_dict(
            c.ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
        )
        if not naming.is_owned(f"{naming.PREFIX}-{d}", found, d):
            raise OwnershipError(f"SSM parameter {name} exists but isn't tagged as {d}'s")
        if not dry_run:
            c.ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        report.add("ssm parameter", name, "would-update" if dry_run else "updated")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if not dry_run:
            c.ssm.put_parameter(Name=name, Value=value, Type="String",
                                Tags=naming.tag_list(_tags(spec, "trigger")))  # fmt: skip
        report.add("ssm parameter", name, "would-create" if dry_run else "created")


def specs(ssm: Any, deployment: str) -> dict[str, TriggerSpec]:
    """The deployment's triggers, from ``/aegc/<d>/triggers/*`` (unreadable ones skipped)."""
    out: dict[str, TriggerSpec] = {}
    for p in paginate(ssm, "get_parameters_by_path", "Parameters",
                      Path=naming.trigger_parameter(deployment)):  # fmt: skip
        try:
            s = TriggerSpec.from_dict(json.loads(p["Value"]))
        except Exception:
            continue
        if s.deployment == deployment:
            out[s.name] = s
    return out


def rule_names(ssm: Any, deployment: str) -> dict[str, str]:
    """Trigger name -> rule name, for the lease (:attr:`aegc.lease.LeaseSpec.rules`)."""
    return {n: s.resource for n, s in specs(ssm, deployment).items()}


def update_lease(
    c: TriggerClients, deployment: str, report: Report, *, dry_run: bool,
    rules: dict[str, str],
) -> None:  # fmt: skip
    """Give the deployment's lease (and TTL) a ``*-trigger-<name>`` schedule per trigger."""
    if c.scheduler is None:
        return
    from aegc import lease
    from aegc.infra.deployment import read_manifest

    m = read_manifest(c.ssm, deployment)
    spec = lease.spec_from_manifest(m, rules)
    if not spec.lease_s and not spec.ttl_at:
        return
    lease.ensure_lease(scheduler=c.scheduler, iam=c.iam, spec=spec, report=report,
                       dry_run=dry_run)  # fmt: skip


def create(c: TriggerClients, spec: TriggerSpec, *, package: bytes | None, dry_run: bool) -> Report:
    """Create or update a trigger. Needs the deployment (its manifest) and the stored login
    (``aegc trigger login``) first. Safe to run again."""
    from aegc import state
    from aegc.infra.deployment import read_manifest

    d = spec.deployment
    report = Report()
    try:
        m = read_manifest(c.ssm, d)
    except Exception as e:
        if is_not_found(e):
            raise ValueError(f"deployment {d} isn't deployed (no manifest)") from None
        raise
    if str(m.get("region", spec.region)) != spec.region:
        raise ValueError(f"deployment {d} is in {m.get('region')}, not {spec.region}")
    if c.secretsmanager is not None:
        found = secrets._describe(c.secretsmanager, spec.resource)
        if found is None:
            if not dry_run:
                raise ValueError(
                    f"no stored login for trigger {spec.name}: run `aegc trigger login {d} "
                    f"--name {spec.name} --flow {spec.flow_id} --client-id ...` first"
                )
            report.add("secret", spec.resource, "kept", "missing: run `aegc trigger login`")
        elif not naming.is_owned(spec.resource, naming.tag_dict(found.get("Tags")), d):
            raise OwnershipError(f"secret {spec.resource} exists but isn't tagged as {d}'s")
        elif f"(flow {spec.flow_id})" not in str(found.get("Description", "")):
            raise ValueError(
                f"the stored login of trigger {spec.name} is for another flow; run `aegc trigger "
                f"login {d} --name {spec.name} --flow {spec.flow_id}` again"
            )
        else:
            report.add("secret", spec.resource, "exists", "the stored Globus login")
    halted = not state.read_state(c.ssm, d).running
    ensure_dlq(c, spec, report, dry_run=dry_run)
    ensure_role(c, spec, report, dry_run=dry_run)
    ensure_function(c, spec, package, report, dry_run=dry_run)
    ensure_rule(c, spec, report, dry_run=dry_run, halted=halted)
    ensure_permission(c, spec, report, dry_run=dry_run)
    if c.s3 is not None:
        enable_bucket_delivery(c.s3, spec.bucket, report, dry_run=dry_run)
    write_spec(c, spec, report, dry_run=dry_run)
    update_lease(c, d, report, dry_run=dry_run,
                 rules={**rule_names(c.ssm, d), spec.name: spec.resource})  # fmt: skip
    return report


# --- delete / destroy ---------------------------------------------------------------------------


def _foreign(tags: dict[str, str], d: str) -> bool:
    """Another aegc deployment's (deployment ``d-x`` shares the ``aegc-d-`` prefix): skipped
    quietly, not reported as a look-alike."""
    return tags.get(naming.TAG_MANAGED) == "true" and tags.get(naming.TAG_DEPLOYMENT) != d


def _delete_rule(ev: Any, name: str, arn: str, d: str, report: Report, *, dry_run: bool) -> None:
    tags = _rule_tags(ev, arn)
    if _foreign(tags, d):
        return
    if not naming.is_owned(name, tags, d):
        report.add("eventbridge rule", name, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        ids = [t["Id"] for t in paginate(ev, "list_targets_by_rule", "Targets", Rule=name)]
        if ids:
            ev.remove_targets(Rule=name, Ids=ids)
        ev.delete_rule(Name=name)
    report.add("eventbridge rule", name, "would-delete" if dry_run else "deleted")


def _delete_function(lam: Any, f: dict[str, Any], d: str, report: Report, *, dry_run: bool) -> None:
    name = str(f["FunctionName"])
    tags = naming.tag_dict(lam.list_tags(Resource=f["FunctionArn"]).get("Tags"))
    if _foreign(tags, d):
        return
    if not naming.is_owned(name, tags, d):
        report.add("lambda function", name, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        try:
            lam.delete_function(FunctionName=name)  # its permission and settings go with it
        except Exception as e:
            if not is_not_found(e):
                raise
    report.add("lambda function", name, "would-delete" if dry_run else "deleted")


def _delete_queue(sqs: Any, url: str, d: str, report: Report, *, dry_run: bool) -> None:
    name = url.rsplit("/", 1)[-1]
    tags = _queue_tags(sqs, url)
    if _foreign(tags, d):
        return
    if not naming.is_owned(name, tags, d):
        report.add("sqs queue", name, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        sqs.delete_queue(QueueUrl=url)
    report.add("sqs queue", name, "would-delete" if dry_run else "deleted")


def _delete_role(iam_: Any, name: str, d: str, report: Report, *, dry_run: bool) -> None:
    tags = _role_tags(iam_, name)
    if tags is None or _foreign(tags, d):
        return
    if not naming.is_owned(name, tags, d) or tags.get(naming.TAG_ROLE) != "trigger-role":
        report.add("iam role", name, "kept", "not tagged as aegc's trigger role")
        return
    if not dry_run:
        for p in list(paginate(iam_, "list_role_policies", "PolicyNames", RoleName=name)):
            iam_.delete_role_policy(RoleName=name, PolicyName=p)
        iam_.delete_role(RoleName=name)
    report.add("iam role", name, "would-delete" if dry_run else "deleted")


def _keep_bucket(report: Report, bucket: str) -> None:
    report.add("s3 bucket notifications", bucket, "kept",
               "not aegc's bucket: EventBridge delivery stays on")  # fmt: skip


def delete(c: TriggerClients, deployment: str, name: str, region: str, *, dry_run: bool) -> Report:
    """Delete one trigger (dry run unless ``dry_run=False``): rule, function (and its
    permission), role, DLQ, login, spec and lease schedules. The bucket's notifications stay."""
    d = naming.validate_name("deployment", deployment)
    naming.validate_name("trigger", name)
    report = Report()
    resource = naming.trigger_resource(d, name)
    spec = specs(c.ssm, d).get(name)
    got = _rule(c.events, resource)
    if got is not None:
        _delete_rule(c.events, resource, str(got["Arn"]), d, report, dry_run=dry_run)
    f = _function(c.lambda_, resource)
    if f is not None:
        _delete_function(c.lambda_, dict(f["Configuration"]), d, report, dry_run=dry_run)
    _delete_role(c.iam, naming.trigger_role(d, name, region), d, report, dry_run=dry_run)
    if (url := _queue_url(c.sqs, naming.trigger_dlq(d, name))) is not None:
        _delete_queue(c.sqs, url, d, report, dry_run=dry_run)
    if c.secretsmanager is not None:
        secrets.delete_owned_secret(c.secretsmanager, resource, d, report, dry_run=dry_run)
    param = naming.trigger_parameter(d, name)
    if spec is not None:
        if not dry_run:
            c.ssm.delete_parameter(Name=param)
        report.add("ssm parameter", param, "would-delete" if dry_run else "deleted")
        _keep_bucket(report, spec.bucket)
        rest = {n: r for n, r in rule_names(c.ssm, d).items() if n != name}
        update_lease(c, d, report, dry_run=dry_run, rules=rest)
    return report


def destroy(
    c: TriggerClients, d: str, region: str, report: Report, *, dry_run: bool, queues: bool = True
) -> None:
    """All of the deployment's triggers, found by name prefix and tags (``aegc destroy``). The
    specs and lease schedules go with the deployment's parameters and schedule group, and its
    queue scan (``queues=False``) takes the DLQs."""
    prefix = naming.trigger_prefix(d)
    buckets = sorted({s.bucket for s in specs(c.ssm, d).values()})
    for name, arn in _list_rules(c.events, prefix).items():
        _delete_rule(c.events, name, arn, d, report, dry_run=dry_run)
    for f in _list_functions(c.lambda_, prefix):
        _delete_function(c.lambda_, f, d, report, dry_run=dry_run)
    for role in _list_roles(c.iam, prefix, region):
        _delete_role(c.iam, role, d, report, dry_run=dry_run)
    for url in _list_queues(c.sqs, prefix) if queues else []:
        _delete_queue(c.sqs, url, d, report, dry_run=dry_run)
    if c.secretsmanager is not None:
        for s, owned in secrets.owned_secrets(c.secretsmanager, prefix, d):
            if owned or not _foreign(naming.tag_dict(s.get("Tags")), d):
                secrets.delete_owned_secret(c.secretsmanager, str(s["Name"]), d, report,
                                            dry_run=dry_run)  # fmt: skip
    for b in buckets:
        _keep_bucket(report, b)


def _list_rules(ev: Any, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    kw: dict[str, Any] = {"NamePrefix": prefix}
    while True:
        r = ev.list_rules(**kw)
        out.update({str(x["Name"]): str(x["Arn"]) for x in r.get("Rules", [])})
        if not r.get("NextToken"):
            return out
        kw["NextToken"] = r["NextToken"]


def _list_functions(lam: Any, prefix: str) -> list[dict[str, Any]]:
    return [dict(f) for f in paginate(lam, "list_functions", "Functions")
            if str(f["FunctionName"]).startswith(prefix)]  # fmt: skip


def _list_roles(iam_: Any, prefix: str, region: str) -> list[str]:
    return [
        str(r["RoleName"])
        for r in paginate(iam_, "list_roles", "Roles", PathPrefix=naming.IAM_PATH)
        if str(r["RoleName"]).startswith(prefix) and str(r["RoleName"]).endswith(f"-{region}")
    ]


def _list_queues(sqs: Any, prefix: str) -> list[str]:
    return [u for u in paginate(sqs, "list_queues", "QueueUrls", page_size=1000,
                                QueueNamePrefix=prefix) if u.endswith("-dlq")]  # fmt: skip


def leftovers(
    c: TriggerClients, d: str, region: str, *, queues: bool = True
) -> list[tuple[str, str]]:
    """``(kind, name)`` of the deployment's trigger resources still there (verify)."""
    prefix = naming.trigger_prefix(d)
    left = [("eventbridge rule", n) for n, a in _list_rules(c.events, prefix).items()
            if naming.is_owned(n, _rule_tags(c.events, a), d)]  # fmt: skip
    for f in _list_functions(c.lambda_, prefix):
        tags = naming.tag_dict(c.lambda_.list_tags(Resource=f["FunctionArn"]).get("Tags"))
        if naming.is_owned(str(f["FunctionName"]), tags, d):
            left.append(("lambda function", str(f["FunctionName"])))
    for role in _list_roles(c.iam, prefix, region):
        if naming.is_owned(role, _role_tags(c.iam, role) or {}, d):
            left.append(("iam role", role))
    for url in _list_queues(c.sqs, prefix) if queues else []:
        if naming.is_owned(url.rsplit("/", 1)[-1], _queue_tags(c.sqs, url), d):
            left.append(("sqs queue", url.rsplit("/", 1)[-1]))
    if c.secretsmanager is not None:
        found = secrets.owned_secrets(c.secretsmanager, prefix, d)
        left += [("secret", str(s["Name"])) for s, owned in found if owned]
    return left


def footprint(c: TriggerClients, fp: Any, d: str) -> None:
    """Add the deployment's trigger rules and logins to an :class:`aegc.ops.Footprint`, with
    look-alikes (aegc's names without its tags), keyed as the tagging API's ARNs are."""
    prefix = naming.trigger_prefix(d)
    for name, arn in _list_rules(c.events, prefix).items():
        tags = _rule_tags(c.events, arn)
        if not _foreign(tags, d):
            fp.add("events rule", name, name, owned=naming.is_owned(name, tags, d),
                   found_by="name")  # fmt: skip
    if c.secretsmanager is not None:
        for s, owned in secrets.owned_secrets(c.secretsmanager, prefix, d):
            if owned or not _foreign(naming.tag_dict(s.get("Tags")), d):
                key = str(s["ARN"]).split(":", 6)[-1]
                fp.add("secretsmanager secret", key, str(s["Name"]), owned=owned,
                       found_by="name")  # fmt: skip


# --- list ---------------------------------------------------------------------------------------


@dataclass
class TriggerRow:
    name: str
    rule_state: str
    spec: TriggerSpec | None
    function: str
    invocations: int | None = None  # last 24 h
    errors: int | None = None
    dead_letters: int | None = None

    def __str__(self) -> str:
        s = self.spec
        where = f"s3://{s.bucket}/{s.prefix}*{s.suffix} -> flow {s.flow_id}" if s else "no spec"
        stats = []
        if self.invocations is not None:
            stats.append(f"{self.invocations} run(s), {self.errors or 0} error(s) in 24 h")
        if self.dead_letters is not None:
            stats.append(f"{self.dead_letters} in the DLQ")
        tail = f" [{'; '.join(stats)}]" if stats else ""
        return f"{self.name:<16} {self.rule_state or 'no rule':<9} {where}{tail}"


def list_triggers(c: TriggerClients, d: str, *, now: float | None = None) -> list[TriggerRow]:
    """Each trigger with its rule's state and (given a CloudWatch client) the function's
    invocations and errors in the last 24 h, from one GetMetricData call."""
    from datetime import UTC, datetime

    found = specs(c.ssm, d)
    states = triggers.rules(c.events, d)
    names = sorted(set(found) | {r.removeprefix(naming.trigger_prefix(d)) for r in states})
    rows = []
    for n in names:
        resource = naming.trigger_resource(d, n)
        rows.append(TriggerRow(n, states.get(resource, ""), found.get(n),
                               resource if _function(c.lambda_, resource) else ""))  # fmt: skip
        if (url := _queue_url(c.sqs, naming.trigger_dlq(d, n))) is not None:
            a = c.sqs.get_queue_attributes(
                QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
            )["Attributes"]
            rows[-1].dead_letters = int(a.get("ApproximateNumberOfMessages", 0))
    live = [r for r in rows if r.function]
    if c.cloudwatch is not None and live:
        end = time.time() if now is None else now
        queries = [
            {"Id": f"{m[0].lower()}{i}", "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": "AWS/Lambda", "MetricName": m,
                           "Dimensions": [{"Name": "FunctionName", "Value": r.function}]},
                "Period": 86400, "Stat": "Sum"}}
            for i, r in enumerate(live) for m in ("Invocations", "Errors")
        ]  # fmt: skip
        try:
            res = c.cloudwatch.get_metric_data(
                MetricDataQueries=queries, StartTime=datetime.fromtimestamp(end - 86400, UTC),
                EndTime=datetime.fromtimestamp(end, UTC),
            )  # fmt: skip
            got = {x["Id"]: int(sum(x.get("Values", []))) for x in res["MetricDataResults"]}
            for i, r in enumerate(live):
                r.invocations, r.errors = got.get(f"i{i}", 0), got.get(f"e{i}", 0)
        except Exception as e:
            if error_code(e) not in {"AccessDenied", "AccessDeniedException"}:
                raise
    return rows
