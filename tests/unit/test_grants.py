"""Worker data-access grants (#53): validation, the aegc-grants policy, deploy and the CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from aegc import cli, naming
from aegc.infra import deployment, iam
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from aegc.infra.iam import Grant
from tests.conftest import ACCOUNT, REGION
from tests.unit.test_deployment import env as env  # the fixture
from tests.unit.test_deployment import outcomes
from tests.unit.test_iam import decide, principal

ROLE = naming.worker_role("dev", REGION)


def obj(bucket: str, key: str) -> str:
    return f"arn:aws:s3:::{bucket}/{key}"


# --- validation --------------------------------------------------------------------------------


def test_parse_grants() -> None:
    assert Grant.parse("read=s3://open-data/in/") == Grant("read", "open-data", "in/")
    assert Grant.parse("write=s3://my.lab-bucket/out/run1") == Grant(
        "write", "my.lab-bucket", "out/run1"
    )
    whole = Grant.parse("read=s3://open-data/")  # the slash says "the whole bucket"
    assert whole.prefix == "" and whole.uri == "s3://open-data/"
    assert Grant.from_dict(whole.to_dict()) == whole


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("read=s3://open-data", "whole bucket"),  # no slash: ambiguous
        ("read=https://open-data.s3.amazonaws.com/in/", "s3://"),
        ("read=/local/path", "s3://"),
        ("admin=s3://open-data/in/", "read=s3://"),
        ("s3://open-data/in/", "read=s3://"),
        ("read=s3:///in/", "no bucket"),
        ("read=s3://*/", "wildcards"),
        ("read=s3://open-*/in/", "wildcards"),
        ("write=s3://open-data/out/*", "wildcards"),
        ("write=s3://open-data/out/?", "wildcards"),
        ("write=s3://open-data/${aws:username}/", "wildcards"),
        ("read=s3://Open-Data/in/", "bucket name"),
        ("read=s3://ab/in/", "bucket name"),
        ("read=s3://a..b/in/", "bucket name"),
        ("read=s3://192.168.1.1/in/", "bucket name"),
        ("read=s3://data-ap--ol-s3/in/", "bucket name"),
        (f"read=s3://{naming.bucket_name(ACCOUNT, REGION)}/payloads/", "aegc's own bucket"),
        (f"write=s3://{naming.bucket_name(ACCOUNT, REGION)}/", "aegc's own bucket"),
    ],
)
def test_bad_grants_are_refused(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Grant.parse(text)


def test_too_many_grants_fail_before_anything_changes() -> None:
    many = tuple(Grant("read", f"bucket-{i:04d}", f"some/longish/prefix/{i}/") for i in range(200))
    with pytest.raises(ValueError, match="at most 32"):  # user data holds them too
        iam.grants_policy(many, deployment="dev")
    # Few grants with long prefixes can still overflow IAM's inline-policy budget.
    long = tuple(Grant("read", f"bucket-{i:04d}", "p/" * 150 + f"{i}/") for i in range(32))
    with pytest.raises(ValueError, match="10240"):
        iam.grants_policy(long, deployment="dev")
    with pytest.raises(ValueError, match="room for"):
        DeploymentSpec(
            deployment="dev",
            account=ACCOUNT,
            region=REGION,
            bucket=naming.bucket_name(ACCOUNT, REGION),
            pools=(),
            grants=long[:24],  # fits alone, not next to aegc-worker
        )
    with pytest.raises(ValueError, match="no grants"):
        iam.grants_policy((), deployment="dev")


# --- the policy, in the IAM simulator ----------------------------------------------------------


def test_grants_policy(aws: Callable[[str], Any]) -> None:
    grants = (
        Grant.parse("read=s3://open-data/in/"),
        Grant.parse("read=s3://open-data/ref/"),
        Grant.parse("read=s3://whole-bucket/"),
        Grant.parse("write=s3://lab-results/out/"),
        Grant.parse("read=s3://open-data/in/"),  # a duplicate collapses
    )
    doc = iam.grants_policy(grants, deployment="dev")
    assert iam.policy_size(doc) < iam.INLINE_POLICY_LIMIT
    c, arn = principal(aws, doc)
    allowed = [
        ("s3:GetObject", obj("open-data", "in/a/b.tif")),
        ("s3:GetObjectVersion", obj("open-data", "in/a/b.tif")),
        ("s3:GetObject", obj("open-data", "ref/genome.fa")),
        ("s3:GetObject", obj("whole-bucket", "anything/at/all")),
        ("s3:PutObject", obj("lab-results", "out/task/result.parquet")),
        ("s3:AbortMultipartUpload", obj("lab-results", "out/task/result.parquet")),
        ("s3:ListMultipartUploadParts", obj("lab-results", "out/task/result.parquet")),
    ]
    for action, res in allowed:
        assert decide(c, arn, action, res) == "allowed", (action, res)
    denied = [
        ("s3:GetObject", obj("open-data", "secret/x")),  # outside the prefix
        ("s3:GetObject", obj("other-bucket", "in/a/b.tif")),  # another bucket
        ("s3:PutObject", obj("open-data", "in/a/b.tif")),  # read grants don't write
        ("s3:DeleteObject", obj("open-data", "in/a/b.tif")),
        ("s3:PutObject", obj("lab-results", "elsewhere/x")),  # outside the write prefix
        ("s3:PutObject", obj("other-bucket", "out/x")),
        ("s3:GetObject", obj("lab-results", "out/task/result.parquet")),  # write isn't read
        ("s3:DeleteObject", obj("lab-results", "out/task/result.parquet")),
        ("s3:PutObject", obj("whole-bucket", "x")),
        ("s3:PutBucketPolicy", "arn:aws:s3:::open-data"),
    ]
    for action, res in denied:
        assert decide(c, arn, action, res) != "allowed", (action, res)

    listing = "s3:ListBucket"
    assert decide(c, arn, listing, "arn:aws:s3:::open-data", **{"s3:prefix": "in/2024/"}) == (
        "allowed"
    )
    assert decide(c, arn, listing, "arn:aws:s3:::open-data", **{"s3:prefix": "ref/"}) == "allowed"
    assert decide(c, arn, listing, "arn:aws:s3:::open-data", **{"s3:prefix": "secret/"}) != (
        "allowed"
    )
    assert decide(c, arn, listing, "arn:aws:s3:::open-data") != "allowed"  # no prefix: the root
    assert decide(c, arn, listing, "arn:aws:s3:::whole-bucket") == "allowed"
    assert decide(c, arn, listing, "arn:aws:s3:::other-bucket", **{"s3:prefix": "in/"}) != (
        "allowed"
    )
    assert decide(c, arn, listing, "arn:aws:s3:::lab-results", **{"s3:prefix": "out/"}) != (
        "allowed"
    )


def test_deployer_can_manage_the_grants_policy(aws: Callable[[str], Any]) -> None:
    c, arn = principal(aws, iam.deployer_policy(region=REGION, account=ACCOUNT))
    role = f"arn:aws:iam::{ACCOUNT}:role/aegc/{ROLE}"
    for action in ("iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies"):
        assert decide(c, arn, action, role) == "allowed", action
        assert decide(c, arn, action, f"arn:aws:iam::{ACCOUNT}:role/admin") != "allowed", action


# --- deploy and destroy ------------------------------------------------------------------------


WORKER_GRANTS = f"{iam.GRANTS_POLICY} (worker role)"
TASK_GRANTS = f"{iam.GRANTS_POLICY} (task role)"
TASK_ROLE = "aegc-dev-task-us-west-2"


def role_policies(c: Clients, role: str = ROLE) -> list[str]:
    return sorted(c.iam.list_role_policies(RoleName=role)["PolicyNames"])


def worker_config_of(c: Clients) -> dict[str, Any]:
    import base64
    import re

    data = c.ec2.describe_launch_template_versions(
        LaunchTemplateName="aegc-dev-sci-py314", Versions=["$Default"]
    )["LaunchTemplateVersions"][0]["LaunchTemplateData"]
    script = base64.b64decode(data["UserData"]).decode()
    m = re.search(r"AEGC_WORKER_CONFIG_B64=([A-Za-z0-9+/=]+)", script)
    assert m, "worker config not found in user data"
    return dict(json.loads(base64.b64decode(m.group(1))))


def test_deploy_puts_redeploy_clears_destroy_removes(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    grants = (Grant.parse("read=s3://open-data/in/"), Grant.parse("write=s3://lab/out/"))
    with_grants = DeploymentSpec(**{**spec.__dict__, "grants": grants})
    r = outcomes(deployment.deploy(c, with_grants))
    # POOL's image predates 0.4, so its tasks still run with the worker role: both roles get them.
    assert r[("iam role policy", WORKER_GRANTS)] == "created"
    assert r[("iam role policy", TASK_GRANTS)] == "created"
    assert role_policies(c) == [iam.GRANTS_POLICY, iam.WORKER_POLICY]
    doc = c.iam.get_role_policy(RoleName=ROLE, PolicyName=iam.GRANTS_POLICY)["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    assert doc == iam.grants_policy(grants, deployment="dev", account=ACCOUNT)
    write = next(st for st in doc["Statement"] if st["Sid"] == "GrantWrite")
    assert write["Condition"] == {"StringEquals": {"s3:ResourceAccount": ACCOUNT}}
    m = deployment.read_manifest(c.ssm, "dev")
    assert m["grants"] == [
        {"access": "read", "bucket": "open-data", "prefix": "in/"},
        {"access": "write", "bucket": "lab", "prefix": "out/"},
    ]
    assert worker_config_of(c)["grants"] == m["grants"]

    # Redeploying without grants (--grant none) deletes the policy, and says so once.
    r = outcomes(deployment.deploy(c, spec))
    assert r[("iam role policy", WORKER_GRANTS)] == "deleted"
    assert r[("iam role policy", TASK_GRANTS)] == "deleted"
    assert role_policies(c) == [iam.WORKER_POLICY]
    assert deployment.read_manifest(c.ssm, "dev")["grants"] == []
    assert "grants" not in worker_config_of(c)
    r = outcomes(deployment.deploy(c, spec))
    assert ("iam role policy", WORKER_GRANTS) not in r

    deployment.deploy(c, with_grants)
    assert iam.GRANTS_POLICY in role_policies(c)
    done = outcomes(
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=30, poll_s=0.1)
    )
    assert done[("iam role", ROLE)] == "deleted"
    with pytest.raises(c.iam.exceptions.NoSuchEntityException):
        c.iam.get_role_policy(RoleName=ROLE, PolicyName=iam.GRANTS_POLICY)


def test_spec_accepts_manifest_dicts() -> None:
    spec = DeploymentSpec(
        deployment="dev",
        account=ACCOUNT,
        region=REGION,
        bucket=naming.bucket_name(ACCOUNT, REGION),
        pools=(),
        grants=({"access": "write", "bucket": "lab", "prefix": "out/"},),  # type: ignore[arg-type]
    )
    assert spec.grants == (Grant("write", "lab", "out/"),)


# --- the CLI merge -----------------------------------------------------------------------------


def merge(given: list[str] | None, prev: dict[str, Any]) -> tuple[Grant, ...]:
    return cli.deploy_grants(given, prev, deployment="dev", account=ACCOUNT, region=REGION)


def test_cli_grants_merge_with_the_previous_manifest() -> None:
    prev = {"grants": [{"access": "read", "bucket": "open-data", "prefix": "in/"}]}
    assert merge(None, prev) == (Grant("read", "open-data", "in/"),)  # kept
    assert merge([], prev) == (Grant("read", "open-data", "in/"),)
    assert merge(None, {}) == ()  # a first deploy, or a manifest from before grants
    assert merge(["none"], prev) == ()
    assert merge(["write=s3://lab/out/", "read=s3://x-bucket/a/", "write=s3://lab/out/"], prev) == (
        Grant("write", "lab", "out/"),
        Grant("read", "x-bucket", "a/"),
    )  # replaced, not added to


@pytest.mark.parametrize(
    ("given", "match"),
    [
        (["none", "read=s3://open-data/in/"], "don't combine"),
        (["read=s3://open-data"], "whole bucket"),
        (["read=s3://*/"], "wildcards"),
        ([f"read=s3://{naming.bucket_name(ACCOUNT, REGION)}/"], "aegc's own bucket"),
        ([f"read=s3://bucket-{i:04d}/some/longish/prefix/" for i in range(150)], "at most 32"),
    ],
)
def test_cli_grant_errors(given: list[str], match: str) -> None:
    with pytest.raises(SystemExit, match=match):
        merge(given, {})


def test_cli_parses_grant_flags() -> None:
    args = cli.build_parser().parse_args(
        ["deploy", "dev", "--pool", "sci", "--grant", "read=s3://a-b/c/", "--grant", "none"]
    )
    assert args.grant == ["read=s3://a-b/c/", "none"]
    assert cli.build_parser().parse_args(["deploy", "dev", "--pool", "sci"]).grant is None


def test_isolated_pools_keep_grants_off_the_worker_role(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    # #32: tasks of 0.4 images get the task role; the worker role (queues, payloads, ledger,
    # Auto Scaling) never holds the grants.
    from dataclasses import asdict

    from tests.unit.test_deployment import POOL

    c, spec = env
    pool = PoolSpec(**{**asdict(POOL), "isolate_tasks": True})
    grants = (Grant.parse("read=s3://open-data/in/"), Grant.parse("write=s3://lab/out/"))
    iso = DeploymentSpec(**{**spec.__dict__, "pools": (pool,), "grants": grants})
    deployment.deploy(c, iso)
    assert role_policies(c) == [iam.WORKER_POLICY]
    assert role_policies(c, TASK_ROLE) == [iam.GRANTS_POLICY, iam.TASK_POLICY]
    trust = c.iam.get_role(RoleName=TASK_ROLE)["Role"]["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    (st,) = trust["Statement"]
    assert st["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:role/aegc/{ROLE}"}
    cfg = worker_config_of(c)
    assert cfg["task_role_arn"].endswith(f"/{TASK_ROLE}") and cfg["task_uid"] == 57439
    done = outcomes(
        deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=30, poll_s=0.1)
    )
    assert done[("iam role", TASK_ROLE)] == "deleted"


def test_task_role_can_only_use_its_grants(aws: Callable[[str], Any]) -> None:
    # The IAM simulator: the task role reaches its grants and the manifest, nothing of the
    # worker's (task queues, result queues, payloads, ledger, Auto Scaling).
    c = aws("iam")
    policy = iam.task_policy(deployment="dev", region=REGION, account=ACCOUNT)
    grants = iam.grants_policy(
        (Grant.parse("write=s3://lab/out/"),), deployment="dev", account=ACCOUNT
    )
    docs = [json.dumps(policy), json.dumps(grants)]

    def allowed(action: str, resource: str) -> bool:
        r = c.simulate_custom_policy(
            PolicyInputList=docs,
            ActionNames=[action],
            ResourceArns=[resource],
            ContextEntries=[
                {
                    "ContextKeyName": "s3:ResourceAccount",
                    "ContextKeyValues": [ACCOUNT],
                    "ContextKeyType": "string",
                }
            ],
        )["EvaluationResults"][0]
        return bool(r["EvalDecision"] == "allowed")

    sqs = f"arn:aws:sqs:{REGION}:{ACCOUNT}"
    bucket = naming.bucket_name(ACCOUNT, REGION)
    assert allowed("s3:PutObject", "arn:aws:s3:::lab/out/x")
    assert allowed(
        "ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/manifest"
    )
    for action, resource in (
        ("sqs:ReceiveMessage", f"{sqs}:aegc-dev-sci-py314-tasks"),
        ("sqs:SendMessage", f"{sqs}:aegc-dev-res-abc"),
        ("s3:GetObject", f"arn:aws:s3:::{bucket}/payloads/dev/tasks/t/1.task"),
        ("s3:PutObject", f"arn:aws:s3:::{bucket}/ledger/dev/x.json"),
        ("autoscaling:SetDesiredCapacity", "*"),
        ("ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/state"),
    ):
        assert not allowed(action, resource), action


def test_worker_role_keeps_grants_while_pre_04_instances_run(
    env: tuple[Clients, DeploymentSpec],
) -> None:
    # Review: rebuilding every image to 0.4 and redeploying mustn't pull the grants from
    # instances still running the old images (their tasks use the worker role).
    from dataclasses import asdict

    from tests.unit.test_deployment import ASG, POOL

    c, spec = env
    grants = (Grant.parse("read=s3://open-data/in/"),)
    old = DeploymentSpec(**{**spec.__dict__, "grants": grants})
    deployment.deploy(c, old)
    c.autoscaling.set_desired_capacity(
        AutoScalingGroupName=ASG, DesiredCapacity=2
    )  # one old worker
    pool = PoolSpec(**{**asdict(POOL), "isolate_tasks": True})
    new = DeploymentSpec(**{**old.__dict__, "pools": (pool,)})
    r = outcomes(deployment.deploy(c, new))
    assert r[("iam role policy", WORKER_GRANTS)] == "kept"
    assert iam.GRANTS_POLICY in role_policies(c)
    # The old worker leaves; the next deploy removes them.
    g = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])
    for i in g["AutoScalingGroups"][0]["Instances"]:
        c.ec2.terminate_instances(InstanceIds=[i["InstanceId"]])
    c.autoscaling.set_desired_capacity(AutoScalingGroupName=ASG, DesiredCapacity=0)
    r = outcomes(deployment.deploy(c, new))
    assert r[("iam role policy", WORKER_GRANTS)] == "deleted"
    assert role_policies(c) == [iam.WORKER_POLICY]


def test_isolated_launch_templates(env: tuple[Clients, DeploymentSpec]) -> None:
    from dataclasses import asdict

    from tests.unit.test_deployment import POOL

    c, spec = env
    pool = PoolSpec(**{**asdict(POOL), "isolate_tasks": True})
    deployment.deploy(c, DeploymentSpec(**{**spec.__dict__, "pools": (pool,)}))
    data = c.ec2.describe_launch_template_versions(
        LaunchTemplateName="aegc-dev-sci-py314", Versions=["$Default"]
    )["LaunchTemplateVersions"][0]["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpProtocolIpv6"] == "disabled"
    tags = {t["Key"]: t["Value"] for ts in data["TagSpecifications"] for t in ts["Tags"]}
    assert tags[deployment.TAG_ISOLATED] == "true"
