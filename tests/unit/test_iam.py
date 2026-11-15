"""Generated IAM policies, evaluated with the IAM policy simulator (substrate)."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from aegc import naming
from aegc.infra import iam
from tests.conftest import ACCOUNT, REGION

SQS = f"arn:aws:sqs:{REGION}:{ACCOUNT}"
BUCKET = naming.bucket_name(ACCOUNT, REGION)


def principal(aws: Callable[[str], Any], doc: dict[str, Any]) -> tuple[Any, str]:
    c = aws("iam")
    arn = c.create_role(
        RoleName="aegc-test", AssumeRolePolicyDocument=json.dumps(iam.ec2_trust_policy())
    )["Role"]["Arn"]
    c.put_role_policy(RoleName="aegc-test", PolicyName="p", PolicyDocument=json.dumps(doc))
    return c, arn


def decide(c: Any, arn: str, action: str, resource: str, **ctx: str) -> str:
    entries = [
        {"ContextKeyName": k, "ContextKeyValues": [v], "ContextKeyType": "string"}
        for k, v in ctx.items()
    ]
    kw: dict[str, Any] = {"ContextEntries": entries} if entries else {}
    r = c.simulate_principal_policy(
        PolicySourceArn=arn, ActionNames=[action], ResourceArns=[resource], **kw
    )["EvaluationResults"][0]
    return str(r["EvalDecision"])


def tag(v: str) -> dict[str, str]:
    return {"autoscaling:ResourceTag/aegc:deployment": v}


def test_worker_policy(aws: Callable[[str], Any]) -> None:
    c, arn = principal(
        aws, iam.worker_policy(deployment="dev", region=REGION, account=ACCOUNT, bucket=BUCKET)
    )
    allowed = [
        ("sqs:ReceiveMessage", f"{SQS}:aegc-dev-sci-py314-tasks"),
        ("sqs:ChangeMessageVisibility", f"{SQS}:aegc-dev-sci-py314-tasks"),
        ("sqs:SendMessage", f"{SQS}:aegc-dev-res-1234567890ab"),
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/tasks/u/1.task"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/results/u/u:1.result"),
        ("ecr:BatchGetImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/aegc/geospatial"),
    ]
    for action, res in allowed:
        assert decide(c, arn, action, res) == "allowed", (action, res)
    denied = [
        ("sqs:ReceiveMessage", f"{SQS}:aegc-prod-sci-py314-tasks"),  # another deployment
        ("sqs:SendMessage", f"{SQS}:aegc-dev-sci-py314-tasks"),  # workers don't submit
        ("sqs:DeleteQueue", f"{SQS}:aegc-dev-sci-py314-tasks"),
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/prod/tasks/u/1.task"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/tasks/u/1.task"),
        ("ecr:BatchGetImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other/x"),
    ]
    for action, res in denied:
        assert decide(c, arn, action, res) != "allowed", (action, res)
    term = "autoscaling:TerminateInstanceInAutoScalingGroup"
    assert decide(c, arn, term, "*", **tag("dev")) == "allowed"
    assert decide(c, arn, term, "*", **tag("prod")) != "allowed"
    state_param = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/state"
    assert decide(c, arn, "ssm:GetParameter", state_param) == "allowed"
    assert decide(c, arn, "ssm:PutParameter", state_param) != "allowed"  # workers only read it
    ledger_key = f"arn:aws:s3:::{BUCKET}/ledger/dev/2026-11-15/i-1.json"
    assert decide(c, arn, "s3:PutObject", ledger_key) == "allowed"
    assert decide(c, arn, "s3:PutObject", ledger_key.replace("/dev/", "/prod/")) != "allowed"


def test_operator_policy(aws: Callable[[str], Any]) -> None:
    c, arn = principal(
        aws, iam.operator_policy(deployment="dev", region=REGION, account=ACCOUNT, bucket=BUCKET)
    )
    for action, res in [
        ("sqs:SendMessage", f"{SQS}:aegc-dev-sci-py314-tasks"),
        ("sqs:CreateQueue", f"{SQS}:aegc-dev-res-1234567890ab"),
        ("sqs:ReceiveMessage", f"{SQS}:aegc-dev-res-1234567890ab"),
        ("ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/manifest"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/tasks/u/1.task"),
    ]:
        assert decide(c, arn, action, res) == "allowed", (action, res)
    for action, res in [
        ("sqs:SendMessage", f"{SQS}:aegc-prod-sci-py314-tasks"),
        ("sqs:DeleteQueue", f"{SQS}:aegc-dev-res-1234567890ab"),
        ("ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/prod/manifest"),
    ]:
        assert decide(c, arn, action, res) != "allowed", (action, res)
    assert decide(c, arn, "autoscaling:SetDesiredCapacity", "*", **tag("dev")) == "allowed"
    assert decide(c, arn, "autoscaling:SetDesiredCapacity", "*", **tag("prod")) != "allowed"
    assert decide(c, arn, "sqs:ListQueues", "*") == "allowed"  # aegc status (#25)
    # v0.2 spend safety: cap state, ledger, prices, own-task purge, minimum to 0.
    state_param = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/state"
    assert decide(c, arn, "ssm:PutParameter", state_param) == "allowed"
    # write_state's ownership check and create-with-tags (review finding #2):
    assert decide(c, arn, "ssm:ListTagsForResource", state_param) == "allowed"
    assert decide(c, arn, "ssm:AddTagsToResource", state_param) == "allowed"
    assert (
        decide(
            c,
            arn,
            "ssm:PutParameter",
            f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/manifest",
        )
        != "allowed"
    )
    assert (
        decide(c, arn, "s3:GetObject", f"arn:aws:s3:::{BUCKET}/ledger/dev/2026-11-15/i-1.json")
        == "allowed"
    )
    assert (
        decide(c, arn, "s3:GetObject", f"arn:aws:s3:::{BUCKET}/ledger/prod/2026-11-15/i-1.json")
        != "allowed"
    )
    assert decide(c, arn, "ec2:DescribeSpotPriceHistory", "*") == "allowed"
    # Receiving from task queues is allowed since v0.2 so a capped or stopped endpoint can
    # remove its own queued tasks (it deletes only messages whose reply_to is its own queue).
    assert decide(c, arn, "sqs:ReceiveMessage", f"{SQS}:aegc-dev-geo-py314-tasks") == "allowed"
    assert decide(c, arn, "sqs:ReceiveMessage", f"{SQS}:aegc-prod-geo-py314-tasks") != "allowed"
    assert decide(c, arn, "autoscaling:UpdateAutoScalingGroup", "*", **tag("dev")) == "allowed"
    assert decide(c, arn, "autoscaling:UpdateAutoScalingGroup", "*", **tag("prod")) != "allowed"
    sched = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule"
    assert decide(c, arn, "scheduler:UpdateSchedule", f"{sched}/aegc-dev/lease-state") == "allowed"
    assert decide(c, arn, "scheduler:UpdateSchedule", f"{sched}/aegc-prod/lease-state") != "allowed"
    assert decide(c, arn, "scheduler:DeleteSchedule", f"{sched}/aegc-dev/lease-state") != "allowed"
    lease_role = f"arn:aws:iam::{ACCOUNT}:role/aegc/aegc-dev-scheduler-{REGION}"
    passed = {"iam:PassedToService": "scheduler.amazonaws.com"}
    assert decide(c, arn, "iam:PassRole", lease_role, **passed) == "allowed"
    assert (
        decide(c, arn, "iam:PassRole", lease_role.replace("-dev-", "-prod-"), **passed) != "allowed"
    )


def test_deployer_policy(aws: Callable[[str], Any]) -> None:
    c, arn = principal(aws, iam.deployer_policy(region=REGION, account=ACCOUNT))
    role = f"arn:aws:iam::{ACCOUNT}:role/aegc/aegc-dev-worker-{REGION}"
    assert decide(c, arn, "iam:CreateRole", role) == "allowed"
    assert decide(c, arn, "iam:CreateRole", f"arn:aws:iam::{ACCOUNT}:role/admin") != "allowed"
    assert (
        decide(c, arn, "iam:PassRole", role, **{"iam:PassedToService": "ec2.amazonaws.com"})
        == "allowed"
    )
    assert (
        decide(c, arn, "iam:PassRole", role, **{"iam:PassedToService": "lambda.amazonaws.com"})
        != "allowed"
    )
    managed = {"aws:ResourceTag/aegc:managed": "true"}
    assert decide(c, arn, "ec2:DeleteSecurityGroup", "*", **managed) == "allowed"
    assert decide(c, arn, "ec2:DeleteSecurityGroup", "*") != "allowed"
    assert decide(c, arn, "sqs:DeleteQueue", f"{SQS}:aegc-dev-sci-py314-tasks") == "allowed"
    assert decide(c, arn, "sqs:DeleteQueue", f"{SQS}:someone-elses") != "allowed"

    # RunInstances (#25): aegc's launch templates only, and other resources only via a template.
    ec2 = f"arn:aws:ec2:{REGION}:{ACCOUNT}"
    lt = {"ec2:LaunchTemplate": f"{ec2}:launch-template/lt-0123456789abcdef0"}
    assert decide(c, arn, "ec2:RunInstances", f"{ec2}:launch-template/lt-1", **managed) == "allowed"
    assert decide(c, arn, "ec2:RunInstances", f"{ec2}:launch-template/lt-1") != "allowed"
    assert decide(c, arn, "ec2:RunInstances", f"{ec2}:instance/*", **lt) == "allowed"
    assert decide(c, arn, "ec2:RunInstances", f"{ec2}:instance/*") != "allowed"
    slr = f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/autoscaling.amazonaws.com/x"
    for svc, want in [("autoscaling.amazonaws.com", True), ("lambda.amazonaws.com", False)]:
        got = decide(c, arn, "iam:CreateServiceLinkedRole", slr, **{"iam:AWSServiceName": svc})
        assert (got == "allowed") is want, svc

    # In-use image tags (#27): aegc's image repositories only, and never deleting a repository.
    repo = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/aegc/geospatial"
    for action in (
        "ecr:BatchGetImage", "ecr:PutImage", "ecr:BatchDeleteImage", "ecr:DescribeImages",
        "ecr:PutLifecyclePolicy",
    ):  # fmt: skip
        assert decide(c, arn, action, repo) == "allowed", action
    for action, res in [
        ("ecr:PutImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other/x"),
        ("ecr:BatchDeleteImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other/x"),
        ("ecr:BatchDeleteImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/aegc-quay/aarchsci/x"),
        ("ecr:PutLifecyclePolicy", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other/x"),
        ("ecr:SetRepositoryPolicy", repo),
    ]:
        assert decide(c, arn, action, res) != "allowed", (action, res)


def test_builder_policy(aws: Callable[[str], Any]) -> None:
    c, arn = principal(aws, iam.builder_policy(region=REGION, account=ACCOUNT))
    repo = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/aegc/geospatial"
    cache = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/aegc-quay/aarchsci/geospatial"
    for action, res in [
        ("ecr:GetAuthorizationToken", "*"),
        ("ecr:CreateRepository", repo),
        ("ecr:PutImage", repo),
        ("ecr:CompleteLayerUpload", repo),
        ("ecr:PutLifecyclePolicy", repo),
        ("ecr:BatchImportUpstreamImage", cache),
        ("ecr:BatchGetImage", cache),
        ("ssm:PutParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/images/geospatial"),
    ]:
        assert decide(c, arn, action, res) == "allowed", (action, res)
    for action, res in [
        ("ecr:PutImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other"),
        ("ecr:PutImage", cache),  # the cache is read-only for builders
        ("ecr:DeleteRepository", repo),
        ("ssm:PutParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/manifest"),
    ]:
        assert decide(c, arn, action, res) != "allowed", (action, res)


def test_policy_dispatch() -> None:
    for kind in iam.POLICIES:
        doc = iam.policy(kind, deployment="dev", region=REGION, account=ACCOUNT)
        assert doc["Version"] == "2012-10-17"
        assert len(json.dumps(doc)) < 10_240, "inline role policies are limited to 10,240 chars"
        # Customer managed policies: 6,144 characters, whitespace not counted.
        big = iam.policy(kind, deployment="d" * 20, region="ap-southeast-4", account=ACCOUNT)
        assert len(json.dumps(big, separators=(",", ":"))) < 6_144, kind
    with pytest.raises(ValueError):
        iam.policy("admin", deployment="dev", region=REGION, account=ACCOUNT)


def test_bootstrap_and_deployer_split(aws: Callable[[str], Any]) -> None:
    # Managed policies are limited to 6,144 characters, so the region-wide bootstrap actions
    # live in their own policy.
    bucket = f"arn:aws:s3:::{BUCKET}"
    c, boot = principal(aws, iam.bootstrap_policy(region=REGION, account=ACCOUNT))
    assert decide(c, boot, "s3:CreateBucket", bucket) == "allowed"
    assert decide(c, boot, "ecr:CreatePullThroughCacheRule", "*") == "allowed"
    assert decide(c, boot, "autoscaling:CreateAutoScalingGroup", "*") != "allowed"
    c.delete_role_policy(RoleName="aegc-test", PolicyName="p")
    c.put_role_policy(
        RoleName="aegc-test",
        PolicyName="p",
        PolicyDocument=json.dumps(iam.deployer_policy(region=REGION, account=ACCOUNT)),
    )
    assert decide(c, boot, "s3:CreateBucket", bucket) != "allowed"
    assert decide(c, boot, "tag:GetResources", "*") == "allowed"  # aegc footprint
    assert decide(c, boot, "ec2:DescribeNetworkInterfaces", "*") == "allowed"  # destroy --verify
    group = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule-group/aegc-dev"
    assert decide(c, boot, "scheduler:CreateScheduleGroup", group) == "allowed"
    assert (
        decide(c, boot, "scheduler:CreateScheduleGroup", group.replace("aegc-dev", "x"))
        != "allowed"
    )
