"""IAM documents for aegc's roles, scoped to one deployment."""

from __future__ import annotations

from typing import Any

from aegc import naming

WORKER_POLICY = "aegc-worker"


def ec2_trust_policy() -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def worker_policy(*, deployment: str, region: str, account: str, bucket: str) -> dict[str, Any]:
    """Least privilege for aegc-worker on an instance of ``deployment``.

    Workers consume this deployment's task queues, post to its result queues, move payloads under
    ``payloads/<d>/``, pull aegc images, write their log streams, and terminate themselves only in
    Auto Scaling groups tagged with this deployment.
    """
    sqs = f"arn:aws:sqs:{region}:{account}"
    d = deployment
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ConsumeTasks",
                "Effect": "Allow",
                "Action": [
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                ],
                "Resource": f"{sqs}:{naming.PREFIX}-{d}-*-tasks",
            },
            {
                "Sid": "PostResults",
                "Effect": "Allow",
                "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
                "Resource": f"{sqs}:{naming.PREFIX}-{d}-res-*",
            },
            {
                "Sid": "ReadTaskPayloads",
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/payloads/{d}/tasks/*",
            },
            {
                "Sid": "WriteResultPayloads",
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{bucket}/payloads/{d}/results/*",
            },
            {
                "Sid": "SelfDrain",
                "Effect": "Allow",
                "Action": "autoscaling:TerminateInstanceInAutoScalingGroup",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {f"autoscaling:ResourceTag/{naming.TAG_DEPLOYMENT}": d}
                },
            },
            {
                "Sid": "EcrAuth",
                "Effect": "Allow",
                "Action": "ecr:GetAuthorizationToken",
                "Resource": "*",
            },
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": f"arn:aws:ecr:{region}:{account}:repository/{naming.PREFIX}/*",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{region}:{account}:log-group:{naming.log_group(d)}:*",
            },
        ],
    }
