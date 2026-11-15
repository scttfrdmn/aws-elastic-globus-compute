"""IAM documents for aegc's roles, scoped to one deployment."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from aegc import naming

WORKER_POLICY = "aegc-worker"
#: The worker role's inline policy for the user's data-access grants (#53).
GRANTS_POLICY = "aegc-grants"
#: IAM limits the inline policies of one role to 10,240 characters in total, whitespace not counted.
INLINE_POLICY_LIMIT = 10_240

_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_IP_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
#: aegc's own buckets (`aegc-<account>-<region>`): workers already have what they need there.
_AEGC_BUCKET_RE = re.compile(rf"{naming.PREFIX}-\d{{12}}-[a-z0-9-]+")
#: Characters IAM treats as wildcards or variables in resources and StringLike conditions.
_WILDCARDS = ("*", "?", "${")

Access = Literal["read", "write"]


@dataclass(frozen=True)
class Grant:
    """Worker access to ``s3://bucket/prefix``. aegc references the bucket; it never creates,
    changes or deletes it. The prefix is literal: ``out`` also covers ``outside/``, so end it with
    ``/`` for a folder. An empty prefix is the whole bucket."""

    access: Access
    bucket: str
    prefix: str = ""

    def __post_init__(self) -> None:
        if self.access not in ("read", "write"):
            raise ValueError(f"grant access must be read or write, not {self.access!r}")
        b = self.bucket
        if any(w in b for w in _WILDCARDS) or any(w in self.prefix for w in _WILDCARDS):
            raise ValueError(f"grants can't use wildcards: {self.uri}")
        if (
            not _BUCKET_RE.fullmatch(b)
            or ".." in b
            or _IP_RE.fullmatch(b)
            or b.startswith(("xn--", "sthree-"))
            or b.endswith(("-s3alias", "--ol-s3", "--x-s3", "--table-s3"))
        ):
            raise ValueError(f"not a valid general purpose S3 bucket name: {b!r}")
        if _AEGC_BUCKET_RE.fullmatch(b):
            raise ValueError(
                f"{b} is aegc's own bucket; workers already have exactly what they need there"
            )
        if len(self.prefix.encode()) > 1024:
            raise ValueError("a grant prefix is an S3 key prefix: at most 1,024 bytes")

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def covers(self, bucket: str, key: str) -> bool:
        return bucket == self.bucket and key.startswith(self.prefix)

    def to_dict(self) -> dict[str, str]:
        return {"access": self.access, "bucket": self.bucket, "prefix": self.prefix}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Grant:
        return cls(access=d["access"], bucket=str(d["bucket"]), prefix=str(d.get("prefix", "")))

    @classmethod
    def parse(cls, text: str) -> Grant:
        """``read=s3://bucket/prefix`` or ``write=s3://bucket/prefix``. ``s3://bucket/`` (with
        the slash) grants the whole bucket; ``s3://bucket`` is refused as ambiguous."""
        access, sep, uri = text.partition("=")
        if not sep or access not in ("read", "write"):
            raise ValueError(f"a grant is read=s3://bucket/prefix or write=s3://...: {text!r}")
        bucket, prefix = split_s3_uri(uri)
        if prefix is None:
            raise ValueError(
                f"{uri} has no prefix: give s3://{bucket}/PREFIX, or s3://{bucket}/ to grant "
                "the whole bucket"
            )
        return cls(access=access, bucket=bucket, prefix=prefix)  # type: ignore[arg-type]


def split_s3_uri(uri: str) -> tuple[str, str | None]:
    """``s3://bucket/key`` -> ``(bucket, key)``; ``s3://bucket`` -> ``(bucket, None)``."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    bucket, sep, key = uri.removeprefix("s3://").partition("/")
    if not bucket:
        raise ValueError(f"no bucket in {uri!r}")
    return bucket, (key if sep else None)


def policy_size(doc: dict[str, Any]) -> int:
    """A policy's size as IAM counts it (no whitespace)."""
    return len(json.dumps(doc, separators=(",", ":")))


