# Resources and tags

Everything aegc creates in AWS, with its name, its `aegc:role` tag, and what creates and deletes
it. `<d>` is the deployment, `<pool>` the pool (`<env>-py<NNN>`), `<account>` and `<region>` the
account and region.

## Tags

Every resource gets these tags on its create call:

| Tag | Value |
|---|---|
| `aegc:managed` | `true` |
| `aegc:deployment` | the deployment name, or `_bootstrap` for region-wide resources and images |
| `aegc:role` | what the resource is (below) |
| `aegc:version` | the aegc version that created it, e.g. `0.1.0` |
| `aegc:pool` | the pool name, on pool resources |
| `aegc:endpoint-id` | the user endpoint process's id, on result queues |

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

## Images: aegc image build

Not deleted by any aegc command.

| Resource | Name | `aegc:role` |
|---|---|---|
| ECR repository (immutable tags, scan on push, keeps 10 images plus those tagged `inuse-`) | `aegc/<env>` | `image-repo` |
| SSM parameter: `{env, python_minor, image, tag, base, gce_version, aegc_version, built_at}` | `/aegc/images/<env>` | `image-record` |

## Deployment: aegc deploy

Deleted by `aegc destroy`.

| Resource | Name | `aegc:role` |
|---|---|---|
| IAM role, path `/aegc/`, inline policy `aegc-worker`, trusts EC2 | `aegc-<d>-worker-<region>` | `worker-role` |
| IAM instance profile, path `/aegc/` | `aegc-<d>-worker-<region>` | `worker-role` |
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

## Endpoint process: AWSEngine

| Resource | Name | `aegc:role` | Deleted by |
|---|---|---|---|
| SQS result queue (14-day retention, visibility 120 s, SSE) | `aegc-<d>-res-<first 12 hex of the endpoint id>` | `result-queue` | `aegc reap` (when empty and idle 14 days) or `aegc destroy` |
| S3 objects | `payloads/<d>/tasks/<task>/<attempt>.task`, `payloads/<d>/results/<task>/<task>:<attempt>.result` | — | the engine after use, else the 15-day lifecycle rule |

## Workers: aegc-worker

| Resource | Name | `aegc:role` | Deleted by |
|---|---|---|---|
| S3 objects (the [cost ledger](../concepts/cost.md#cost-ledger), one JSON record per instance) | `ledger/<d>/<yyyy-mm-dd>/<instance-id>.json`, dated by the instance's boot (UTC) | — | the 400-day lifecycle rule; not `aegc destroy` |

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
slots, idle timeout, volume, walltimes, lifetime, instance types) and the URLs and names of its
queues, launch template and group; `limits` (`max_usd_day`, `max_usd_month`,
`max_instance_hours_day`, `on_cap`); `lease` (`lease_s`, `ttl_at`); and `generation`, a hash of
the rest, which engines compare to notice a redeploy. `aegc limits` rewrites it. `AWSEngine` and
`aegc status` read it.

## State

`/aegc/<d>/state` is JSON: `mode` (`running`, `capped`, `stopped` or `expired`), `reason`, `since`,
`until` (for `capped`, the end of the cap's period), `by` (`aegc deploy`, `engine`, `aegc stop`,
`aegc resume`, `lease` or `ttl`) and `kill` (fail running tasks too). `aegc deploy` creates it;
engines, `aegc stop`, `aegc resume`, `aegc limits --resume` and the lease schedules overwrite it.
A missing parameter reads as `running`.
