# IAM policies

aegc uses nine policies, printed by
`aegc iam-policy {worker,operator,deployer,bootstrap,builder,task,endpoint,endpoint-deployer,lambda-deployer}`.
Each is scoped to one account and region, and the worker, operator, task and endpoint policies to
one deployment.
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
| [operator](#operator) | The credentials the endpoint container runs with | `AWSEngine`; also `aegc cost`, `aegc limits --resume`, `aegc provenance` and the spend lines of `aegc status` |
| [deployer](#deployer) | The credentials you run `aegc deploy`, `destroy`, `stop`, `resume`, `limits`, `status`, `metrics`, `reap` and `footprint` with | The `aegc` CLI |
| [bootstrap](#bootstrap) | The credentials you run `aegc bootstrap` and `unbootstrap` with | The `aegc` CLI |
| [builder](#builder) | The credentials you run `aegc image build`, `register`, `list` and `forget` with | The `aegc` CLI |
| [endpoint](#endpoint) | Created by `aegc endpoint deploy` as the inline policy `aegc-endpoint` on the role `aegc-<d>-endpoint-<region>` | The [cloud endpoint](../how-to/cloud-endpoint.md)'s host: `AWSEngine` and `aegc evaluate` |
| [endpoint-deployer](#endpoint-deployer) | The credentials you run `aegc endpoint deploy`, `undeploy` and `status` with, next to the deployer policy | The `aegc` CLI |
| [lambda-deployer](#lambda-deployer) | The credentials you run `aegc deploy`, `destroy`, `stop`, `resume`, `status` and `footprint` with on a deployment with [Lambda pools](../how-to/lambda-pools.md), next to the deployer policy | The `aegc` CLI |

`aegc deploy` also creates the [task role](#task-role), `aegc-<d>-task-<region>`, whose
credentials task code gets in [isolated](../concepts/security.md#task-isolation) pools, with its
inline policy `aegc-task`. With [grants](../how-to/data.md), it adds the inline policy
[`aegc-grants`](#aegc-grants) for your own S3 prefixes to the task role (and to the worker role
while pools with images from before aegc 0.4 remain). For each Lambda pool it creates a
[function role](#function-role), `aegc-<d>-<pool>-<region>`, with the inline policy
`aegc-function` and the grants. Two more roles of aegc's own are shown at the end: the
[scheduler role](#scheduler-role) for the deployment lease, and (from `aegc bootstrap`) the
[ECR creation template role](#ecr-creation-template-role).

## Why so many

Customer managed policies are limited to 6,144 characters, not counting whitespace. In v0.1 one
deployer policy covered `bootstrap` as well as `deploy` and `destroy`; with v0.2's additions (the
lease's schedules, in-use image tags, and the reads `destroy --verify` and `footprint` need) it no
longer fits. It's now split by when you need it: **bootstrap** for the region-wide resources, used
once per region, and **deployer** for everything about a deployment. Attach both to an
administrator's credentials, or give the bootstrap policy only to whoever sets up the region.

The cloud endpoint's secret, alarm and topic are in a separate **endpoint-deployer** policy for
the same reason: the deployer policy is already within 300 characters of the limit. And only
whoever moves endpoints to the cloud needs it. Lambda pools' functions, mappings and repository
policies are in **lambda-deployer**, for the same reasons.

Sizes of the documents below, without whitespace:

| Policy | Characters | Limit |
|---|---|---|
| worker | 2,192 | 10,240 for all of the role's inline policies, `aegc-grants` included |
| task (`aegc-task`) | 182 | 10,240 for all of the role's inline policies, `aegc-grants` included |
| operator | 3,962 | 6,144 |
| deployer | 5,841 | 6,144 |
| bootstrap | 2,311 | 6,144 |
| builder | 1,142 | 6,144 |
| endpoint (`aegc-endpoint`) | 5,120 (5,350 with `--session-manager`) | 10,240 for the role's inline policies |
| endpoint-deployer | 1,623 | 6,144 |
| lambda-deployer | 2,209 | 6,144 |
| function (`aegc-function`), for the pool `geospatial-py314-lambda` | 1,254 | 10,240 for all of the role's inline policies, `aegc-grants` included |

The deployer is the closest to the limit; longer region names make it a little longer.

## worker

What a worker may do: consume the deployment's task queues, post to its result queues, read task
payloads and write result payloads under `payloads/<d>/`, pull `aegc/*` images, write to its log
group, terminate itself in a worker Auto Scaling group of the deployment (tagged with the
deployment and `aegc:role=asg`, so never the [cloud endpoint](#endpoint)'s group), read the deployment's
state and manifest (for [`aegc.outputs`](../how-to/data.md#return-large-outputs)' current
grants on workers from before aegc 0.4), write its cost ledger record under `ledger/<d>/`, and
assume the deployment's [task role](#task-role) (`AssumeTaskRole`), whose credentials it hands to
task processes. Nothing else; in particular no S3 access outside those prefixes, so tasks read
open data unsigned, and reach your own buckets only through [grants](#aegc-grants).

`ShrinkToLeave` exists because a vCPU-weighted group refuses a decrement larger than its desired
capacity (desired 1 vCPU, a 2-vCPU worker): the worker lowers desired by its own weight, then
terminates without a decrement. The agent only ever lowers it, but `SetDesiredCapacity` has no
condition on the value, so code running as the worker role could also raise a group's desired
capacity, up to its `max_vcpus`. Task code runs as the worker role only in pools with images from
before aegc 0.4 (see [Security model](../concepts/security.md#pools-with-older-images)).

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
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "asg"
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
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "asg"
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
      "Sid": "AssumeTaskRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::123456789012:role/aegc/aegc-e2e-task-us-west-2"
    },
    {
      "Sid": "ReadDeploymentState",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": [
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/state",
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/manifest"
      ]
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

`aegc deploy` puts this inline policy back whenever it differs from the role's (and reports
`exists` when it doesn't), so redeploying an older deployment gives its workers the new
permissions, such as v0.4's `AssumeTaskRole`.

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

## Task role

`aegc deploy` creates `/aegc/aegc-<d>-task-<region>` (tag `aegc:role=task-role`, maximum session
an hour) for task code. The worker agent of an [isolated](../concepts/security.md#task-isolation)
pool assumes it and serves its credentials to task processes on `127.0.0.1`. It trusts only the
deployment's worker role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:role/aegc/aegc-e2e-worker-us-west-2"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Its inline policy `aegc-task` lets task code read the deployment's manifest, where
[`aegc.outputs`](../how-to/data.md#return-large-outputs) finds the current grants, and nothing
else: no queues, payloads, cost ledger, deployment state or Auto Scaling.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadManifest",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/manifest"
    }
  ]
}
```

With grants it also has [`aegc-grants`](#aegc-grants). `aegc iam-policy task` prints
`aegc-task`; the trust policy and `aegc-grants` come from `deploy`, which writes them. A new worker role can take a few seconds to become a valid
principal, so `deploy` retries the trust policy while IAM reports `MalformedPolicyDocument`.
`aegc destroy` deletes the role with its inline policies.

## aegc-grants

The inline policy for the deployment's [grants](../how-to/data.md): `aegc deploy --grant read=...`
and `--grant write=...`. It goes on the [task role](#task-role), and also on the worker role while
any pool runs an image from before aegc 0.4, or any worker launched before its pool was isolated
(without the instance tag `aegc:isolated=true`) is still running. It exists only while the
deployment has grants: `deploy` puts it after `aegc-worker` and `aegc-task`, `--grant none`
deletes it, and `aegc destroy` deletes it with the roles. `aegc iam-policy` doesn't print it; it
depends on the grants. For

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
      ],
      "Condition": {
        "StringEquals": {
          "s3:ResourceAccount": "123456789012"
        }
      }
    }
  ]
}
```

- All read grants share one `GrantRead` statement, and all write grants one `GrantWrite`. Each
  bucket with read grants gets a `GrantList<n>` statement, conditioned on its prefixes; a
  whole-bucket grant (`s3://bucket/`) drops the condition.
- Write grants don't allow `GetObject`, `DeleteObject` or listing, and only reach buckets owned
  by the deployment's account (`s3:ResourceAccount`), so a mistyped or squatted bucket name in
  another account can't receive task outputs. Read grants may cross accounts.
- At most 32 grants: they also go into each worker's user data.
- Grants can't contain `*`, `?` or `${`, so the only wildcard is the `*` aegc appends to each
  prefix.
- IAM counts all of a role's inline policies against 10,240 characters, without whitespace.
  `aegc-worker` takes about 2,190 and `aegc-task` about 180, and `deploy` refuses grants that
  don't fit next to either, before it changes anything. The policy above is 564 characters.
- Requester-pays buckets need nothing more. Buckets in another account (read grants only) must
  also allow the task role in their bucket policy (and the worker role while pools with older
  images remain), and buckets encrypted with a customer managed KMS key need `kms:Decrypt` or
  `kms:GenerateDataKey` on the key, which aegc doesn't add.
- `deploy` compares the policy with the role's and reports `created`, `updated` or `exists`
  (`would-create`, `would-update` or `would-delete` in a dry run), labelled with the role, for
  example `aegc-grants (task role) (2 grant(s))`. While pre-0.4 workers still run on an otherwise
  isolated deployment, the worker role's copy is reported as `kept`.

The [deployer](#deployer) policy already allows `iam:PutRolePolicy` and `iam:DeleteRolePolicy` on roles
under `/aegc/`, so grants need no extra permissions to deploy.

## operator

What `AWSEngine` needs at runtime: read the deployment's parameters (manifest and state), send
tasks and read queue depth, create and use its own result queues, move payloads, and raise desired
capacity on the deployment's worker Auto Scaling groups. Every Auto Scaling statement here and in
the worker policy requires the group's tags `aegc:deployment=<d>` and `aegc:role=asg`, so neither
policy can change the [cloud endpoint](#endpoint)'s group (`aegc:role=endpoint-asg`). For spend
limits and the lease it also:

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
  Scheduler (`RenewLease`, `ListLease`, `PassLeaseRole`), to renew the lease;
- enables and disables the event source mappings of the deployment's
  [Lambda pools](../how-to/lambda-pools.md) (`FreezeLambdaPools`), to freeze them on a cap and
  turn them back on when the deployment runs again. Only mappings that feed the deployment's
  functions (`lambda:FunctionArn` is `aegc-<d>-*`) **and** carry `aegc:deployment=<d>`, the same
  conditions as the scheduler role's;
- reads the functions' configuration and the mappings' state (`ReadLambdaPools`) and lists
  mappings (`lambda:ListEventSourceMappings`, in `FindResultQueues`). The reads carry no
  conditions: the Lambda guide lists none for `GetEventSourceMapping`, and a denied read would
  leave a frozen pool frozen.

For [`aegc provenance`](cli.md#aegc-provenance), it may also search the deployment's log group,
`/aegc/<d>`, and no other (`ReadTaskLogs`, `logs:FilterLogEvents`). `aegc metrics` uses
`cloudwatch:GetMetricData`, already in `VcpuQuotaPreflight`.

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
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/payloads/e2e/*"
    },
    {
      "Sid": "ScaleOut",
      "Effect": "Allow",
      "Action": "autoscaling:SetDesiredCapacity",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "asg"
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
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "asg"
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
      "Sid": "ReadTaskLogs",
      "Effect": "Allow",
      "Action": "logs:FilterLogEvents",
      "Resource": [
        "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/e2e",
        "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/e2e:*"
      ]
    },
    {
      "Sid": "FindResultQueues",
      "Effect": "Allow",
      "Action": [
        "sqs:ListQueues",
        "lambda:ListEventSourceMappings",
        "events:ListRules"
      ],
      "Resource": "*"
    },
    {
      "Sid": "FreezeLambdaPools",
      "Effect": "Allow",
      "Action": "lambda:UpdateEventSourceMapping",
      "Resource": "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*",
      "Condition": {
        "ArnLike": {
          "lambda:FunctionArn": "arn:aws:lambda:us-west-2:123456789012:function:aegc-e2e-*"
        },
        "StringEquals": {
          "aws:ResourceTag/aegc:deployment": "e2e"
        }
      }
    },
    {
      "Sid": "ReadLambdaPools",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunctionConfiguration",
        "lambda:GetEventSourceMapping"
      ],
      "Resource": [
        "arn:aws:lambda:us-west-2:123456789012:function:aegc-e2e-*",
        "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*"
      ]
    },
    {
      "Sid": "FreezeTriggers",
      "Effect": "Allow",
      "Action": [
        "events:EnableRule",
        "events:DisableRule",
        "events:DescribeRule"
      ],
      "Resource": "arn:aws:events:us-west-2:123456789012:rule/aegc-e2e-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:deployment": "e2e"
        }
      }
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

`sqs:ListQueues`, `lambda:ListEventSourceMappings` and `scheduler:ListSchedules` have no
resource-level scoping. `aegc status` uses
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
`metrics`, `reap` and `footprint`. Creates are conditioned on the request carrying `aegc:managed=true`, and
changes and deletes on the resource carrying it, where the service supports tag conditions. IAM
actions are limited to the `/aegc/` path, and roles can be passed only to EC2 and EventBridge
Scheduler ([lambda-deployer](#lambda-deployer) passes them to Lambda).

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
        "ec2:DescribeInstanceTypeOfferings",
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
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        "autoscaling:EnableMetricsCollection"
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
  are for `destroy`'s verification and `aegc footprint`. `deploy` also uses `DescribeInstances` to
  find workers launched before their pool was isolated.
- `ec2:DescribeInstanceTypeOfferings` tells `deploy` which Availability Zones offer a pool's
  instance types, so it can leave subnets in other zones out of the pool's group
  ([Other regions](../how-to/operations.md#other-regions)). With a deployer policy from before
  0.6, `deploy` keeps every subnet and reports `kept  subnet offerings ... (... failed (deployer
  policy before 0.6?))`.
- The deployer policy covers the [cloud endpoint](#endpoint)'s role, instance profile, launch
  template and group too; its secret, alarm and topic need [endpoint-deployer](#endpoint-deployer).
- `autoscaling:EnableMetricsCollection` turns on the groups' 1-minute Auto Scaling metrics for
  [`aegc metrics`](cli.md#aegc-metrics). With a deployer policy from before 0.4, `deploy` reports
  `kept  auto scaling group metrics ... (not enabled: AccessDenied)` and carries on.
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

What `aegc image build`, `register`, `list` and `forget` need: pull a base image through the
`aegc-quay` pull-through cache (the first pull of a base creates its cache repository), create and
push to `aegc/<env>` repositories, and read, write and delete image records under
`/aegc/images/`. The cache repositories are read-only to it. A `--base` or `register` image from
another registry is pulled with your own Docker login there; one in this account's ECR gets an ECR
login from aegc, and needs pull access to that repository.

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
        "ssm:DeleteParameter",
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

## endpoint

The role of the [cloud endpoint](../how-to/cloud-endpoint.md)'s host,
`/aegc/aegc-<d>-endpoint-<region>`, with its instance profile of the same name and the inline
policy `aegc-endpoint`. It trusts EC2, like the worker role. The host runs `AWSEngine`, so the
policy starts with the whole [operator](#operator) policy, and adds:

- `ReadOwnLogin`: `GetSecretValue` on its own secret, `aegc-<d>-endpoint`. The secret's resource
  policy denies it to everyone else.
- `PullEndpointImage` and `EcrAuth`: pull from `aegc/endpoint`.
- `EndpointLogs`: write its log streams in `/aegc/<d>`.
- `ReplaceSelf`: `SetInstanceHealth` on its own group (tagged `aegc:role=endpoint-asg`), so a boot
  that fails marks the instance unhealthy and the group replaces it.
- `LivenessMetric`: `PutMetricData` in the namespace `aegc/endpoint` only, for `EndpointOnline`.
  Workers have no `PutMetricData` at all.
- `ReapResultQueues`: `DeleteQueue` on the deployment's result queues, for `aegc evaluate`.

`aegc iam-policy endpoint --deployment <d>` prints it:

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
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::aegc-123456789012-us-west-2/payloads/e2e/*"
    },
    {
      "Sid": "ScaleOut",
      "Effect": "Allow",
      "Action": "autoscaling:SetDesiredCapacity",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "asg"
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
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "asg"
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
      "Sid": "ReadTaskLogs",
      "Effect": "Allow",
      "Action": "logs:FilterLogEvents",
      "Resource": [
        "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/e2e",
        "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/e2e:*"
      ]
    },
    {
      "Sid": "FindResultQueues",
      "Effect": "Allow",
      "Action": [
        "sqs:ListQueues",
        "lambda:ListEventSourceMappings",
        "events:ListRules"
      ],
      "Resource": "*"
    },
    {
      "Sid": "FreezeLambdaPools",
      "Effect": "Allow",
      "Action": "lambda:UpdateEventSourceMapping",
      "Resource": "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*",
      "Condition": {
        "ArnLike": {
          "lambda:FunctionArn": "arn:aws:lambda:us-west-2:123456789012:function:aegc-e2e-*"
        },
        "StringEquals": {
          "aws:ResourceTag/aegc:deployment": "e2e"
        }
      }
    },
    {
      "Sid": "ReadLambdaPools",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunctionConfiguration",
        "lambda:GetEventSourceMapping"
      ],
      "Resource": [
        "arn:aws:lambda:us-west-2:123456789012:function:aegc-e2e-*",
        "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*"
      ]
    },
    {
      "Sid": "FreezeTriggers",
      "Effect": "Allow",
      "Action": [
        "events:EnableRule",
        "events:DisableRule",
        "events:DescribeRule"
      ],
      "Resource": "arn:aws:events:us-west-2:123456789012:rule/aegc-e2e-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:deployment": "e2e"
        }
      }
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
      "Sid": "ReadOwnLogin",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aegc-e2e-endpoint-??????"
    },
    {
      "Sid": "PullEndpointImage",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/endpoint"
    },
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EndpointLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-west-2:123456789012:log-group:/aegc/e2e:*"
    },
    {
      "Sid": "ReplaceSelf",
      "Effect": "Allow",
      "Action": "autoscaling:SetInstanceHealth",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:deployment": "e2e",
          "autoscaling:ResourceTag/aegc:role": "endpoint-asg"
        }
      }
    },
    {
      "Sid": "LivenessMetric",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "aegc/endpoint"
        }
      }
    },
    {
      "Sid": "ReapResultQueues",
      "Effect": "Allow",
      "Action": "sqs:DeleteQueue",
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-res-*"
    }
  ]
}
```

With `aegc endpoint deploy --session-manager` it also has a `SessionManager` statement
(`ssm:UpdateInstanceInformation` and the four `ssmmessages` channel actions, on `*`), so Session
Manager can open shells on the host. Whoever may `ssm:StartSession` there gets root on the host,
and with it the stored Globus login; it's off unless you ask for it.

## endpoint-deployer

What `aegc endpoint deploy`, `undeploy` and `status` need besides the [deployer](#deployer) policy,
which already covers the host's role, launch template and group: the login secret, the liveness
metric's first datapoint, the instance refresh, the offline alarm and its SNS topic. Attach it
next to the deployer policy, to whoever moves endpoints to the cloud.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CreateLoginTagged",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:TagResource"
      ],
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aegc-*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ChangeOwnedLogin",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret",
        "secretsmanager:PutResourcePolicy",
        "secretsmanager:UntagResource"
      ],
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aegc-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ListLogins",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:ListSecrets",
        "sns:ListTopics"
      ],
      "Resource": "*"
    },
    {
      "Sid": "LivenessSeed",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "aegc/endpoint"
        }
      }
    },
    {
      "Sid": "RollEndpointHost",
      "Effect": "Allow",
      "Action": [
        "autoscaling:StartInstanceRefresh",
        "autoscaling:SuspendProcesses"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/aegc:role": "endpoint-asg"
        }
      }
    },
    {
      "Sid": "OfflineAlarm",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DeleteAlarms",
        "cloudwatch:DescribeAlarms",
        "cloudwatch:TagResource",
        "cloudwatch:ListTagsForResource"
      ],
      "Resource": "arn:aws:cloudwatch:us-west-2:123456789012:alarm:aegc-*"
    },
    {
      "Sid": "AlarmTopic",
      "Effect": "Allow",
      "Action": [
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:Subscribe",
        "sns:GetTopicAttributes",
        "sns:ListTagsForResource",
        "sns:TagResource",
        "sns:ListSubscriptionsByTopic"
      ],
      "Resource": "arn:aws:sns:us-west-2:123456789012:aegc-*"
    }
  ]
}
```

- It never allows `secretsmanager:GetSecretValue`: only the endpoint host reads the login. `status`
  reads the endpoint's name and id from the secret's tags (`ListSecrets`), and `deploy` finds
  whether another deployment already runs the same endpoint id the same way.
- Secrets are created only with the request tag `aegc:managed=true`, and changed or deleted only
  when they carry it.
- `aegc destroy` works without this policy, but leaves a cloud endpoint's secret, alarm and topic
  (and, in that case, its role) in place; see
  [Destroy a deployment](../how-to/operations.md#destroy-a-deployment).

## lambda-deployer

What `aegc deploy`, `destroy`, `stop`, `resume`, `status` and `footprint` need for
[Lambda pools](../how-to/lambda-pools.md), besides the [deployer](#deployer) policy, which already
covers their queues and function roles (under its `/aegc/` IAM statement). Attach it next to the
deployer policy, to whoever deploys Lambda pools.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CreateFunctionsTagged",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction",
        "lambda:TagResource"
      ],
      "Resource": "arn:aws:lambda:us-west-2:123456789012:function:aegc-*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ChangeOwnedFunctions",
      "Effect": "Allow",
      "Action": [
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:DeleteFunction"
      ],
      "Resource": "arn:aws:lambda:us-west-2:123456789012:function:aegc-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ReadFunctions",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:ListTags",
        "lambda:GetEventSourceMapping"
      ],
      "Resource": [
        "arn:aws:lambda:us-west-2:123456789012:function:aegc-*",
        "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*"
      ]
    },
    {
      "Sid": "CreateMappingsTagged",
      "Effect": "Allow",
      "Action": "lambda:CreateEventSourceMapping",
      "Resource": "*",
      "Condition": {
        "ArnLike": {
          "lambda:FunctionArn": "arn:aws:lambda:us-west-2:123456789012:function:aegc-*"
        },
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "TagMappingsOnCreate",
      "Effect": "Allow",
      "Action": "lambda:TagResource",
      "Resource": "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ChangeMappings",
      "Effect": "Allow",
      "Action": [
        "lambda:UpdateEventSourceMapping",
        "lambda:DeleteEventSourceMapping"
      ],
      "Resource": "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*",
      "Condition": {
        "ArnLike": {
          "lambda:FunctionArn": "arn:aws:lambda:us-west-2:123456789012:function:aegc-*"
        }
      }
    },
    {
      "Sid": "ListLambda",
      "Effect": "Allow",
      "Action": [
        "lambda:ListFunctions",
        "lambda:ListEventSourceMappings",
        "iam:ListRoles"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassFunctionRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/aegc/aegc-*",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "lambda.amazonaws.com"
        }
      }
    },
    {
      "Sid": "ImagesForLambda",
      "Effect": "Allow",
      "Action": [
        "ecr:GetRepositoryPolicy",
        "ecr:SetRepositoryPolicy",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:DescribeImages"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/*"
    },
    {
      "Sid": "CreateTriggerRulesTagged",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:TagResource"
      ],
      "Resource": "arn:aws:events:us-west-2:123456789012:rule/aegc-*-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ChangeOwnedTriggerRules",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:DeleteRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "events:EnableRule",
        "events:DisableRule"
      ],
      "Resource": "arn:aws:events:us-west-2:123456789012:rule/aegc-*-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ReadTriggerRules",
      "Effect": "Allow",
      "Action": [
        "events:DescribeRule",
        "events:ListTargetsByRule",
        "events:ListTagsForResource"
      ],
      "Resource": "arn:aws:events:us-west-2:123456789012:rule/aegc-*-trigger-*"
    },
    {
      "Sid": "ListTriggerRules",
      "Effect": "Allow",
      "Action": [
        "events:ListRules",
        "secretsmanager:ListSecrets",
        "sqs:ListQueues"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TriggerFunctions",
      "Effect": "Allow",
      "Action": [
        "lambda:AddPermission",
        "lambda:RemovePermission",
        "lambda:GetPolicy",
        "lambda:PutFunctionConcurrency",
        "lambda:PutFunctionEventInvokeConfig",
        "lambda:GetFunctionEventInvokeConfig"
      ],
      "Resource": "arn:aws:lambda:us-west-2:123456789012:function:aegc-*-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "CreateTriggerLoginsTagged",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:TagResource"
      ],
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aegc-*-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "ChangeOwnedTriggerLogins",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret",
        "secretsmanager:PutResourcePolicy"
      ],
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aegc-*-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:managed": "true"
        }
      }
    },
    {
      "Sid": "TriggerDeadLetters",
      "Effect": "Allow",
      "Action": [
        "sqs:CreateQueue",
        "sqs:TagQueue",
        "sqs:ListQueueTags",
        "sqs:GetQueueUrl",
        "sqs:GetQueueAttributes",
        "sqs:SetQueueAttributes",
        "sqs:DeleteQueue"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-*-trigger-*-dlq"
    },
    {
      "Sid": "BucketEventBridgeDelivery",
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketNotification",
        "s3:PutBucketNotification"
      ],
      "Resource": "arn:aws:s3:::*"
    },
    {
      "Sid": "TriggerActivity",
      "Effect": "Allow",
      "Action": "cloudwatch:GetMetricData",
      "Resource": "*"
    }
  ]
}
```

- Functions are created only as `aegc-*` with the request tag `aegc:managed=true`, and changed or
  deleted only when they carry it. Mappings are created, changed and deleted only for `aegc-*`
  functions, and created only with the tag.
- Reads aren't tag-conditioned, so a function that doesn't exist reads as missing rather than
  denied.
- `iam:PassRole` is limited to `/aegc/aegc-*` roles passed to Lambda.
- `ImagesForLambda` reads and sets the `aegc/*` repositories' policies, for the
  [`AegcLambdaPull`](resources.md#lambda-pool-aegc-deploy-pool-envlambda) statement; the pull actions are what
  `CreateFunction`'s caller needs; `DescribeImages` checks the image's size.
- `stop` and `resume` use `ChangeMappings` to disable and enable the mappings; `status` reads them
  with `ReadFunctions`; `destroy` and `footprint` list functions, mappings and `/aegc/` roles with
  `ListLambda`.
- `aegc destroy` works without this policy, but leaves the Lambda pools' mappings, functions and
  function roles in place; see [Destroy a deployment](../how-to/operations.md#destroy-a-deployment).
- The statements from `CreateTriggerRulesTagged` on are for [triggers](../how-to/triggers.md):
  rules `aegc-*-trigger-*` created with the tag and changed or deleted only when they carry it;
  the functions' invoke permission, reserved concurrency and failure destination; the stored
  logins (created, replaced and deleted, never read: only the trigger's function reads its login);
  the dead-letter queues; and `s3:GetBucketNotification` / `s3:PutBucketNotification` on any
  bucket, for the read-merge that turns on EventBridge delivery. Your bucket's own policy still
  decides: grant those two on your bucket if it restricts them.
- `stop` and `resume` also disable and enable the trigger rules (`ChangeOwnedTriggerRules`).
- `aegc destroy` without this policy leaves triggers in place and reports them as remaining.

## Trigger role

`aegc trigger create` creates `/aegc/aegc-<d>-trigger-<name>-<region>` for each
[trigger](../how-to/triggers.md); its function runs with it. It trusts `lambda.amazonaws.com` for
functions in the account only (`aws:SourceAccount`), and its inline policy `aegc-trigger` reads
the trigger's own stored login, writes the function's log streams, and sends failed events to
its dead-letter queue. It has no S3 access: the flow does the work, as you. For trigger `uploads`
of deployment `e2e`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOwnLogin",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aegc-e2e-trigger-uploads-??????"
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
      "Sid": "DeadLetters",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-trigger-uploads-dlq"
    }
  ]
}
```

The login's secret also has a resource policy that denies `secretsmanager:GetSecretValue` to every
principal except this role.

## Function role

`aegc deploy` creates `/aegc/aegc-<d>-<pool>-<region>` for each [Lambda pool](../how-to/lambda-pools.md).
The function runs with it, and so does task code: Lambda pools aren't
[isolated](../concepts/security.md#lambda-pools). Its inline policy `aegc-function` covers what
`aegc-worker lambda` does for one pool. The deployment's grants go next to it as `aegc-grants`.
Its trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

`aegc-function`, for the pool `geospatial-py314-lambda` (the policy isn't printed by
`aegc iam-policy`, since `deploy` writes it):

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
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:us-west-2:123456789012:aegc-e2e-geospatial-py314-lambda-tasks"
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
      "Sid": "ReadDeploymentState",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": [
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/state",
        "arn:aws:ssm:us-west-2:123456789012:parameter/aegc/e2e/manifest"
      ]
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
      "Sid": "EcrPull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:us-west-2:123456789012:repository/aegc/geospatial"
    }
  ]
}
```

- `ConsumeTasks` is the pool's own task queue only. The event source mapping receives with this
  role, and the handler deletes and releases messages.
- `PostResults`, `ReadTaskPayloads` and `WriteResultPayloads` cover the whole deployment, not just
  the pool: this is what lets a task on a Lambda pool read other pools' task payloads and forge
  results ([#80](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/80)).
- It has no cost ledger, Auto Scaling, task role or state writes: Lambda spend comes from
  Lambda's own metrics.
- `EcrPull` lets the function pull its image; the repository's `AegcLambdaPull` statement does
  the same for the Lambda service.

## Scheduler role

`aegc deploy` creates `/aegc/aegc-<d>-scheduler-<region>` for the deployment's lease and TTL
schedules, unless the deployment has neither. It may only write the deployment's state parameter,
update Auto Scaling groups tagged with the deployment, and update event source mappings that are
tagged with the deployment and feed its functions (the `lease-<pool>` schedules of Lambda pools
disable them), and disable [trigger](../how-to/triggers.md) rules tagged with the deployment (the
`lease-trigger-<name>` schedules). Every deploy and `aegc trigger create` rewrites its policy. Its trust policy:

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
    },
    {
      "Effect": "Allow",
      "Action": "lambda:UpdateEventSourceMapping",
      "Resource": "arn:aws:lambda:us-west-2:123456789012:event-source-mapping:*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:deployment": "e2e"
        },
        "ArnLike": {
          "lambda:FunctionArn": "arn:aws:lambda:us-west-2:123456789012:function:aegc-e2e-*"
        }
      }
    },
    {
      "Effect": "Allow",
      "Action": "events:DisableRule",
      "Resource": "arn:aws:events:us-west-2:123456789012:rule/aegc-e2e-trigger-*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/aegc:deployment": "e2e"
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
