"""S3-event triggers (#83): create/delete/destroy, freezing, the handler, the package, IAM."""

from __future__ import annotations

import io
import json
import os
import zipfile
from collections.abc import Callable
from typing import Any

import pytest

from aegc import caps, control, evaluator, lease, naming, state, trigger, triggers
from aegc.infra import OwnershipError, Report, deployment, iam, secrets
from aegc.infra import triggers as infra
from aegc.infra.deployment import Clients
from aegc.spend import Trip
from aegc.state import Limits
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import function_concurrency, lambda_echoes, rule_states
from tests.unit.test_deployment import outcomes
from tests.unit.test_iam import decide, principal

FLOW = "5f2a8a7e-2b4c-4f53-9a43-6a3c7b1e9d10"
OTHER_FLOW = "0b0c7d34-8d55-4a89-9a5c-1d8f61f1b0aa"
CLIENT = "9d1b6e4c-1111-4222-8333-444455556666"
BUCKET = "lab-uploads"
NAME = "t1"
RES = f"aegc-dev-trigger-{NAME}"
ROLE = f"{RES}-{REGION}"
DLQ = f"{RES}-dlq"
TEMPLATE = {"input": {"uri": "s3://{bucket}/{key}", "bytes": "{size}"}, "tag": "{etag}"}


def package(marker: str = "1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("aegc/trigger.py", f"# {marker}\n")
    return buf.getvalue()


def spec(**kw: Any) -> infra.TriggerSpec:
    base: dict[str, Any] = dict(
        deployment="dev",
        region=REGION,
        account=ACCOUNT,
        name=NAME,
        flow_id=FLOW,
        bucket=BUCKET,
        prefix="in/",
        input_template=TEMPLATE,
    )
    return infra.TriggerSpec(**{**base, **kw})


def login(flow: str = FLOW) -> infra.TriggerLogin:
    return infra.TriggerLogin(CLIENT, flow, {flow: {"refresh_token": "RT-secret"}})


@pytest.fixture
def env(aws: Callable[[str], Any]) -> tuple[Clients, dict[str, Any]]:
    lam = aws("lambda")
    lambda_echoes(lam)  # substrate#1659
    ev = aws("events")
    rec = {"rules": rule_states(ev), "reserved": function_concurrency(lam)}
    c = Clients(
        iam=aws("iam"),
        ec2=aws("ec2"),
        autoscaling=aws("autoscaling"),
        sqs=aws("sqs"),
        logs=aws("logs"),
        ssm=aws("ssm"),
        scheduler=aws("scheduler"),
        secretsmanager=aws("secretsmanager"),
        cloudwatch=aws("cloudwatch"),
        lambda_=lam,
        events=ev,
        s3=aws("s3"),
    )
    manifest = {
        "deployment": "dev",
        "region": REGION,
        "account": ACCOUNT,
        "pools": {},
        "lease": {"lease_s": 3 * 3600, "ttl_at": 0},
    }
    c.ssm.put_parameter(
        Name=naming.deployment_manifest_parameter("dev"),
        Value=json.dumps(manifest),
        Type="String",
        Tags=naming.tag_list(naming.tags("dev", "manifest")),
    )
    c.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    # The bucket already notifies a queue of the lab's: create must keep it.
    q = c.sqs.create_queue(QueueName="lab-queue")["QueueUrl"]
    qarn = c.sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]
    rec["lab_queue"] = qarn["QueueArn"]
    c.s3.put_bucket_notification_configuration(
        Bucket=BUCKET,
        NotificationConfiguration={
            "QueueConfigurations": [
                {"Id": "lab", "QueueArn": qarn["QueueArn"], "Events": ["s3:ObjectCreated:*"]}
            ]
        },
    )
    return c, rec


def create(c: Clients, s: infra.TriggerSpec | None = None, **kw: Any) -> Report:
    s = s or spec()
    infra.put_login(
        c.secretsmanager, "dev", s.name, REGION, ACCOUNT, login(s.flow_id), Report(), dry_run=False
    )
    return infra.create(c.triggers(), s, package=kw.pop("package", package()), dry_run=False, **kw)


def rule_state(c: Clients, name: str = RES) -> str:
    return str(c.events.describe_rule(Name=name)["State"])


# --- names, specs, patterns ----------------------------------------------------------------------


