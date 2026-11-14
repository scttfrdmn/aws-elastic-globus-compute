# IAM policies

aegc uses three policies, printed by `aegc iam-policy {worker,operator,deployer}`. Each is scoped
to one account and region, and the worker and operator policies to one deployment. The documents
below were generated with:

```sh
aegc --region us-west-2 iam-policy <kind> --deployment e2e --account 123456789012
```

Generate your own with your account, region and deployment; with `--region` and `--account` given,
the command makes no AWS calls. The tests check these documents with the IAM policy simulator, and
IAM Access Analyzer reports no findings for them.

| Policy | Attach to | Used by |
|---|---|---|
| [worker](#worker) | Created by `aegc deploy` as the inline policy `aegc-worker` on the role `aegc-<d>-worker-<region>` | `aegc-worker` on worker instances |
| [operator](#operator) | The credentials the endpoint container runs with | `AWSEngine` |
| [deployer](#deployer) | The credentials you run `aegc bootstrap`, `deploy`, `destroy`, `status` and `reap` with | The `aegc` CLI |

## worker

What a worker may do: consume the deployment's task queues, post to its result queues, read task
payloads and write result payloads under `payloads/<d>/`, pull `aegc/*` images, write to its log
group, and terminate itself in an Auto Scaling group tagged with the deployment. Nothing else; in
particular no S3 access outside the payload prefix, so tasks must read open data unsigned.

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
    }
  ]
}
```

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

## operator

What `AWSEngine` needs at runtime: read the deployment's manifest, send tasks and read queue depth,
create and use its own result queues, move payloads, and raise desired capacity on the deployment's
Auto Scaling groups.

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
    }
  ]
}
```

The operator policy doesn't include `sqs:ListQueues`, which `aegc status` uses to find result
queues; run `status` with deployer credentials.

## deployer

A least-privilege starting point for `aegc bootstrap`, `unbootstrap`, `deploy`, `destroy`, `status`
and `reap`. Creates are conditioned on the request carrying `aegc:managed=true`, and changes and
deletes on the resource carrying it, where the service supports tag conditions. IAM actions are
limited to the `/aegc/` path, and roles can be passed only to EC2 and ECR.

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
            "ecr.amazonaws.com"
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
        "ec2:RunInstances"
      ],
      "Resource": "*"
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
        "autoscaling:SetDesiredCapacity"
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

Notes:

- `ec2:RunInstances` is allowed on all resources (in the `Ec2Read` statement): creating an Auto
  Scaling group from a launch template checks that the caller may launch instances.
- `aegc image build` isn't covered. From the calls it makes, it also needs
  `ecr:GetAuthorizationToken`; `ecr:CreateRepository`, `ecr:TagResource`, `ecr:PutLifecyclePolicy`,
  `ecr:DescribeImages` and the push actions (`ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`,
  `ecr:CompleteLayerUpload`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`) on `aegc/*`; and
  pull access to the `aegc-quay/*` cache repositories (`ecr:BatchGetImage`,
  `ecr:GetDownloadUrlForLayer`, and `ecr:BatchImportUpstreamImage` for the first pull of a base).
  This list hasn't been checked with the policy simulator.
- If the account has never used Auto Scaling, creating the first group also creates its
  service-linked role, which needs `iam:CreateServiceLinkedRole`; the deployer policy doesn't
  include it.

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