#: More grants than this are refused: they also go into each worker's user data (16 KB).
MAX_GRANTS = 32


def grants_policy(
    grants: tuple[Grant, ...] | list[Grant],
    *,
    deployment: str,
    budget: int = INLINE_POLICY_LIMIT,
    account: str = "",
) -> dict[str, Any]:
    """The inline policy ``aegc-grants``: worker access to the user's own S3 prefixes.

    - read: ``s3:GetObject`` and ``s3:GetObjectVersion`` under each prefix, and ``s3:ListBucket``
      on its bucket for keys under it (the ``s3:prefix`` condition; none for a whole bucket);
    - write: ``s3:PutObject``, ``s3:AbortMultipartUpload`` and ``s3:ListMultipartUploadParts``
      under each prefix (multipart uploads need all three).

    Every task in the deployment can use every grant: a grant extends the deployment's trust
    domain. Requester-pays buckets need no extra IAM; the reader passes ``RequestPayer='requester'``
    (``s3.get_object(..., RequestPayer="requester")``, ``ExtraArgs={"RequestPayer": "requester"}``
    for ``download_file``), and this account pays for the requests and transfer. Buckets encrypted
    with a customer managed KMS key also need ``kms:Decrypt`` (read) or ``kms:GenerateDataKey``
    (write) on that key, which aegc doesn't grant.

    With ``account``, writes are limited to buckets that account owns (``s3:ResourceAccount``),
    so a mistyped or squatted bucket name in another account can't receive task outputs. Reads
    may cross accounts (open data lives in other accounts).

    ``budget`` is the room left in the role's 10,240 inline characters; too many grants raise
    ``ValueError``.
    """
    gs = list(dict.fromkeys(grants))
    if not gs:
        raise ValueError("no grants: delete the aegc-grants policy instead")
    if len(gs) > MAX_GRANTS:
        raise ValueError(
            f"deployment {deployment} has {len(gs)} grants; at most {MAX_GRANTS} (use fewer, "
            "broader prefixes)"
        )
    reads = [g for g in gs if g.access == "read"]
    writes = [g for g in gs if g.access == "write"]
    statements: list[dict[str, Any]] = []
    if reads:
        statements.append(
            {
                "Sid": "GrantRead",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": [f"arn:aws:s3:::{g.bucket}/{g.prefix}*" for g in reads],
            }
        )
        for i, bucket in enumerate(dict.fromkeys(g.bucket for g in reads)):
            prefixes = [g.prefix for g in reads if g.bucket == bucket]
            st: dict[str, Any] = {
                "Sid": f"GrantList{i}",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
            }
            if "" not in prefixes:  # a whole-bucket grant lists without a prefix too
                st["Condition"] = {"StringLike": {"s3:prefix": [f"{p}*" for p in prefixes]}}
            statements.append(st)
    if writes:
        statements.append(
            {
                "Sid": "GrantWrite",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": [f"arn:aws:s3:::{g.bucket}/{g.prefix}*" for g in writes],
                **(
                    {"Condition": {"StringEquals": {"s3:ResourceAccount": account}}}
                    if account
                    else {}
                ),
            }
        )
    doc = {"Version": "2012-10-17", "Statement": statements}
    if (size := policy_size(doc)) > budget:
        raise ValueError(
            f"deployment {deployment}'s {len(gs)} grants need a {size}-character IAM policy, but "
            f"the worker role has room for {budget} (IAM allows {INLINE_POLICY_LIMIT} in all of a "
            "role's inline policies); use fewer, broader prefixes"
        )
    return doc