def test_trigger_names_are_reserved_and_bounded() -> None:
    for bad in ("trigger", "trigger-a"):
        with pytest.raises(naming.NameError_, match="reserved"):
            naming.validate_name("pool", bad)
    naming.validate_name("pool", "triggered-py314")  # only the word itself is reserved
    assert naming.validate_name("trigger", "a" * 15)
    with pytest.raises(naming.NameError_):
        naming.validate_name("trigger", "a" * 16)
    s = spec(deployment="a" * 20, name="b" * 15, region="ap-southeast-2")
    assert len(s.role) == 64
    assert s.role == naming.trigger_role("a" * 20, "b" * 15, "ap-southeast-2")


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"flow_id": "not-a-uuid"}, "UUID"),
        ({"bucket": "Bad_Bucket"}, "bucket"),
        ({"input_template": {"x": "{path}"}}, "placeholder"),
        ({"input_template": ["x"]}, "JSON object"),
        ({"input_template": {"x": "y" * 4000}}, "environment"),
    ],
)
def test_spec_refuses(kw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        spec(**kw)


def test_event_pattern_ands_prefix_and_suffix() -> None:
    base = {"source": ["aws.s3"], "detail-type": ["Object Created"]}
    assert spec(prefix="").event_pattern() == {**base, "detail": {"bucket": {"name": [BUCKET]}}}
    assert spec().event_pattern()["detail"]["object"] == {"key": [{"prefix": "in/"}]}
    assert spec(prefix="", suffix=".csv").event_pattern()["detail"]["object"] == {
        "key": [{"suffix": ".csv"}]
    }
    both = spec(prefix="a*b/", suffix=".csv").event_pattern()["detail"]["object"]
    assert both == {"key": [{"wildcard": "a\\*b/*.csv"}]}


# --- create -------------------------------------------------------------------------------------


def test_create_makes_everything_and_is_idempotent(env: tuple[Clients, dict[str, Any]]) -> None:
    c, rec = env
    r = outcomes(create(c))
    assert r[("secret", RES)] == "exists"
    for key in (
        ("sqs queue", DLQ),
        ("iam role", ROLE),
        ("lambda function", RES),
        ("eventbridge rule", RES),
        ("ssm parameter", f"/aegc/dev/triggers/{NAME}"),
    ):
        assert r[key] == "created", key
    assert r[("s3 bucket notifications", BUCKET)] == "updated"

    # The rule: S3 Object Created on the bucket and prefix, tagged, targeting the function.
    rule = c.events.describe_rule(Name=RES)
    assert rule["State"] == "ENABLED"
    assert json.loads(rule["EventPattern"]) == spec().event_pattern()
    tags = naming.tag_dict(c.events.list_tags_for_resource(ResourceARN=rule["Arn"])["Tags"])
    assert naming.is_owned(RES, tags, "dev") and tags["aegc:role"] == "trigger-rule"
    targets = c.events.list_targets_by_rule(Rule=RES)["Targets"]
    assert [(t["Id"], t["Arn"]) for t in targets] == [(infra.TARGET_ID, spec().function_arn)]

    # The function: a small arm64 zip on python3.13, with its settings and only events may call.
    cfg = c.lambda_.get_function(FunctionName=RES)["Configuration"]
    assert (cfg["PackageType"], cfg["Runtime"], cfg["Handler"], cfg["Architectures"]) == (
        "Zip",
        "python3.13",
        "aegc.trigger.handler",
        ["arm64"],
    )
    assert cfg["Role"] == spec().role_arn
    env_ = cfg["Environment"]["Variables"]
    assert env_["AEGC_FLOW_ID"] == FLOW and env_["AEGC_TRIGGER_SECRET"] == RES
    assert json.loads(env_["AEGC_TRIGGER_INPUT"]) == TEMPLATE
    assert cfg["CodeSha256"] == infra.code_sha256(package())
    assert rec["reserved"] == {RES: infra.RESERVED_CONCURRENCY}
    policy = json.loads(c.lambda_.get_policy(FunctionName=RES)["Policy"])
    (st,) = policy["Statement"]
    assert st["Sid"] == infra.PERMISSION_SID
    assert st["Principal"] == {"Service": "events.amazonaws.com"}
    assert st["Condition"]["ArnLike"]["AWS:SourceArn"] == spec().rule_arn

    # The role: trusted by Lambda in this account only; its one policy.
    role = c.iam.get_role(RoleName=ROLE)["Role"]
    assert role["Path"] == "/aegc/"
    trust = role["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    assert trust["Statement"][0]["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    assert c.iam.list_role_policies(RoleName=ROLE)["PolicyNames"] == [iam.TRIGGER_POLICY]

    # The DLQ, the spec, the bucket (lab's queue notification kept), the lease schedule.
    url = c.sqs.get_queue_url(QueueName=DLQ)["QueueUrl"]
    assert naming.is_owned(DLQ, c.sqs.list_queue_tags(QueueUrl=url)["Tags"], "dev")
    assert infra.specs(c.ssm, "dev") == {NAME: spec()}
    n = c.s3.get_bucket_notification_configuration(Bucket=BUCKET)
    assert n["EventBridgeConfiguration"] == {}
    assert [q["QueueArn"] for q in n["QueueConfigurations"]] == [rec["lab_queue"]]
    s = c.scheduler.get_schedule(Name=f"lease-trigger-{NAME}", GroupName="aegc-dev")
    assert (
        s["Target"]["Arn"]
        == lease.EVENTS_TARGET
        == ("arn:aws:scheduler:::aws-sdk:eventbridge:disableRule")
    )
    assert json.loads(s["Target"]["Input"]) == {"Name": RES}

    # Again: nothing changes.
    again = outcomes(infra.create(c.triggers(), spec(), package=package(), dry_run=False))
    for key in (
        ("sqs queue", DLQ),
        ("iam role", ROLE),
        ("lambda function", RES),
        ("eventbridge rule", RES),
        ("lambda permission", f"{RES} <- events.amazonaws.com"),
        ("s3 bucket notifications", BUCKET),
    ):
        assert again[key] == "exists", key
    # A new filter and a new package update in place; a stopped rule stays stopped.
    c.events.disable_rule(Name=RES)
    r = infra.create(c.triggers(), spec(prefix="new/"), package=package("2"), dry_run=False)
    assert outcomes(r)[("eventbridge rule", RES)] == "updated"
    assert outcomes(r)[("lambda function", RES)] == "updated"
    (fn,) = [a for a in r.actions if a.resource == "lambda function"]
    assert fn.detail.startswith("code")
    assert rule_state(c) == "DISABLED"
    assert c.lambda_.get_function(FunctionName=RES)["Configuration"]["CodeSha256"] == (
        infra.code_sha256(package("2"))
    )


def test_dry_run_changes_nothing(env: tuple[Clients, dict[str, Any]]) -> None:
    c, _ = env
    r = infra.create(c.triggers(), spec(), package=None, dry_run=True)
    assert outcomes(r)[("secret", RES)] == "kept"  # no login yet: said, not refused
    assert {a.outcome for a in r.actions} <= {"would-create", "would-update", "kept"}
    assert c.events.list_rules(NamePrefix="aegc-")["Rules"] == []
    assert "EventBridgeConfiguration" not in c.s3.get_bucket_notification_configuration(
        Bucket=BUCKET
    )


def test_create_needs_the_login_for_that_flow(env: tuple[Clients, dict[str, Any]]) -> None:
    c, _ = env
    with pytest.raises(ValueError, match="trigger login"):
        infra.create(c.triggers(), spec(), package=package(), dry_run=False)
    infra.put_login(
        c.secretsmanager, "dev", NAME, REGION, ACCOUNT, login(OTHER_FLOW), Report(), dry_run=False
    )
    with pytest.raises(ValueError, match="another flow"):
        infra.create(c.triggers(), spec(), package=package(), dry_run=False)


def test_login_secret_is_tagged_and_readable_by_the_function_only(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    c, _ = env
    r = Report()
    infra.put_login(c.secretsmanager, "dev", NAME, REGION, ACCOUNT, login(), r, dry_run=False)
    infra.put_login(c.secretsmanager, "dev", NAME, REGION, ACCOUNT, login(), r, dry_run=False)
    assert [a.outcome for a in r.actions] == ["created", "exists"]
    d = secrets._describe(c.secretsmanager, RES)
    assert d is not None and naming.tag_dict(d["Tags"])["aegc:role"] == "trigger-login"
    policy = json.loads(c.secretsmanager.get_resource_policy(SecretId=RES)["ResourcePolicy"])
    assert policy == secrets.reader_policy(spec().role_arn, sid="OnlyTheTriggerFunction")
    value = json.loads(c.secretsmanager.get_secret_value(SecretId=RES)["SecretString"])
    assert value["client_id"] == CLIENT and value["tokens"][FLOW]["refresh_token"] == "RT-secret"
    # The endpoint's secret helper still works on the same generalized code.
    assert secrets.exists(c.secretsmanager, "dev") is False


def test_ownership_is_checked_before_anything_changes(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    c, _ = env
    c.secretsmanager.create_secret(Name=RES, SecretString="{}")  # someone else's
    with pytest.raises(OwnershipError):
        infra.put_login(
            c.secretsmanager, "dev", NAME, REGION, ACCOUNT, login(), Report(), dry_run=False
        )
    c.secretsmanager.delete_secret(SecretId=RES, ForceDeleteWithoutRecovery=True)
    c.events.put_rule(Name=RES, EventPattern=json.dumps({"source": ["x"]}))  # untagged
    with pytest.raises(OwnershipError, match="rule"):
        create(c)
    assert json.loads(c.events.describe_rule(Name=RES)["EventPattern"]) == {"source": ["x"]}


def test_halted_deployment_gets_a_disabled_rule(env: tuple[Clients, dict[str, Any]]) -> None:
    c, _ = env
    state.write_state(c.ssm, "dev", state.State(mode="stopped", reason="t", since=1, by="t"))
    assert outcomes(create(c))[("eventbridge rule", RES)] == "created"
    assert rule_state(c) == "DISABLED"


# --- freezing -----------------------------------------------------------------------------------


def test_stop_resume_caps_lease_and_evaluator_follow_the_rules(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    c, rec = env
    create(c)
    create(c, spec(name="t2"))
    both = {RES, "aegc-dev-trigger-t2"}
    assert set(triggers.rules(c.events, "dev")) == both

    r = control.stop(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", events=c.events)
    assert f"  trigger {RES}: rule disabled" in r.lines
    assert {rule_state(c, n) for n in both} == {"DISABLED"}
    r = control.resume(ssm=c.ssm, autoscaling=c.autoscaling, deployment="dev", events=c.events)
    assert f"  trigger {RES}: rule enabled" in r.lines
    assert {rule_state(c, n) for n in both} == {"ENABLED"}

    caps.trip(
        ssm=c.ssm,
        autoscaling=c.autoscaling,
        deployment="dev",
        cap=Trip("over $1/day", until=0.0),
        limits=Limits(),
        groups={},
        by="test",
        events=c.events,
    )
    assert {rule_state(c, n) for n in both} == {"DISABLED"}

    # The evaluator: capped -> stays disabled; running again -> enabled.
    clients = {
        "ssm": c.ssm,
        "s3": c.s3,
        "autoscaling": c.autoscaling,
        "scheduler": c.scheduler,
        "sqs": c.sqs,
        "events": c.events,
    }
    ev = evaluator.evaluate(deployment="dev", region=REGION, clients=clients)
    assert ev.state == "capped" and ev.rules == {}
    state.write_state(c.ssm, "dev", state.State(reason="t", since=1, by="t"))
    dry = evaluator.evaluate(deployment="dev", region=REGION, clients=clients, dry_run=True)
    assert dry.rules == dict.fromkeys(both, "enabled") and "would enable trigger rule" in (
        dry.render()
    )
    assert {rule_state(c, n) for n in both} == {"DISABLED"}
    ev = evaluator.evaluate(deployment="dev", region=REGION, clients=clients)
    assert ev.rules == dict.fromkeys(both, "enabled") and not ev.errors
    assert {rule_state(c, n) for n in both} == {"ENABLED"}
    # Stopped while the evaluator enabled: frozen again at once (confirm re-reads the state).
    c.events.disable_rule(Name=RES)
    calls = iter([True, False])
    _, changes = triggers.reconcile(c.events, "dev", True, confirm=lambda: next(calls))
    assert changes == {RES: "disabled"} and rule_state(c) == "DISABLED"

    # The lease: one schedule per trigger, and firing it disables the rule.
    c.events.enable_rule(Name=RES)
    rules = infra.rule_names(c.ssm, "dev")
    assert rules == {NAME: RES, "t2": "aegc-dev-trigger-t2"}
    lease.fire(c.ssm, c.autoscaling, "dev", {}, events=c.events, rules=rules)
    assert {rule_state(c, n) for n in both} == {"DISABLED"}
    assert [x["Name"] for x in rec["rules"]][-2:] == [RES, "aegc-dev-trigger-t2"]


def test_lease_schedules_and_role_cover_triggers() -> None:
    s = lease.LeaseSpec(
        deployment="dev",
        region=REGION,
        account=ACCOUNT,
        groups={},
        rules={NAME: RES},
        lease_s=3 * 3600,
        ttl_at=2e9,
    )
    got = lease.schedules(s, "arn:role", now=1e9)
    for prefix in ("lease", "ttl"):
        t = got[f"{prefix}-trigger-{NAME}"]["Target"]
        assert (t["Arn"], json.loads(t["Input"])) == (lease.EVENTS_TARGET, {"Name": RES})
    _, policy = lease._role_documents(s)
    (st,) = [x for x in policy["Statement"] if x["Action"] == "events:DisableRule"]
    assert st["Resource"] == f"arn:aws:events:{REGION}:{ACCOUNT}:rule/aegc-dev-trigger-*"
    assert st["Condition"] == {"StringEquals": {"aws:ResourceTag/aegc:deployment": "dev"}}


def test_redeploy_keeps_the_trigger_lease_schedules(env: tuple[Clients, dict[str, Any]]) -> None:
    c, _ = env
    create(c)
    assert deployment.trigger_rules(c, "dev") == {NAME: RES}
    m = deployment.read_manifest(c.ssm, "dev")
    r = Report()
    lease.ensure_lease(
        scheduler=c.scheduler,
        iam=c.iam,
        report=r,
        dry_run=False,
        spec=lease.spec_from_manifest(m, deployment.trigger_rules(c, "dev")),
    )
    assert ("schedule", f"lease-trigger-{NAME}", "updated") in {
        (a.resource, a.name, a.outcome) for a in r.actions
    }


# --- delete / destroy / verify / footprint ------------------------------------------------------


def test_delete_is_a_dry_run_by_default_and_keeps_the_bucket(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    c, _ = env
    create(c)
    dry = outcomes(infra.delete(c.triggers(), "dev", NAME, REGION, dry_run=True))
    assert dry[("eventbridge rule", RES)] == "would-delete"
    assert rule_state(c) == "ENABLED"
    r = outcomes(infra.delete(c.triggers(), "dev", NAME, REGION, dry_run=False))
    for key in (
        ("eventbridge rule", RES),
        ("lambda function", RES),
        ("iam role", ROLE),
        ("sqs queue", DLQ),
        ("secret", RES),
        ("ssm parameter", f"/aegc/dev/triggers/{NAME}"),
    ):
        assert r[key] == "deleted", key
    assert r[("s3 bucket notifications", BUCKET)] == "kept"
    assert r[("schedule", f"lease-trigger-{NAME}")] == "deleted"
    n = c.s3.get_bucket_notification_configuration(Bucket=BUCKET)
    assert "EventBridgeConfiguration" in n and n["QueueConfigurations"]
    assert infra.leftovers(c.triggers(), "dev", REGION) == []


def test_destroy_removes_triggers_and_verifies(env: tuple[Clients, dict[str, Any]]) -> None:
    c, _ = env
    create(c)
    # Look-alikes: an untagged rule with aegc's name; another deployment's rule with the prefix.
    c.events.put_rule(Name="aegc-dev-trigger-stray", EventPattern=json.dumps({"source": ["x"]}))
    c.events.put_rule(
        Name="aegc-dev-trigger-x-y",
        EventPattern=json.dumps({"source": ["x"]}),
        Tags=naming.tag_list(naming.tags("dev-trigger-x", "trigger-rule")),
    )
    left = {(k, n) for k, n in infra.leftovers(c.triggers(), "dev", REGION)}
    assert {
        ("eventbridge rule", RES),
        ("lambda function", RES),
        ("iam role", ROLE),
        ("sqs queue", DLQ),
        ("secret", RES),
    } == left

    dry = outcomes(deployment.destroy(c, deployment="dev", region=REGION))
    assert dry[("eventbridge rule", RES)] == "would-delete"
    assert dry[("eventbridge rule", "aegc-dev-trigger-stray")] == "kept"
    assert ("eventbridge rule", "aegc-dev-trigger-x-y") not in dry
    assert dry[("s3 bucket notifications", BUCKET)] == "kept"
    done = deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=0, poll_s=0)
    r = outcomes(done)
    for key in (
        ("eventbridge rule", RES),
        ("lambda function", RES),
        ("iam role", ROLE),
        ("sqs queue", DLQ),
        ("secret", RES),
    ):
        assert r[key] == "deleted", key
    assert deployment.remaining(done) == []
    assert deployment.verify_destroyed(c, deployment="dev", region=REGION, wait_s=0).actions == []
    assert sorted(x["Name"] for x in c.events.list_rules(NamePrefix="aegc-")["Rules"]) == [
        "aegc-dev-trigger-stray",
        "aegc-dev-trigger-x-y",
    ]
    assert "EventBridgeConfiguration" in c.s3.get_bucket_notification_configuration(Bucket=BUCKET)


def test_destroy_without_trigger_permissions_reports_remaining(
    env: tuple[Clients, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ClientError

    c, _ = env
    create(c)

    def denied(*a: Any, **k: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListRules"
        )

    monkeypatch.setattr(infra, "destroy", denied)
    r = deployment.destroy(c, deployment="dev", region=REGION)
    assert outcomes(r)[("triggers", "dev")] == "remaining"
    assert any(a.detail == deployment.TRIGGERS_DENIED for a in r.actions)


def test_footprint_lists_rules_and_logins_with_look_alikes(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    from aegc import ops

    c, _ = env
    create(c)
    c.events.put_rule(Name="aegc-dev-trigger-stray", EventPattern=json.dumps({"source": ["x"]}))
    fp = ops.Footprint("dev", REGION)
    infra.footprint(c.triggers(), fp, "dev")
    got = {(i.kind, i.name): i.owned for i in fp.items.values()}
    assert got[("events rule", RES)] is True
    assert got[("events rule", "aegc-dev-trigger-stray")] is False
    assert got[("secretsmanager secret", RES)] is True


def test_list_shows_rule_state_and_dlq(env: tuple[Clients, dict[str, Any]]) -> None:
    c, _ = env
    create(c)
    c.events.disable_rule(Name=RES)
    (row,) = infra.list_triggers(c.triggers(), "dev")
    assert (row.name, row.rule_state, row.function, row.dead_letters) == (NAME, "DISABLED", RES, 0)
    assert f"s3://{BUCKET}/in/* -> flow {FLOW}" in str(row)


def test_cli_trigger_create_dry_run_list_and_delete(
    env: tuple[Clients, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from aegc import cli

    c, _ = env
    monkeypatch.setattr(cli, "_clients", lambda sess: c)
    monkeypatch.setattr(infra, "build_package", lambda: package())
    base = ["--region", REGION, "trigger"]
    args = [
        "dev",
        "--name",
        NAME,
        "--flow",
        FLOW,
        "--bucket",
        BUCKET,
        "--prefix",
        "in/",
        "--input",
        json.dumps(TEMPLATE),
    ]
    with pytest.raises(SystemExit, match="client-id"):
        cli.main([*base, "login", "dev", "--name", NAME, "--flow", FLOW])
    with pytest.raises(SystemExit, match="placeholder"):
        cli.main([*base, "create", *args[:-1], '{"x": "{nope}"}'])
    assert cli.main([*base, "create", *args, "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    infra.put_login(
        c.secretsmanager, "dev", NAME, REGION, ACCOUNT, login(), Report(), dry_run=False
    )
    assert cli.main([*base, "create", *args]) == 0
    assert cli.main([*base, "list", "dev"]) == 0
    assert f"{NAME:<16} ENABLED" in capsys.readouterr().out
    assert cli.main([*base, "delete", "dev", "--name", NAME]) == 0
    assert "pass --yes" in capsys.readouterr().out
    assert cli.main([*base, "delete", "dev", "--name", NAME, "--yes"]) == 0
    assert infra.specs(c.ssm, "dev") == {}


# --- the handler --------------------------------------------------------------------------------


def s3_event(key: str = "in/sample 1.csv") -> dict[str, Any]:
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": BUCKET},
            "object": {"key": key, "size": 1234, "etag": "abc123"},
        },
    }


class FakeFlows:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def run_flow(self, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.calls.append({"body": body, **kw})
        if self.fail:
            raise self.fail
        return {"run_id": "run-1"}


@pytest.fixture
def handler_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"flows": FakeFlows()}
    for k, v in spec().environment().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(trigger, "_LOGIN", None)
    monkeypatch.setattr(trigger, "_secret_value", lambda name: login().to_json())

    def client(flow_id: str, stored: dict[str, Any]) -> FakeFlows:
        seen["flow_id"], seen["login"] = flow_id, stored
        return seen["flows"]  # type: ignore[no-any-return]

    monkeypatch.setattr(trigger, "_flow_client", client)
    return seen


def test_handler_renders_the_input_and_starts_the_flow(
    handler_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert trigger.handler(s3_event()) == {"run_id": "run-1"}
    (call,) = handler_env["flows"].calls
    assert call["body"] == {
        "input": {"uri": f"s3://{BUCKET}/in/sample 1.csv", "bytes": 1234},
        "tag": "abc123",
    }
    assert call["label"] == "s3:in/sample 1.csv"
    assert call["tags"] == ["aegc-trigger", "aegc:dev", f"trigger:{NAME}"]
    assert handler_env["flow_id"] == FLOW and handler_env["login"]["client_id"] == CLIENT
    line = json.loads(capsys.readouterr().out.strip())
    assert (line["event"], line["run_id"]) == ("run_started", "run-1")


def test_handler_raises_on_failure_without_logging_tokens(
    handler_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    handler_env["flows"].fail = RuntimeError("403 forbidden")
    with pytest.raises(RuntimeError, match="403"):
        trigger.handler(s3_event())
    out = capsys.readouterr().out
    assert json.loads(out)["event"] == "run_failed"
    assert "RT-secret" not in out
    with pytest.raises(ValueError, match="Object Created"):
        trigger.handler({"source": "aws.ec2", "detail-type": "x"})


def test_label_and_render_edges() -> None:
    long = "k" * 100
    assert len(trigger.label(long)) == trigger.LABEL_MAX and trigger.label(long).endswith("k")
    assert trigger.render(
        {"n": "{size}", "s": "{size} bytes", "{key}": [1, "{key}"]}, {"size": "5", "key": "a"}
    ) == {"n": 5, "s": "5 bytes", "{key}": [1, "a"]}


def test_storage_is_seeded_from_the_secret(capsys: pytest.CaptureFixture[str]) -> None:
    from globus_sdk.token_storage import TokenStorageData

    td = TokenStorageData(
        resource_server=FLOW,
        identity_id="u",
        scope="s",
        access_token="AT",
        refresh_token="RT",
        expires_at_seconds=1,
        token_type="Bearer",
    )
    stored = {"client_id": CLIENT, "tokens": {FLOW: td.to_dict()}}
    storage = trigger._storage(stored)
    assert storage.get_token_data(FLOW).refresh_token == "RT"
    # A refresh that brings a new refresh token is kept in memory and logged (not the token).
    new = TokenStorageData(**{**td.to_dict(), "access_token": "AT2", "refresh_token": "RT2"})
    storage.store_token_data_by_resource_server({FLOW: new})
    out = capsys.readouterr().out
    assert "refresh_token_changed" in out and "RT2" not in out


def test_flow_client_refuses_an_interactive_login() -> None:
    with pytest.raises(trigger.LoginExpired, match="trigger login"):
        trigger._no_interactive_login(None, RuntimeError("x"))


# --- the package --------------------------------------------------------------------------------


def test_build_package_vendors_globus_sdk_deterministically(tmp_path: Any) -> None:
    commands: list[list[str]] = []

    def fake_pip(cmd: list[str], **_: Any) -> Any:
        commands.append(cmd)
        target = cmd[cmd.index("--target") + 1]
        os.makedirs(os.path.join(target, "globus_sdk", "__pycache__"))
        with open(os.path.join(target, "globus_sdk", "__init__.py"), "w") as f:
            f.write("__version__ = '4'\n")
        with open(os.path.join(target, "globus_sdk", "__pycache__", "x.pyc"), "w") as f:
            f.write("")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    a = infra.build_package(run=fake_pip, workdir=str(tmp_path))
    b = infra.build_package(run=fake_pip, workdir=str(tmp_path))
    assert a == b and infra.code_sha256(a) == infra.code_sha256(b)
    names = zipfile.ZipFile(io.BytesIO(a)).namelist()
    assert sorted(names) == ["aegc/__init__.py", "aegc/trigger.py", "globus_sdk/__init__.py"]
    cmd = commands[0]
    assert "--only-binary=:all:" in cmd and cmd[-1].startswith("globus-sdk==4.")
    assert any("aarch64" in x for x in cmd) and "3.13" in cmd
    # The handler module is the whole of aegc in the package: it imports nothing else of aegc.
    src = zipfile.ZipFile(io.BytesIO(a)).read("aegc/trigger.py").decode()
    assert "from aegc" not in src and "import aegc" not in src

    def failing(cmd: list[str], **_: Any) -> Any:
        return type("R", (), {"returncode": 1, "stderr": "no matching distribution"})()

    with pytest.raises(RuntimeError, match="no matching"):
        infra.build_package(run=failing, workdir=str(tmp_path))


# --- IAM ----------------------------------------------------------------------------------------


SECRETS = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret"
RULES = f"arn:aws:events:{REGION}:{ACCOUNT}:rule"


def test_trigger_role_reads_only_its_secret(aws: Callable[[str], Any]) -> None:
    c, arn = principal(
        aws, iam.trigger_policy(deployment="dev", name=NAME, region=REGION, account=ACCOUNT)
    )
    assert decide(c, arn, "secretsmanager:GetSecretValue", f"{SECRETS}:{RES}-AbCdEf") == "allowed"
    for other in (
        f"{SECRETS}:aegc-dev-trigger-t2-AbCdEf",
        f"{SECRETS}:aegc-dev-endpoint-AbCdEf",
        f"{SECRETS}:{RES}-x-AbCdEf",
    ):
        assert decide(c, arn, "secretsmanager:GetSecretValue", other) != "allowed", other
    sqs = f"arn:aws:sqs:{REGION}:{ACCOUNT}"
    assert decide(c, arn, "sqs:SendMessage", f"{sqs}:{DLQ}") == "allowed"
    assert decide(c, arn, "sqs:SendMessage", f"{sqs}:aegc-dev-sci-py314-tasks") != "allowed"
    assert decide(c, arn, "s3:GetObject", f"arn:aws:s3:::{BUCKET}/in/x") != "allowed"
    logs = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aegc/dev:log-stream:x"
    assert decide(c, arn, "logs:PutLogEvents", logs) == "allowed"


def test_operator_and_endpoint_freeze_only_their_own_rules(aws: Callable[[str], Any]) -> None:
    bucket = naming.bucket_name(ACCOUNT, REGION)
    for doc in (
        iam.operator_policy(deployment="dev", region=REGION, account=ACCOUNT, bucket=bucket),
        iam.endpoint_role_policy(deployment="dev", region=REGION, account=ACCOUNT, bucket=bucket),
    ):
        c, arn = principal(aws, doc, f"p{len(json.dumps(doc))}")
        ours = {"aws:ResourceTag/aegc:deployment": "dev"}
        for action in ("events:DisableRule", "events:EnableRule"):
            assert decide(c, arn, action, f"{RULES}/{RES}", **ours) == "allowed"
            assert decide(c, arn, action, f"{RULES}/{RES}") != "allowed"  # untagged
            assert decide(c, arn, action, f"{RULES}/aegc-prod-trigger-x", **ours) != "allowed"
            assert (
                decide(
                    c, arn, action, f"{RULES}/{RES}", **{"aws:ResourceTag/aegc:deployment": "prod"}
                )
                != "allowed"
            )
        assert decide(c, arn, "events:ListRules", "*") == "allowed"
        assert decide(c, arn, "events:DeleteRule", f"{RULES}/{RES}", **ours) != "allowed"


def test_lambda_deployer_covers_triggers(aws: Callable[[str], Any]) -> None:
    c, arn = principal(aws, iam.lambda_deployer_policy(region=REGION, account=ACCOUNT))
    req = {"aws:RequestTag/aegc:managed": "true"}
    res = {"aws:ResourceTag/aegc:managed": "true"}
    assert decide(c, arn, "events:PutRule", f"{RULES}/{RES}", **req) == "allowed"
    assert decide(c, arn, "events:PutRule", f"{RULES}/someones-rule", **req) != "allowed"
    assert decide(c, arn, "events:DeleteRule", f"{RULES}/{RES}", **res) == "allowed"
    assert decide(c, arn, "events:DeleteRule", f"{RULES}/{RES}") != "allowed"
    fn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{RES}"
    assert decide(c, arn, "lambda:AddPermission", fn, **res) == "allowed"
    assert decide(c, arn, "lambda:PutFunctionConcurrency", fn, **res) == "allowed"
    mine = {"s3:ResourceAccount": ACCOUNT}
    assert decide(c, arn, "s3:PutBucketNotification", f"arn:aws:s3:::{BUCKET}", **mine) == "allowed"
    # Review: this account's buckets only.
    theirs = {"s3:ResourceAccount": "999999999999"}
    assert decide(c, arn, "s3:PutBucketNotification", "arn:aws:s3:::x", **theirs) != "allowed"
    secret = f"{SECRETS}:{RES}-AbCdEf"
    assert decide(c, arn, "secretsmanager:CreateSecret", secret, **req) == "allowed"
    assert decide(c, arn, "secretsmanager:GetSecretValue", secret, **res) != "allowed"
    assert decide(c, arn, "sqs:CreateQueue", f"arn:aws:sqs:{REGION}:{ACCOUNT}:{DLQ}") == "allowed"


def test_policies_stay_under_the_managed_policy_limit() -> None:
    kw = {"region": "ap-southeast-2", "account": ACCOUNT}
    d = "a" * 20
    bucket = naming.bucket_name(ACCOUNT, "ap-southeast-2")
    for doc in (
        iam.lambda_deployer_policy(**kw),
        iam.operator_policy(deployment=d, bucket=bucket, **kw),
        iam.endpoint_role_policy(deployment=d, bucket=bucket, session_manager=True, **kw),
        iam.trigger_policy(deployment=d, name="b" * 15, **kw),
    ):
        assert iam.policy_size(doc) < 6_144


# --- review fixes ----------------------------------------------------------------------------


def test_a_flow_writing_into_the_watched_prefix_is_refused(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    c, _ = env
    loop = spec(input_template={"src": "s3://{bucket}/{key}", "out": f"s3://{BUCKET}/in/results/"})
    assert infra.watched_outputs(loop) == [f"s3://{BUCKET}/in/results/"]
    with pytest.raises(ValueError, match="starts it again"):
        create(c, loop)
    assert (
        outcomes(create(c, loop, allow_self_trigger=True))[("eventbridge rule", RES)] == "created"
    )
    assert infra.watched_outputs(spec(input_template={"out": f"s3://{BUCKET}/out/"})) == []


def test_a_bucket_in_another_region_is_refused(
    env: tuple[Clients, dict[str, Any]], aws: Callable[[str], Any]
) -> None:
    import boto3

    c, _ = env
    east = boto3.client("s3", region_name="us-east-1")
    east.create_bucket(Bucket="lab-east")
    with pytest.raises(ValueError, match="is in us-east-1"):
        create(c, spec(bucket="lab-east"))


def test_rules_of_a_look_alike_deployment_are_left_alone(
    env: tuple[Clients, dict[str, Any]],
) -> None:
    # Review: deployment "dev-trigger"'s rule "aegc-dev-trigger-trigger-x" shares dev's prefix.
    from aegc import triggers

    c, _ = env
    create(c)
    other = "aegc-dev-trigger-trigger-x"
    tags = naming.tags("dev-trigger", "trigger-rule")
    c.events.put_rule(Name=other, EventPattern='{"source": ["aws.s3"]}', State="ENABLED",
                      Tags=[{"Key": k, "Value": v} for k, v in tags.items()])  # fmt: skip
    assert set(triggers.rules(c.events, "dev")) == {RES}
