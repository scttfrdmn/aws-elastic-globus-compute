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
        ("sqs:ReceiveMessage", f"{SQS}:aegc-dev-sci-py314-tasks"),  # only workers consume tasks
        ("sqs:DeleteQueue", f"{SQS}:aegc-dev-res-1234567890ab"),
        ("ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/prod/manifest"),
    ]:
        assert decide(c, arn, action, res) != "allowed", (action, res)
    assert decide(c, arn, "autoscaling:SetDesiredCapacity", "*", **tag("dev")) == "allowed"
    assert decide(c, arn, "autoscaling:SetDesiredCapacity", "*", **tag("prod")) != "allowed"


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


def test_policy_dispatch() -> None:
    for kind in iam.POLICIES:
        doc = iam.policy(kind, deployment="dev", region=REGION, account=ACCOUNT)
        assert doc["Version"] == "2012-10-17"
        assert len(json.dumps(doc)) < 10_240, "inline role policies are limited to 10,240 chars"
    with pytest.raises(ValueError):
        iam.policy("admin", deployment="dev", region=REGION, account=ACCOUNT)