#: The vCPU quota preflight in `aegc deploy` and `aegc status` (none of these take resources).
VCPU_QUOTA_STATEMENT: dict[str, Any] = {
    "Sid": "VcpuQuotaPreflight",
    "Effect": "Allow",
    "Action": [
        "servicequotas:GetServiceQuota",
        "servicequotas:GetAWSDefaultServiceQuota",
        "cloudwatch:GetMetricData",
    ],
    "Resource": "*",
}


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
                # vCPU-weighted groups refuse a decrement larger than desired: the worker lowers
                # desired by its own weight, then terminates without decrementing.
                "Sid": "ShrinkToLeave",
                "Effect": "Allow",
                "Action": "autoscaling:SetDesiredCapacity",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {f"autoscaling:ResourceTag/{naming.TAG_DEPLOYMENT}": d}
                },
            },
            {
                "Sid": "ReadOwnGroup",
                "Effect": "Allow",
                "Action": "autoscaling:DescribeAutoScalingGroups",
                "Resource": "*",
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
            {
                # Stop claiming when the deployment is capped, stopped or expired.
                # The state (stop, caps), and the manifest for aegc.outputs' current grants.
                "Sid": "ReadDeploymentState",
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": [
                    f"arn:aws:ssm:{region}:{account}:parameter{naming.state_parameter(d)}",
                    f"arn:aws:ssm:{region}:{account}:parameter"
                    f"{naming.deployment_manifest_parameter(d)}",
                ],
            },
            {
                "Sid": "WriteCostLedger",
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{bucket}/ledger/{d}/*",
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
            {
                # Spend caps: write `capped`, read the cost ledger, price instances, and fail
                # this endpoint's own queued tasks (purge them from the task queues).
                "Sid": "CapState",
                "Effect": "Allow",
                # write_state checks ownership tags first, and tags the parameter if it creates it
                # (deployments from v0.1 have none).
                "Action": ["ssm:PutParameter", "ssm:ListTagsForResource", "ssm:AddTagsToResource"],
                "Resource": f"arn:aws:ssm:{region}:{account}:parameter{naming.state_parameter(d)}",
            },
            {
                "Sid": "ReadCostLedger",
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/ledger/{d}/*",
            },
            {
                "Sid": "ListCostLedger",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": f"ledger/{d}/*"}},
            },
            {
                "Sid": "Prices",
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeSpotPriceHistory",
                    "pricing:GetProducts",
                    "ce:GetCostAndUsage",  # aegc cost --actual
                ],
                "Resource": "*",
            },
            {
                "Sid": "PurgeOwnTasks",
                "Effect": "Allow",
                "Action": [
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:ChangeMessageVisibility",
                ],
                "Resource": f"{sqs}:{naming.PREFIX}-{d}-*-tasks",
            },
            {
                # On a cap trip, drop a group's minimum to 0 so its workers can leave.
                "Sid": "CapMinimum",
                "Effect": "Allow",
                "Action": "autoscaling:UpdateAutoScalingGroup",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {f"autoscaling:ResourceTag/{naming.TAG_DEPLOYMENT}": d}
                },
            },
            {
                # Renew the deployment lease (EventBridge Scheduler) hourly.
                "Sid": "RenewLease",
                "Effect": "Allow",
                "Action": ["scheduler:GetSchedule", "scheduler:UpdateSchedule"],
                "Resource": f"arn:aws:scheduler:{region}:{account}:schedule/{naming.PREFIX}-{d}/*",
            },
            {
                "Sid": "ListLease",
                "Effect": "Allow",
                "Action": "scheduler:ListSchedules",
                "Resource": "*",
            },
            {
                "Sid": "PassLeaseRole",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": (
                    f"arn:aws:iam::{account}:role{naming.IAM_PATH}"
                    f"{naming.PREFIX}-{d}-scheduler-{region}"
                ),
                "Condition": {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            },
            {
                # `aegc status` finds result queues by name prefix. ListQueues has no resource.
                "Sid": "FindResultQueues",
                "Effect": "Allow",
                "Action": "sqs:ListQueues",
                "Resource": "*",
            },
            VCPU_QUOTA_STATEMENT,
        ],
    }


#: Statements only `aegc bootstrap` / `unbootstrap` need (the region-wide resources).
BOOTSTRAP_ONLY = {"Bucket", "Ecr", "EcrDeleteAegcRepos"}
#: Statements both policies need.
SHARED = {"IamUnderAegcPath", "PassAegcRoles", "Parameters", "Identity"}


def deployer_policy(*, region: str, account: str) -> dict[str, Any]:
    """What ``aegc deploy / destroy / stop / resume / reap / footprint`` need.

    The region-wide resources (``aegc bootstrap``) are in :func:`bootstrap_policy`. Managed IAM
    policies are limited to 6,144 characters, so they're split.
    """
    doc = _admin_policy(region=region, account=account)
    a, r = account, region
    keep = [st for st in doc["Statement"] if st["Sid"] not in BOOTSTRAP_ONLY]
    for st in keep:
        if st["Sid"] == "PassAegcRoles":
            st["Condition"]["StringEquals"]["iam:PassedToService"] = [
                "ec2.amazonaws.com",
                "scheduler.amazonaws.com",
            ]
        if st["Sid"] == "Ec2Read":
            st["Action"] = [
                *st["Action"],
                "ec2:DescribeInstances",
                "ec2:DescribeVolumes",
                "ec2:DescribeNetworkInterfaces",
            ]
    keep += [
        {
            "Sid": "EcrRead",
            "Effect": "Allow",
            "Action": ["ecr:DescribeRepositories", "ecr:ListTagsForResource"],
            "Resource": f"arn:aws:ecr:{r}:{a}:repository/{naming.PREFIX}/*",
        },
        {
            # The deployment lease: a tagged schedule group aegc-<d> and its schedules.
            "Sid": "LeaseSchedules",
            "Effect": "Allow",
            "Action": [
                "scheduler:CreateScheduleGroup",
                "scheduler:GetScheduleGroup",
                "scheduler:DeleteScheduleGroup",
                "scheduler:CreateSchedule",
                "scheduler:GetSchedule",
                "scheduler:UpdateSchedule",
                "scheduler:DeleteSchedule",
                "scheduler:TagResource",
                "scheduler:ListTagsForResource",
            ],
            "Resource": [
                f"arn:aws:scheduler:{r}:{a}:schedule-group/{naming.PREFIX}-*",
                f"arn:aws:scheduler:{r}:{a}:schedule/{naming.PREFIX}-*/*",
            ],
        },
        {
            "Sid": "ListSchedules",
            "Effect": "Allow",
            "Action": "scheduler:ListSchedules",
            "Resource": "*",
        },
        {
            # aegc footprint (report only).
            "Sid": "TaggedResources",
            "Effect": "Allow",
            "Action": "tag:GetResources",
            "Resource": "*",
        },
    ]
    return {"Version": "2012-10-17", "Statement": keep}


