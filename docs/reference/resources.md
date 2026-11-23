# Resources and tags

Everything aegc creates in AWS, with its name, its `aegc:role` tag, and what creates and deletes
it. `<d>` is the deployment, `<pool>` the pool (`<env>-py<NNN>`, `<env>-py<NNN>-<size>` with a
[size class](configuration.md#size-classes), or `<env>-py<NNN>-lambda` for a
[Lambda pool](#lambda-pool-aegc-deploy-pool-envlambda)), `<account>` and `<region>` the account and region.

## Tags

Every resource gets these tags on its create call:

| Tag | Value |
|---|---|
| `aegc:managed` | `true` |
| `aegc:deployment` | the deployment name, or `_bootstrap` for region-wide resources and images |
| `aegc:role` | what the resource is (below) |
| `aegc:version` | the aegc version that created it, e.g. `0.1.0` |
| `aegc:pool` | the pool name, on pool resources |
| `aegc:endpoint-id` | the user endpoint process's id, on result queues; the endpoint's id, on a cloud endpoint's secret |

User tags can't use the `aegc:` prefix.

**The deletion rule.** aegc deletes a resource only if both hold:

1. its name has aegc's prefix for the deployment (`aegc-<d>-…` or `aegc-<d>`; `aegc-…` for
   `_bootstrap`), and
2. its tags include `aegc:managed=true` and `aegc:deployment=<d>`.

A resource that has an aegc name but not the tags is never adopted, changed or deleted: `bootstrap`
and `deploy` refuse with an ownership error, and `destroy` and `unbootstrap` report it as `kept`.
The pull-through cache rule can't be tagged, so `unbootstrap` relies on the `/aegc/bootstrap`
record for it.

## Region-wide: aegc bootstrap

Deleted by `aegc unbootstrap`.

| Resource | Name | `aegc:role` |
|---|---|---|
| S3 bucket (payloads) | `aegc-<account>-<region>` | `bucket` |
| IAM role, path `/aegc/`, inline policy `aegc-ecr-template` | `aegc-ecr-template-<region>` | `ecr-template-role` |
| ECR pull-through cache rule → `quay.io` | prefix `aegc-quay` | untaggable |
| ECR repository creation template | prefix `aegc-quay` | tags the repositories it creates |
| ECR repositories created by the cache | `aegc-quay/<source>/<env>` | `cache-repo` |
| SSM parameter | `/aegc/bootstrap` | `manifest` |

The bucket has Block Public Access, bucket-owner-enforced ownership, SSE-S3, a TLS-only bucket
policy, and two lifecycle rules: `aegc-expire-payloads` expires `payloads/` after 15 days, and
`aegc-expire-ledger` expires `ledger/` after 400 days. A bucket bootstrapped by v0.1 gets the ledger
rule the next time you run `aegc bootstrap`.

## Images: aegc image build and register

Not deleted by any aegc command.

| Resource | Name | `aegc:role` |
|---|---|---|
| ECR repository (immutable tags, scan on push, keeps 10 images plus those tagged `inuse-`) | `aegc/<env>` | `image-repo` |
| SSM parameter: `{"records": {<minor>: {env, python_minor, image, tag, base, gce_version, aegc_version, built_at, base_kind, python}}}`, one record per Python minor | `/aegc/images/<env>/records` | `image-record` |
| SSM parameter: the newest record, `{env, python_minor, image, tag, base, gce_version, aegc_version, built_at}`, in the shape aegc 0.1 and 0.2 read | `/aegc/images/<env>` | `image-record` |

`base_kind` is `conda`, `pip` or `registered`; `python` is the interpreter the worker runs; `base`
is the base or registered reference with its short image id. Every build and registration writes
both parameters, so 0.2 CLIs keep working. If one of them writes `/aegc/images/<env>`, the next
read merges it in (the newer record for its minor wins). `aegc image forget` rewrites both, or
deletes them with the last minor. `aegc image register` pushes your image into the same
repository; the image you registered from is left where it was.

## Deployment: aegc deploy

Deleted by `aegc destroy`.

| Resource | Name | `aegc:role` |
|---|---|---|
| IAM role, path `/aegc/`, inline policies `aegc-worker` and, with [grants](../how-to/data.md) while pools with images from before aegc 0.4 remain, `aegc-grants`; trusts EC2 | `aegc-<d>-worker-<region>` | `worker-role` |
| IAM instance profile, path `/aegc/` | `aegc-<d>-worker-<region>` | `worker-role` |
| IAM role, path `/aegc/`, inline policies `aegc-task` and, with grants, `aegc-grants`; trusts only the worker role; 1-hour sessions (see [Task role](iam.md#task-role)) | `aegc-<d>-task-<region>` | `task-role` |
| EC2 security group, no ingress, in the default VPC | `aegc-<d>-workers` | `sg` |
| CloudWatch log group, 14-day retention | `/aegc/<d>` | `logs` |
| SSM parameter (the manifest the engine reads) | `/aegc/<d>/manifest` | `manifest` |
| SSM parameter (the [deployment state](../concepts/architecture.md#deployment-state)) | `/aegc/<d>/state` | `state` |
| EventBridge Scheduler schedule group | `aegc-<d>` | `schedules` |
| EventBridge Scheduler schedules (one-shot, in the group; schedules can't be tagged) | `lease-state`, `lease-<pool>`, and with `--ttl`, `ttl-state`, `ttl-<pool>` | — |
| IAM role, path `/aegc/`, inline policy `aegc-lease`, trusts `scheduler.amazonaws.com` | `aegc-<d>-scheduler-<region>` | `scheduler` |
| ECR image tags on the images the pools use | `inuse-<d>-<first 12 hex of the digest>` in `aegc/<env>` | — |

The schedule group and role exist unless the deployment was made with `--lease none` and no TTL.
`deploy` adds the in-use tags and removes the deployment's older ones; `destroy` removes them all,
leaving the images.

## Pool: aegc deploy --pool

Deleted by `aegc destroy`. These also carry `aegc:pool=<pool>`.

| Resource | Name | `aegc:role` |
|---|---|---|
| SQS task queue (visibility 300 s, 20 s long poll, 14-day retention, SSE) | `aegc-<d>-<pool>-tasks` | `task-queue` |
| SQS dead-letter queue (receives after 6 deliveries) | `aegc-<d>-<pool>-dlq` | `dlq` |
| EC2 launch template | `aegc-<d>-<pool>` | `lt` |
| Auto Scaling group | `aegc-<d>-<pool>` | `asg` |
| EC2 instances, EBS volumes, network interfaces (launched by the group) | — | `worker` |

The group's tags propagate to instances except `aegc:role`, so instances keep `aegc:role=worker`
from the launch template. Launch template versions record a content hash in their description, so
a redeploy with no changes adds no version.

Workers of pools that [isolate tasks](../concepts/security.md#task-isolation) also carry
`aegc:isolated=true`, which `deploy` uses to tell when no worker runs tasks with the worker role
any more. Groups have 1-minute group metrics enabled.

## Lambda pool: aegc deploy --pool env:lambda

Deleted by `aegc destroy` and `aegc deploy --prune --yes` (with the
[lambda-deployer](iam.md#lambda-deployer) policy). These also carry `aegc:pool=<pool>`.

| Resource | Name | `aegc:role` |
|---|---|---|
| SQS task queue (visibility 6 × the function's timeout, at most 12 h; otherwise as above) | `aegc-<d>-<pool>-tasks` | `task-queue` |
| SQS dead-letter queue (receives after 6 deliveries) | `aegc-<d>-<pool>-dlq` | `dlq` |
| IAM role, path `/aegc/`, inline policies `aegc-function` and, with grants, `aegc-grants`; trusts `lambda.amazonaws.com` (see [Function role](iam.md#function-role)) | `aegc-<d>-<pool>-<region>` | `function-role` |
| Lambda function (image, arm64, command `aegc-worker lambda`, logs to `/aegc/<d>`) | `aegc-<d>-<pool>` | `function` |
| Lambda event source mapping from the task queue to the function (batch size 1, `ReportBatchItemFailures`, `MaximumConcurrency`) | a UUID | `mapping` |
| EventBridge Scheduler schedules (with the deployment's others) | `lease-<pool>`, `ttl-<pool>`: disable the mapping | — |
| CloudWatch log streams in `/aegc/<d>` | Lambda's own, `<yyyy>/<mm>/<dd>/[$LATEST]<id>` | — |

Ownership is checked as for every resource: a mapping counts as aegc's only if its function has
aegc's name for the deployment and the mapping carries the tags. Lambda pools have no launch
template, group or instances, and no cost ledger records.

**ECR repository policy.** For each env with a Lambda pool, `deploy` keeps a statement with the
Sid `AegcLambdaPull` in the `aegc/<env>` repository policy: the Lambda service
(`lambda.amazonaws.com`) may `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer` for source ARNs
`arn:aws:lambda:<region>:<account>:function:aegc-*`. Other statements are left as they are. It's
region-wide like the repository, so `destroy` leaves it, and no aegc command removes it.

## Endpoint process: AWSEngine

| Resource | Name | `aegc:role` | Deleted by |
|---|---|---|---|
| SQS result queue (14-day retention, visibility 120 s, SSE) | `aegc-<d>-res-<first 12 hex of the endpoint id>` | `result-queue` | `aegc reap` (when empty and idle 14 days) or `aegc destroy` |
| S3 objects | `payloads/<d>/tasks/<task>/<attempt>.task`, `payloads/<d>/results/<task>/<task>:<attempt>.result` | — | the engine after use, else the 15-day lifecycle rule |

## Workers: aegc-worker

| Resource | Name | `aegc:role` | Deleted by |
|---|---|---|---|
| S3 objects (the [cost ledger](../concepts/cost.md#cost-ledger), one JSON record per instance) | `ledger/<d>/<yyyy-mm-dd>/<instance-id>.json`, dated by the instance's boot (UTC) | — | the 400-day lifecycle rule; not `aegc destroy` |

## Tasks: aegc.outputs

| Resource | Name | Deleted by |
|---|---|---|
| S3 objects in your bucket, under a write grant | `<grant prefix>/<task uuid>/<name>` | you; aegc never deletes them, and never creates, changes or deletes the buckets it's granted |

## Cloud endpoint: aegc endpoint deploy

One per deployment, from [`aegc endpoint deploy`](../how-to/cloud-endpoint.md). Deleted by
`aegc endpoint undeploy --yes` or `aegc destroy` (the secret, alarm and topic need the
[endpoint-deployer](iam.md#endpoint-deployer) policy).

| Resource | Name | `aegc:role` |
|---|---|---|
| IAM role, path `/aegc/`, inline policy `aegc-endpoint`; trusts EC2 | `aegc-<d>-endpoint-<region>` | `endpoint-role` |
| IAM instance profile, path `/aegc/` | `aegc-<d>-endpoint-<region>` | `endpoint-role` |
| EC2 launch template (the deployment's security group, IMDSv2 with hop limit 2, 30 GiB encrypted gp3); also tagged `aegc:pool=endpoint` | `aegc-<d>-endpoint` | `lt` |
| Auto Scaling group: one instance, EC2 health checks, `AZRebalance` suspended | `aegc-<d>-endpoint` | `endpoint-asg` |
| EC2 instance and EBS volume (launched by the group; `t4g.micro` by default, on demand) | — | `endpoint` |
| Secrets Manager secret: JSON with `name`, `endpoint_id`, `storage_db_b64` (the Globus token storage) and `metric_token`; a resource policy denies reading it to all but the endpoint role; deleted without a recovery window. Also tagged `aegc:endpoint-id`, `aegc:endpoint-name`, `aegc:metric-token` and `aegc:login-sha256` | `aegc-<d>-endpoint` | `endpoint-secret` |
| CloudWatch alarm on `EndpointOnline` (namespace `aegc/endpoint`, dimensions `Deployment` and `Token`) | `aegc-<d>-endpoint-offline` | `endpoint-alarm` |
| SNS topic, with an email subscription per `--alarm-email` | `aegc-<d>-endpoint-alarm` | `endpoint-alarm` |
| ECR image tag on the endpoint image | `inuse-<d>-<first 12 hex of the digest>` in `aegc/endpoint` | — |
| CloudWatch log streams in `/aegc/<d>` | `endpoint/<instance-id>`, `endpoint/<instance-id>/evaluate` | — |

The ECR repository `aegc/endpoint` (immutable tags, scan on push, the worker repositories'
lifecycle policy; `aegc:role=image-repo`, `aegc:deployment=_bootstrap`) is created by the first
`endpoint deploy` in the region and, like worker image repositories, not deleted by any aegc
command. The env name `endpoint` is reserved for it. On the instance, the endpoint's files are
under `/var/lib/aegc-endpoint/`, and the boot log is `/var/log/aegc-boot.log`.

## On your machine: aegc endpoint

| Resource | Name |
|---|---|
| Docker image | `aegc-endpoint` |
| Docker volume (Globus login, endpoint config) | `aegc-endpoint-home` |
| Docker container | `aegc-endpoint-<name>` (removed by `aegc endpoint stop`) |

## Manifest

`/aegc/<d>/manifest` is JSON written at the end of each `aegc deploy`. It holds the deployment,
region, account, bucket, instance profile ARN, security group and subnets; for each pool its
settings (env, Python, image digest, `min_vcpus`/`max_vcpus` and `capacity_type: vcpu`, sizes,
slots, idle timeout, volume, walltimes, lifetime, instance types, and the size class:
`size_class`, `task_cpus`, `task_memory_gib`, `memory_gib_per_vcpu_min`, and `isolate_tasks`,
true for images with aegc 0.4 or later) and the URLs and names
of its queues, launch template and group (for a Lambda pool, `kind: lambda`, `memory_mib`,
`timeout_s`, `max_concurrency`, `ephemeral_mib`, the walltimes, the queues, `function_name`,
`function_arn`, `function_role_arn` and `event_source_mapping`, the mapping's UUID); `limits` (`max_usd_day`, `max_usd_month`,
`max_instance_hours_day`, `on_cap`); `lease` (`lease_s`, `ttl_at`); `grants`
(`{access, bucket, prefix}` each); and `generation`, a hash of the rest, which engines compare to notice a redeploy. `aegc limits` rewrites it. `AWSEngine` and
`aegc status` read it.

## State

`/aegc/<d>/state` is JSON: `mode` (`running`, `capped`, `stopped` or `expired`), `reason`, `since`,
`until` (for `capped`, the end of the cap's period), `by` (`aegc deploy`, `engine`, `evaluator`,
`aegc stop`, `aegc resume`, `lease` or `ttl`) and `kill` (fail running tasks too). `aegc deploy` creates it;
engines, `aegc evaluate`, `aegc stop`, `aegc resume`, `aegc limits --resume` and the lease schedules overwrite it.
A missing parameter reads as `running`.
