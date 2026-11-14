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
policy, and a lifecycle rule that expires `payloads/` after 15 days.

## Images: aegc image build

Not deleted by any aegc command.

| Resource | Name | `aegc:role` |
|---|---|---|
| ECR repository (immutable tags, scan on push, keeps 10 images) | `aegc/<env>` | `image-repo` |
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

## On your machine: aegc endpoint

| Resource | Name |
|---|---|
| Docker image | `aegc-endpoint` |
| Docker volume (Globus login, endpoint config) | `aegc-endpoint-home` |
| Docker container | `aegc-endpoint-<name>` (removed by `aegc endpoint stop`) |

## Manifest

`/aegc/<d>/manifest` is JSON written at the end of each `aegc deploy`. It holds the deployment,
region, account, bucket, instance profile ARN, security group and subnets, and for each pool its
settings (env, Python, image digest, sizes, slots, idle timeout, instance types) and the URLs and
names of its queues, launch template and group. `AWSEngine` and `aegc status` read it.
