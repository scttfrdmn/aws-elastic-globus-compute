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


def operator_policy(*, deployment: str, region: str, account: str, bucket: str) -> dict[str, Any]:
    """What AWSEngine needs at runtime, for the credentials the endpoint container runs with."""
    sqs = f"arn:aws:sqs:{region}:{account}"
    d = deployment
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadManifest",
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": f"arn:aws:ssm:{region}:{account}:parameter/{naming.PREFIX}/{d}/*",
            },
            {
                "Sid": "SubmitTasks",
                "Effect": "Allow",
                "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
                "Resource": [
                    f"{sqs}:{naming.PREFIX}-{d}-*-tasks",
                    f"{sqs}:{naming.PREFIX}-{d}-*-dlq",
                ],
            },
            {
                "Sid": "OwnResultQueues",
                "Effect": "Allow",
                "Action": [
                    "sqs:CreateQueue",
                    "sqs:TagQueue",
                    "sqs:ListQueueTags",
                    "sqs:SetQueueAttributes",
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                ],
                "Resource": f"{sqs}:{naming.PREFIX}-{d}-res-*",
            },
            {
                "Sid": "TaskPayloads",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/payloads/{d}/tasks/*",
            },
            {
                "Sid": "ResultPayloads",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/payloads/{d}/results/*",
            },
            {
                "Sid": "ScaleOut",
                "Effect": "Allow",
                "Action": "autoscaling:SetDesiredCapacity",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {f"autoscaling:ResourceTag/{naming.TAG_DEPLOYMENT}": d}
                },
            },
            {
                "Sid": "Observe",
                "Effect": "Allow",
                "Action": [
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:DescribeScalingActivities",
                ],
                "Resource": "*",
            },
        ],
    }


def deployer_policy(*, region: str, account: str) -> dict[str, Any]:
    """What ``aegc bootstrap / deploy / destroy`` need. A least-privilege starting point.

    Creates are conditioned on the request carrying ``aegc:managed=true``, deletes and updates on
    the resource carrying it, wherever the service supports tag conditions. IAM is limited to the
    /aegc/ path, and roles can only be passed to EC2 and ECR.
    """
    p, a, r = naming.PREFIX, account, region
    managed_req = {"StringEquals": {f"aws:RequestTag/{naming.TAG_MANAGED}": "true"}}
    managed_res = {"StringEquals": {f"aws:ResourceTag/{naming.TAG_MANAGED}": "true"}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "IamUnderAegcPath",
                "Effect": "Allow",
                "Action": [
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:GetRole",
                    "iam:TagRole",
                    "iam:ListRoleTags",
                    "iam:PutRolePolicy",
                    "iam:GetRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:CreateInstanceProfile",
                    "iam:DeleteInstanceProfile",
                    "iam:GetInstanceProfile",
                    "iam:TagInstanceProfile",
                    "iam:ListInstanceProfileTags",
                    "iam:AddRoleToInstanceProfile",
                    "iam:RemoveRoleFromInstanceProfile",
                ],
                "Resource": [
                    f"arn:aws:iam::{a}:role/{p}/*",
                    f"arn:aws:iam::{a}:instance-profile/{p}/*",
                ],
            },
            {
                "Sid": "PassAegcRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": f"arn:aws:iam::{a}:role/{p}/*",
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": ["ec2.amazonaws.com", "ecr.amazonaws.com"]
                    }
                },
            },
            {
                "Sid": "Ec2CreateTagged",
                "Effect": "Allow",
                "Action": ["ec2:CreateSecurityGroup", "ec2:CreateLaunchTemplate"],
                "Resource": "*",
                "Condition": managed_req,
            },
            {
                "Sid": "Ec2TagOnCreate",
                "Effect": "Allow",
                "Action": "ec2:CreateTags",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "ec2:CreateAction": [
                            "CreateSecurityGroup",
                            "CreateLaunchTemplate",
                            "RunInstances",
                        ]
                    }
                },
            },
            {
                "Sid": "Ec2ChangeOwned",
                "Effect": "Allow",
                "Action": [
                    "ec2:DeleteSecurityGroup",
                    "ec2:DeleteLaunchTemplate",
                    "ec2:CreateLaunchTemplateVersion",
                    "ec2:ModifyLaunchTemplate",
                ],
                "Resource": "*",
                "Condition": managed_res,
            },
            {
                "Sid": "Ec2Read",
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeVpcs",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeLaunchTemplates",
                    "ec2:DescribeLaunchTemplateVersions",
                    "ec2:DescribeImages",
                    "ec2:DescribeInstanceTypes",
                    "ec2:RunInstances",
                ],
                "Resource": "*",
            },
            {
                "Sid": "AutoScalingCreateTagged",
                "Effect": "Allow",
                "Action": ["autoscaling:CreateAutoScalingGroup", "autoscaling:CreateOrUpdateTags"],
                "Resource": "*",
                "Condition": managed_req,
            },
            {
                "Sid": "AutoScalingChangeOwned",
                "Effect": "Allow",
                "Action": [
                    "autoscaling:UpdateAutoScalingGroup",
                    "autoscaling:DeleteAutoScalingGroup",
                    "autoscaling:SuspendProcesses",
                    "autoscaling:SetDesiredCapacity",
                ],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {f"autoscaling:ResourceTag/{naming.TAG_MANAGED}": "true"}
                },
            },
            {
                "Sid": "AutoScalingRead",
                "Effect": "Allow",
                "Action": [
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:DescribeScalingActivities",
                ],
                "Resource": "*",
            },
            {
                "Sid": "Queues",
                "Effect": "Allow",
                "Action": [
                    "sqs:CreateQueue",
                    "sqs:DeleteQueue",
                    "sqs:GetQueueUrl",
                    "sqs:GetQueueAttributes",
                    "sqs:SetQueueAttributes",
                    "sqs:TagQueue",
                    "sqs:ListQueueTags",
                ],
                "Resource": f"arn:aws:sqs:{r}:{a}:{p}-*",
            },
            {"Sid": "ListQueues", "Effect": "Allow", "Action": "sqs:ListQueues", "Resource": "*"},
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:DeleteLogGroup",
                    "logs:PutRetentionPolicy",
                    "logs:TagResource",
                    "logs:ListTagsForResource",
                ],
                "Resource": f"arn:aws:logs:{r}:{a}:log-group:/{p}/*",
            },
            {
                "Sid": "LogsRead",
                "Effect": "Allow",
                "Action": "logs:DescribeLogGroups",
                "Resource": "*",
            },
            {
                "Sid": "Parameters",
                "Effect": "Allow",
                "Action": [
                    "ssm:GetParameter",
                    "ssm:GetParametersByPath",
                    "ssm:PutParameter",
                    "ssm:DeleteParameter",
                    "ssm:AddTagsToResource",
                    "ssm:ListTagsForResource",
                ],
                "Resource": [
                    f"arn:aws:ssm:{r}:{a}:parameter/{p}/*",
                    f"arn:aws:ssm:{r}:{a}:parameter/{p}",
                ],
            },
            {
                "Sid": "PublicAmiParameter",
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": f"arn:aws:ssm:{r}::parameter/aws/service/ecs/optimized-ami/*",
            },
            {
                "Sid": "Bucket",
                "Effect": "Allow",
                "Action": [
                    "s3:CreateBucket",
                    "s3:DeleteBucket",
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:GetBucketTagging",
                    "s3:PutBucketTagging",
                    "s3:TagResource",  # CreateBucket with CreateBucketConfiguration.Tags
                    "s3:PutBucketPublicAccessBlock",
                    "s3:GetBucketPublicAccessBlock",
                    "s3:PutBucketOwnershipControls",
                    "s3:PutEncryptionConfiguration",
                    "s3:PutLifecycleConfiguration",
                    "s3:GetLifecycleConfiguration",
                    "s3:PutBucketPolicy",
                    "s3:GetBucketPolicy",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": [
                    f"arn:aws:s3:::{naming.bucket_name(a, r)}",
                    f"arn:aws:s3:::{naming.bucket_name(a, r)}/*",
                ],
            },
            {
                "Sid": "Ecr",
                "Effect": "Allow",
                "Action": [
                    "ecr:CreatePullThroughCacheRule",
                    "ecr:DescribePullThroughCacheRules",
                    "ecr:DeletePullThroughCacheRule",
                    "ecr:CreateRepositoryCreationTemplate",
                    "ecr:DescribeRepositoryCreationTemplates",
                    "ecr:UpdateRepositoryCreationTemplate",
                    "ecr:DeleteRepositoryCreationTemplate",
                    "ecr:DescribeRepositories",
                    "ecr:ListTagsForResource",
                ],
                "Resource": "*",
            },
            {
                "Sid": "EcrDeleteAegcRepos",
                "Effect": "Allow",
                "Action": "ecr:DeleteRepository",
                "Resource": [
                    f"arn:aws:ecr:{r}:{a}:repository/{naming.pull_through_prefix()}/*",
                    f"arn:aws:ecr:{r}:{a}:repository/{p}/*",
                ],
            },
            {
                "Sid": "Identity",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*",
            },
        ],
    }


POLICIES = {"worker", "operator", "deployer"}


def policy(kind: str, *, deployment: str, region: str, account: str) -> dict[str, Any]:
    bucket = naming.bucket_name(account, region)
    if kind == "worker":
        return worker_policy(deployment=deployment, region=region, account=account, bucket=bucket)
    if kind == "operator":
        return operator_policy(deployment=deployment, region=region, account=account, bucket=bucket)
    if kind == "deployer":
        return deployer_policy(region=region, account=account)
    raise ValueError(f"unknown policy {kind!r}; choose from {sorted(POLICIES)}")
