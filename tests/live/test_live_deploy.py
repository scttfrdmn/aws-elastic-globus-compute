"""Live: deploy and destroy a throwaway deployment in a real account. No instances are launched.

AEGC_LIVE=1 AWS_PROFILE=aws pytest -m live tests/live/test_live_deploy.py
"""

from __future__ import annotations

import os
import secrets
import time

import boto3
import pytest

from aegc import naming
from aegc.infra import deployment
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AEGC_LIVE") != "1", reason="set AEGC_LIVE=1"),
    pytest.mark.timeout(900),
]


def test_live_deploy_idempotent_destroy_leaves_nothing() -> None:
    sess = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"))
    region = sess.region_name or "us-west-2"
    account = sess.client("sts").get_caller_identity()["Account"]
    c = Clients(
        iam=sess.client("iam"),
        ec2=sess.client("ec2"),
        autoscaling=sess.client("autoscaling"),
        sqs=sess.client("sqs"),
        logs=sess.client("logs"),
        ssm=sess.client("ssm"),
    )
    d = f"ci{secrets.token_hex(3)}"
    image = f"{account}.dkr.ecr.{region}.amazonaws.com/aegc/ci@sha256:" + "0" * 64
    spec = DeploymentSpec(
        deployment=d,
        account=account,
        region=region,
        bucket=naming.bucket_name(account, region),
        pools=(
            PoolSpec(name="ci-py314", env="ci", python_minor="3.14", image=image, max_workers=1),
        ),
    )
    try:
        first = deployment.deploy(c, spec)
        print(first)
        t0 = time.monotonic()
        again = deployment.deploy(c, spec)
        elapsed = time.monotonic() - t0
        assert "created" not in {a.outcome for a in again.actions}, str(again)
        assert elapsed < 10, f"re-deploy took {elapsed:.1f} s"
        g = c.autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[naming.auto_scaling_group(d, "ci-py314")]
        )["AutoScalingGroups"][0]
        assert g["DesiredCapacity"] == 0
        assert {p["ProcessName"] for p in g["SuspendedProcesses"]} == {"AZRebalance"}
    finally:
        print(deployment.destroy(c, deployment=d, region=region, dry_run=False, force=True))

    tagging = sess.client("resourcegroupstaggingapi")
    left = [
        r["ResourceARN"]
        for page in tagging.get_paginator("get_resources").paginate(
            TagFilters=[{"Key": naming.TAG_DEPLOYMENT, "Values": [d]}]
        )
        for r in page["ResourceTagMappingList"]
    ]
    # The tagging API lags deletes; re-check each by its own API.
    still = [a for a in left if _exists(c, a)]
    assert still == [], still
    with pytest.raises(c.iam.exceptions.NoSuchEntityException):
        c.iam.get_role(RoleName=naming.worker_role(d, region))


def _exists(c: Clients, arn: str) -> bool:
    if ":sqs:" in arn:
        try:
            c.sqs.get_queue_url(QueueName=arn.rsplit(":", 1)[-1])
            return True
        except c.sqs.exceptions.QueueDoesNotExist:
            return False
    if ":launch-template/" in arn:
        lts = c.ec2.describe_launch_templates(
            Filters=[{"Name": "launch-template-id", "Values": [arn.rsplit("/", 1)[-1]]}]
        )["LaunchTemplates"]
        return bool(lts)
    if ":security-group/" in arn:
        sgs = c.ec2.describe_security_groups(
            Filters=[{"Name": "group-id", "Values": [arn.rsplit("/", 1)[-1]]}]
        )["SecurityGroups"]
        return bool(sgs)
    if ":autoScalingGroup:" in arn:
        name = arn.rsplit("/", 1)[-1]
        return bool(
            c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
                "AutoScalingGroups"
            ]
        )
    if ":log-group:" in arn:
        name = arn.split(":log-group:", 1)[1].removesuffix(":*")
        return any(
            g["logGroupName"] == name
            for g in c.logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"]
        )
    if ":parameter/" in arn:
        try:
            c.ssm.get_parameter(Name="/" + arn.split(":parameter/", 1)[1])
            return True
        except c.ssm.exceptions.ParameterNotFound:
            return False
    return True