def bootstrap_policy(*, region: str, account: str) -> dict[str, Any]:
    """What ``aegc bootstrap / unbootstrap`` need: the payload bucket, the ECR pull-through
    cache and its creation template and role, and ``/aegc/bootstrap``."""
    doc = _admin_policy(region=region, account=account)
    keep = [st for st in doc["Statement"] if st["Sid"] in BOOTSTRAP_ONLY | SHARED]
    for st in keep:
        if st["Sid"] == "PassAegcRoles":
            st["Condition"]["StringEquals"]["iam:PassedToService"] = ["ecr.amazonaws.com"]
    return {"Version": "2012-10-17", "Statement": keep}


def _admin_policy(*, region: str, account: str) -> dict[str, Any]:
    """Every statement the deployer and bootstrap policies are cut from. A least-privilege
    starting point.

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
                ],
                "Resource": "*",
            },
            {
                # CreateAutoScalingGroup checks that the caller could launch from the template.
                # Allow that only for aegc's own templates...
                "Sid": "RunFromAegcTemplates",
                "Effect": "Allow",
                "Action": "ec2:RunInstances",
                "Resource": f"arn:aws:ec2:{r}:{a}:launch-template/*",
                "Condition": managed_res,
            },
            {
                # ...and for the other resources in the launch, only through a launch template.
                "Sid": "RunOnlyViaTemplates",
                "Effect": "Allow",
                "Action": "ec2:RunInstances",
                "Resource": [
                    f"arn:aws:ec2:{r}:{a}:{kind}/*"
                    for kind in (
                        "instance",
                        "volume",
                        "network-interface",
                        "subnet",
                        "security-group",
                    )
                ]
                + [f"arn:aws:ec2:{r}::image/*"],
                "Condition": {
                    "ArnLike": {"ec2:LaunchTemplate": f"arn:aws:ec2:{r}:{a}:launch-template/*"}
                },
            },
            {
                # A fresh account has no Auto Scaling or Spot service-linked role yet.
                "Sid": "ServiceLinkedRoles",
                "Effect": "Allow",
                "Action": "iam:CreateServiceLinkedRole",
                "Resource": f"arn:aws:iam::{a}:role/aws-service-role/*",
                "Condition": {
                    "StringEquals": {
                        "iam:AWSServiceName": ["autoscaling.amazonaws.com", "spot.amazonaws.com"]
                    }
                },
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
                    "autoscaling:TerminateInstanceInAutoScalingGroup",
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
            VCPU_QUOTA_STATEMENT,
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
                # deploy tags the images it pins inuse-<d>-..., so the lifecycle keeps them (#27).
                "Sid": "EcrInUseTags",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:PutImage",
                    "ecr:BatchDeleteImage",
                    "ecr:DescribeImages",
                    "ecr:PutLifecyclePolicy",
                ],
                "Resource": f"arn:aws:ecr:{r}:{a}:repository/{p}/*",
            },
            {
                "Sid": "Identity",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*",
            },
        ],
    }


def builder_policy(*, region: str, account: str) -> dict[str, Any]:
    """What ``aegc image build`` needs: pull a base through the ECR cache, push ``aegc/<env>``
    images, and record them under ``/aegc/images/``."""
    p, a, r = naming.PREFIX, account, region
    repos = f"arn:aws:ecr:{r}:{a}:repository/{p}/*"
    cache = f"arn:aws:ecr:{r}:{a}:repository/{naming.pull_through_prefix()}/*"
    pull = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "EcrAuth", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken",
             "Resource": "*"},
            {
                # The first pull of a base creates its cache repository and imports the image.
                "Sid": "PullBaseThroughCache",
                "Effect": "Allow",
                "Action": [*pull, "ecr:BatchImportUpstreamImage", "ecr:CreateRepository"],
                "Resource": cache,
            },
            {
                "Sid": "WorkerImageRepos",
                "Effect": "Allow",
                "Action": [
                    *pull,
                    "ecr:CreateRepository",
                    "ecr:TagResource",
                    "ecr:PutLifecyclePolicy",
                    "ecr:DescribeRepositories",
                    "ecr:DescribeImages",
                    "ecr:ListTagsForResource",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                ],
                "Resource": repos,
            },
            {
                "Sid": "ImageRecords",
                "Effect": "Allow",
                "Action": [
                    "ssm:GetParameter",
                    "ssm:PutParameter",
                    "ssm:DeleteParameter",
                    "ssm:AddTagsToResource",
                    "ssm:ListTagsForResource",
                ],
                "Resource": f"arn:aws:ssm:{r}:{a}:parameter/{p}/images/*",
            },
            {"Sid": "Identity", "Effect": "Allow", "Action": "sts:GetCallerIdentity",
             "Resource": "*"},
        ],
    }  # fmt: skip


POLICIES = {"worker", "operator", "deployer", "bootstrap", "builder"}


def policy(kind: str, *, deployment: str, region: str, account: str) -> dict[str, Any]:
    bucket = naming.bucket_name(account, region)
    if kind == "worker":
        return worker_policy(deployment=deployment, region=region, account=account, bucket=bucket)
    if kind == "operator":
        return operator_policy(deployment=deployment, region=region, account=account, bucket=bucket)
    if kind == "deployer":
        return deployer_policy(region=region, account=account)
    if kind == "builder":
        return builder_policy(region=region, account=account)
    if kind == "bootstrap":
        return bootstrap_policy(region=region, account=account)
    raise ValueError(f"unknown policy {kind!r}; choose from {sorted(POLICIES)}")
