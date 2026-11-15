# IAM policies

aegc uses five policies, printed by `aegc iam-policy {worker,operator,deployer,bootstrap,builder}`.
Each is scoped to one account and region, and the worker and operator policies to one deployment.
The documents below were generated with:

```sh
aegc --region us-west-2 iam-policy <kind> --deployment e2e --account 123456789012
```

Generate your own with your account, region and deployment; with `--region` and `--account` given,
the command makes no AWS calls. The tests check these documents with the IAM policy simulator
(allowed and denied cases, including tag conditions) and check their sizes.

| Policy | Attach to | Used by |
|---|---|---|
| [worker](#worker) | Created by `aegc deploy` as the inline policy `aegc-worker` on the role `aegc-<d>-worker-<region>` | `aegc-worker` on worker instances |
| [operator](#operator) | The credentials the endpoint container runs with | `AWSEngine`; also `aegc cost`, `aegc limits --resume` and the spend lines of `aegc status` |
| [deployer](#deployer) | The credentials you run `aegc deploy`, `destroy`, `stop`, `resume`, `limits`, `status`, `reap` and `footprint` with | The `aegc` CLI |
| [bootstrap](#bootstrap) | The credentials you run `aegc bootstrap` and `unbootstrap` with | The `aegc` CLI |
| [builder](#builder) | The credentials you run `aegc image build`, `register` and `list` with | The `aegc` CLI |

With [grants](../how-to/data.md), `aegc deploy` adds a second inline policy to the worker role,
[`aegc-grants`](#aegc-grants), for your own S3 prefixes. It also creates two roles of its own,
shown at the end: the [scheduler role](#scheduler-role) for the deployment lease, and (from
`aegc bootstrap`) the [ECR creation template role](#ecr-creation-template-role).

## Why five

Customer managed policies are limited to 6,144 characters, not counting whitespace. In v0.1 one
deployer policy covered `bootstrap` as well as `deploy` and `destroy`; with v0.2's additions (the
lease's schedules, in-use image tags, and the reads `destroy --verify` and `footprint` need) it no
longer fits. It's now split by when you need it: **bootstrap** for the region-wide resources, used
once per region, and **deployer** for everything about a deployment. Attach both to an
administrator's credentials, or give the bootstrap policy only to whoever sets up the region.

Sizes of the documents below, without whitespace:

| Policy | Characters | Limit |
|---|---|---|
| worker | 1,900 | 10,240 for all of the role's inline policies, `aegc-grants` included |
| operator | 3,059 | 6,144 |
| deployer | 5,767 | 6,144 |
| bootstrap | 2,311 | 6,144 |
| builder | 1,120 | 6,144 |

The deployer is the closest to the limit; longer region names make it a little longer.

## worker

What a worker may do: consume the deployment's task queues, post to its result queues, read task
payloads and write result payloads under `payloads/<d>/`, pull `aegc/*` images, write to its log
group, terminate itself in an Auto Scaling group tagged with the deployment, read the deployment's
state, and write its cost ledger record under `ledger/<d>/`. Nothing else; in particular no S3
access outside those prefixes, so tasks read open data unsigned, and reach your own buckets only
through [grants](#aegc-grants).

`ShrinkToLeave` exists because a vCPU-weighted group refuses a decrement larger than its desired
capacity (desired 1 vCPU, a 2-vCPU worker): the worker lowers desired by its own weight, then
terminates without a decrement. The agent only ever lowers it, but `SetDesiredCapacity` has no
condition on the value, so code running as the worker role could also raise a group's desired
capacity, up to its `max_vcpus` (see [Security model](../concepts/security.md)).

```json
{
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
        "sqs:GetQueueUrl"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-*-tasks"
    },
    {
      "Sid": "PostResults",
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:GetQueueUrl"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-res-*"
    },
    {
      "Sid": "ReadTaskPayloads",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/payloads/e2e/tasks/*"
    },
    {
      "Sid": "WriteResultPayloads",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/payloads/e2e/results/*"
    },
    {
      "Sid": "ShrinkToLeave",
      "Effect": "Allow",
      "Action": "autoscaling:SetDesiredCapacity",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e"
        }
      }
    },
    {
      "Sid": "ReadOwnGroup",
      "Effect": "Allow",
      "Action": "autoscaling:DescribeAutoScalingGroups",
      "Resource": "*"
    },
    {
      "Sid": "SelfDrain",
      "Effect": "Allow",
      "Action": "autoscaling:TerminateInstanceInAutoScalingGroup",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e"
        }
      }
    },
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrPull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/e2e:*"
    },
    {
      "Sid": "ReadDeploymentState",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/state"
    },
    {
      "Sid": "WriteCostLedger",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/ledger/e2e/*"
    }
  ]
}
```

`aegc deploy` rewrites this inline policy on every run, so redeploying a v0.1 deployment gives its
workers the new state and ledger permissions.

The role trusts EC2:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ec2.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

## aegc-grants

The worker role's second inline policy, for the deployment's
[grants](../how-to/data.md): `aegc deploy --grant read=...` and `--grant write=...`. It exists only
while the deployment has grants: `deploy` puts it after `aegc-worker`, `--grant none` deletes it,
and `aegc destroy` deletes it with the role. `aegc iam-policy` doesn't print it; it depends on the
grants. For

```sh
aegc deploy e2e --pool geospatial \
  --grant read=s3://my-lab-data/inputs/ --grant write=s3://my-lab-results/runs/
```

it is:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GrantRead",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ],
      "Resource": [
        "arn:aws:s3:::my-lab-data/inputs/*"
      ]
    },
    {
      "Sid": "GrantList0",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-lab-data",
      "Condition": {
        "StringLike": {
          "s3:prefix": [
            "inputs/*"
          ]
        }
      }
    },
    {
      "Sid": "GrantWrite",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::my-lab-results/runs/*"
      ]
    }
  ]
}
```

- All read grants share one `GrantRead` statement, and all write grants one `GrantWrite`. Each
  bucket with read grants gets a `GrantList<n>` statement, conditioned on its prefixes; a
  whole-bucket grant (`s3://bucket/`) drops the condition.
- Write grants don't allow `GetObject`, `DeleteObject` or listing.
- Grants can't contain `*`, `?` or `${`, so the only wildcard is the `*` aegc appends to each
  prefix.
- IAM counts all of a role's inline policies against 10,240 characters, without whitespace.
  `aegc-worker` takes about 1,900, and `deploy` refuses grants that don't fit in the rest, before
  it changes anything. The policy above is 497 characters.
- Requester-pays buckets need nothing more. Buckets in another account must also allow the worker
  role in their bucket policy, and buckets encrypted with a customer managed KMS key need
  `kms:Decrypt` or `kms:GenerateDataKey` on the key, which aegc doesn't add.

The [deployer](#deployer) policy already allows `iam:PutRolePolicy` and `iam:DeleteRolePolicy` on roles
under `/aegc/`, so grants need no extra permissions to deploy.

## operator

What `AWSEngine` needs at runtime: read the deployment's parameters (manifest and state), send
tasks and read queue depth, create and use its own result queues, move payloads, and raise desired
capacity on the deployment's Auto Scaling groups. For spend limits and the lease it also:

- writes the deployment's state (`CapState`), to cap it; it reads the parameter's tags first, to
  check that aegc created it, and tags it if it has to create it (a v0.1 deployment has none);
- reads the cost ledger (`ReadCostLedger`, `ListCostLedger`) and prices (`Prices`), and asks Cost
  Explorer (`ce:GetCostAndUsage`, in `Prices`) for `aegc cost --actual`;
- receives and deletes messages on the deployment's task queues (`PurgeOwnTasks`), to remove its own
  tasks when it fails them; the engine leaves other processes' messages alone, but IAM can't tell
  them apart;
- updates the deployment's groups (`CapMinimum`), to lower their minimums to 0 when it caps the
  deployment and restore them when the cap ends; IAM can't limit which settings
  `UpdateAutoScalingGroup` changes, but the engine only ever sets `MinSize`, to 0 or to the
  manifest's value;
- reads and updates the deployment's schedules, and passes the scheduler role to EventBridge
  Scheduler (`RenewLease`, `ListLease`, `PassLeaseRole`), to renew the lease.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadManifest",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/*"
    },
    {
      "Sid": "SubmitTasks",
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl"
      ],
      "Resource": [
        "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-*-tasks",
        "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-*-dlq"
      ]
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
        "sqs:GetQueueUrl"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-res-*"
    },
    {
      "Sid": "TaskPayloads",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/payloads/e2e/tasks/*"
    },
    {
      "Sid": "ResultPayloads",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/payloads/e2e/results/*"
    },
    {
      "Sid": "ScaleOut",
      "Effect": "Allow",
      "Action": "autoscaling:SetDesiredCapacity",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e"
        }
      }
    },
    {
      "Sid": "Observe",
      "Effect": "Allow",
      "Action": [
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeScalingActivities"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CapState",
      "Effect": "Allow",
      "Action": [
        "ssm:PutParameter",
        "ssm:ListTagsForResource",
        "ssm:AddTagsToResource"
      ],
      "Resource": "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/state"
    },
    {
      "Sid": "ReadCostLedger",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/ledger/e2e/*"
    },
    {
      "Sid": "ListCostLedger",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2",
      "Condition": {
        "StringLike": {
          "s3:prefix": "ledger/e2e/*"
        }
      }
    },
    {
      "Sid": "Prices",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeSpotPriceHistory",
        "pricing:GetProducts",
        "ce:GetCostAndUsage"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PurgeOwnTasks",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-*-tasks"
    },
    {
      "Sid": "CapMinimum",
      "Effect": "Allow",
      "Action": "autoscaling:UpdateAutoScalingGroup",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e"
        }
      }
    },
    {
      "Sid": "RenewLease",
      "Effect": "Allow",
      "Action": [
        "scheduler:GetSchedule",
        "scheduler:UpdateSchedule"
      ],
      "Resource": "arn:aws:scheduler:us-west-2:123456789012:schedule/aegc-e2e/*"
    },
    {
      "Sid": "ListLease",
      "Effect": "Allow",
      "Action": "scheduler:ListSchedules",
      "Resource": "*"
    },
    {
      "Sid": "PassLeaseRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/aegc/aegc-e2e-scheduler-us-west-2",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "scheduler.amazonaws.com"
        }
      }
    },
    {
      "Sid": "FindResultQueues",
      "Effect": "Allow",
      "Action": "sqs:ListQueues",
      "Resource": "*"
    },
    {
      "Sid": "VcpuQuotaPreflight",
      "Effect": "Allow",
      "Action": [
        "servicequotas:GetServiceQuota",
        "servicequotas:GetAWSDefaultServiceQuota",
        "cloudwatch:GetMetricData"
      ],
      "Resource": "*"
    }
  ]
}
```

`sqs:ListQueues` and `scheduler:ListSchedules` have no resource-level scoping. `aegc status` uses
`ListQueues` to find the deployment's result queues by name prefix. The `VcpuQuotaPreflight`
statement (Service Quotas and CloudWatch reads, also in the deployer policy) lets `status` and
`deploy` check the account's vCPU quota, and `status` read the oldest waiting task's age.

An endpoint that ran with the v0.1 operator policy needs this one. With the old policy the engine
can't check spend, cap the deployment or renew the lease, so the deployment expires when the lease
runs out even while the endpoint runs.

`aegc cost`, `aegc limits --resume` and the spend lines of `aegc status` read the ledger and
prices, so they need the operator policy for the deployment (or broader credentials); the deployer
policy has no ledger access. The operator policy includes `ce:GetCostAndUsage` for
`aegc cost --actual`.

## deployer

A least-privilege starting point for `aegc deploy`, `destroy`, `stop`, `resume`, `limits`, `status`,
`reap` and `footprint`. Creates are conditioned on the request carrying `aegc:managed=true`, and
changes and deletes on the resource carrying it, where the service supports tag conditions. IAM
actions are limited to the `/aegc/` path, and roles can be passed only to EC2 and EventBridge
Scheduler.

```json
{
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
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": [
        "arn:aws:iam::123456789012:role/aegc/*",
        "arn:aws:iam::123456789012:instance-profile/aegc/*"
      ]
    },
    {
      "Sid": "PassAegcRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/aegc/*",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": [
            "ec2.amazonaws.com",
            "scheduler.amazonaws.com"
          ]
        }
      }
    },
    {
      "Sid": "Ec2CreateTagged",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateSecurityGroup",
        "ec2:CreateLaunchTemplate"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
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
            "RunInstances"
          ]
        }
      }
    },
    {
      "Sid": "Ec2ChangeOwned",
      "Effect": "Allow",
      "Action": [
        "ec2:DeleteSecurityGroup",
        "ec2:DeleteLaunchTemplate",
        "ec2:CreateLaunchTemplateVersion",
        "ec2:ModifyLaunchTemplate"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
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
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes",
        "ec2:DescribeNetworkInterfaces"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RunFromAegcTemplates",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": "arn:aws:ec2:us-west-2:123456789012:launch-template/*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "RunOnlyViaTemplates",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": [
        "arn:aws:ec2:us-west-2:123456789012:instance/*",
        "arn:aws:ec2:us-west-2:123456789012:volume/*",
        "arn:aws:ec2:us-west-2:123456789012:network-interface/*",
        "arn:aws:ec2:us-west-2:123456789012:subnet/*",
        "arn:aws:ec2:us-west-2:123456789012:security-group/*",
        "arn:aws:ec2:us-west-2::image/*"
      ],
      "Condition": {
        "ArnLike": {
          "ec2:LaunchTemplate": "arn:aws:ec2:us-west-2:123456789012:launch-template/*"
        }
      }
    },
    {
      "Sid": "ServiceLinkedRoles",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::123456789012:role/aws-service-role/*",
      "Condition": {
        "StringEquals": {
          "iam:AWSServiceName": [
            "autoscaling.amazonaws.com",
            "spot.amazonaws.com"
          ]
        }
      }
    },
    {
      "Sid": "AutoScalingCreateTagged",
      "Effect": "Allow",
      "Action": [
        "autoscaling:CreateAutoScalingGroup",
        "autoscaling:CreateOrUpdateTags"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "AutoScalingChangeOwned",
      "Effect": "Allow",
      "Action": [
        "autoscaling:UpdateAutoScalingGroup",
        "autoscaling:DeleteAutoScalingGroup",
        "autoscaling:SuspendProcesses",
        "autoscaling:SetDesiredCapacity",
        "autoscaling:TerminateInstanceInAutoScalingGroup"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "AutoScalingRead",
      "Effect": "Allow",
      "Action": [
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeScalingActivities"
      ],
      "Resource": "*"
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
        "sqs:ListQueueTags"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-*"
    },
    {
      "Sid": "ListQueues",
      "Effect": "Allow",
      "Action": "sqs:ListQueues",
      "Resource": "*"
    },
    {
      "Sid": "VcpuQuotaPreflight",
      "Effect": "Allow",
      "Action": [
        "servicequotas:GetServiceQuota",
        "servicequotas:GetAWSDefaultServiceQuota",
        "cloudwatch:GetMetricData"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:TagResource",
        "logs:ListTagsForResource"
      ],
      "Resource": "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/*"
    },
    {
      "Sid": "LogsRead",
      "Effect": "Allow",
      "Action": "logs:DescribeLogGroups",
      "Resource": "*"
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
        "ssm:ListTagsForResource"
      ],
      "Resource": [
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/*",
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc"
      ]
    },
    {
      "Sid": "PublicAmiParameter",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-west-2::parameter/aws/service/ecs/optimized-ami/*"
    },
    {
      "Sid": "EcrInUseTags",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:PutImage",
        "ecr:BatchDeleteImage",
        "ecr:DescribeImages",
        "ecr:PutLifecyclePolicy"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/*"
    },
    {
      "Sid": "Identity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "EcrRead",
      "Effect": "Allow",
      "Action": [
        "ecr:DescribeRepositories",
        "ecr:ListTagsForResource"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/*"
    },
    {
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
        "scheduler:ListTagsForResource"
      ],
      "Resource": [
        "arn:aws:scheduler:us-west-2:123456789012:schedule-group/aegc-*",
        "arn:aws:scheduler:us-west-2:123456789012:schedule/aegc-*/*"
      ]
    },
    {
      "Sid": "ListSchedules",
      "Effect": "Allow",
      "Action": "scheduler:ListSchedules",
      "Resource": "*"
    },
    {
      "Sid": "TaggedResources",
      "Effect": "Allow",
      "Action": "tag:GetResources",
      "Resource": "*"
    }
  ]
}
```

- `ec2:RunInstances` is needed because creating an Auto Scaling group from a launch template checks
  that the caller could launch from it. It's allowed only on launch templates tagged
  `aegc:managed=true`, and on the other resources of a launch only when it comes from a launch
  template.
- `iam:CreateServiceLinkedRole` is limited to the Auto Scaling and Spot service-linked roles, which a
  fresh account doesn't have until its first group or Spot request.
- `EcrInUseTags` and `EcrRead` let `deploy` and `destroy` add and remove `inuse-` tags on `aegc/*`
  images and re-apply their lifecycle policy.
- `LeaseSchedules` and `ListSchedules` manage the `aegc-*` schedule groups and their schedules;
  `PassAegcRoles` lets `deploy` pass the scheduler role to them.
- `ec2:DescribeInstances`, `DescribeVolumes`, `DescribeNetworkInterfaces` and `tag:GetResources`
  are for `destroy`'s verification and `aegc footprint`.
- The deployer policy has no S3 access, so with it alone `aegc status` leaves out the spend
  figures, and `aegc cost` and `aegc limits --resume` are denied; use credentials with the
  [operator](#operator) policy for the deployment as well. Without
  the Price List and Spot price history reads, `status` prices running workers from aegc's static
  table.
- The region's resources need the separate [bootstrap](#bootstrap) policy, and image builds the
  [builder](#builder) policy.

## bootstrap

What `aegc bootstrap` and `unbootstrap` need: the payload bucket and its settings, the `aegc-quay`
pull-through cache rule, its repository creation template and role, deleting the cache's
repositories, and `/aegc/bootstrap`. Roles can be passed only to ECR.

```json
{
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
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": [
        "arn:aws:iam::123456789012:role/aegc/*",
        "arn:aws:iam::123456789012:instance-profile/aegc/*"
      ]
    },
    {
      "Sid": "PassAegcRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/aegc/*",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": [
            "ecr.amazonaws.com"
          ]
        }
      }
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
        "ssm:ListTagsForResource"
      ],
      "Resource": [
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/*",
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc"
      ]
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
        "s3:TagResource",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetBucketPublicAccessBlock",
        "s3:PutBucketOwnershipControls",
        "s3:PutEncryptionConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:PutBucketPolicy",
        "s3:GetBucketPolicy",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion"
      ],
      "Resource": [
        "arn:aws:s3:::aegc-123456789012-us-west-2",
        "arn:aws:s3:::aegc-123456789012-us-west-2/*"
      ]
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
        "ecr:ListTagsForResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EcrDeleteAegcRepos",
      "Effect": "Allow",
      "Action": "ecr:DeleteRepository",
      "Resource": [
        "arn:aws:ecr:us-west-2:123456789012:repository/aegc-quay/*",
        "arn:aws:ecr:us-west-2:123456789012:repository/aegc/*"
      ]
    },
    {
      "Sid": "Identity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

## builder

What `aegc image build`, `aegc image register` and `aegc image list` need: pull a base image
through the `aegc-quay` pull-through cache (the first pull of a base creates its cache repository),
create and push to `aegc/<env>` repositories, and read and record images under `/aegc/images/`.
The cache repositories are read-only to it. A `--base` or `register` image from another registry
is pulled with your own Docker login there, not with this policy.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PullBaseThroughCache",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchImportUpstreamImage",
        "ecr:CreateRepository"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc-quay/*"
    },
    {
      "Sid": "WorkerImageRepos",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
        "ecr:CreateRepository",
        "ecr:TagResource",
        "ecr:PutLifecyclePolicy",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "ecr:ListTagsForResource",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/*"
    },
    {
      "Sid": "ImageRecords",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:PutParameter",
        "ssm:AddTagsToResource",
        "ssm:ListTagsForResource"
      ],
      "Resource": "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/images/*"
    },
    {
      "Sid": "Identity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

## Scheduler role

`aegc deploy` creates `/aegc/aegc-<d>-scheduler-<region>` for the deployment's lease and TTL
schedules, unless the deployment has neither. It may only write the deployment's state parameter
and update Auto Scaling groups tagged with the deployment. Its trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "scheduler.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "123456789012"
        }
      }
    }
  ]
}
```

Its inline policy, `aegc-lease`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:PutParameter",
      "Resource": "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/state"
    },
    {
      "Effect": "Allow",
      "Action": "autoscaling:UpdateAutoScalingGroup",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e"
        }
      }
    }
  ]
}
```

## ECR creation template role

`aegc bootstrap` creates `/aegc/aegc-ecr-template-<region>` for the pull-through cache's repository
creation template, because ECR's default role is denied `ecr:TagResource` (the cache then fails
with only "repository not found"). Its trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ecr.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "123456789012"
        }
      }
    }
  ]
}
```

Its inline policy, `aegc-ecr-template`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:TagResource"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc-quay/*"
    }
  ]
}
```
