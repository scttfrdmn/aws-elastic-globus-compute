# Limits

Values for v0.7. Most are constants in the source; the SQS, EC2, Lambda and Globus Compute
limits are the services'.

## Tasks

| Limit | Value | Notes |
|---|---|---|
| Walltime, default | 39,600 s (11 h), or the pool's `walltime=` | `user_endpoint_config={"walltime_s": N}` |
| Walltime, maximum | 41,400 s (11.5 h), or the pool's lower `max_walltime=` | SQS can't extend a claim past 12 hours |
| Walltime enforcement | Globus Compute's own timeout at `walltime_s`; `SIGTERM` to the task's process group 5 s later; `SIGKILL` to the task process 10 s after that | The task fails with `AEGC_WALLTIME_EXCEEDED` |
| Inline payload | up to 700,000 bytes | Larger task and result bodies go through S3 |
| SQS message size | 1 MiB (1,048,576 bytes) | SQS limit; base64 adds a third, hence the inline limit |
| Globus Compute payload | about 10 MB | Globus Compute's limit on tasks and results, not aegc's; return larger results with [`aegc.outputs`](../how-to/data.md#return-large-outputs) |
| Python | submitter's minor version (or `python` in `user_endpoint_config`) must equal an image's | aarchsci images: 3.14; an env can have an image per minor. Without either, an env with several minors refuses the task |
| `user_endpoint_config` keys | `env` (`^[a-z][a-z0-9-]{0,40}$`), `python` (`^3\.[0-9]{1,2}$`), `pool` (`^[a-z][a-z0-9-]{0,39}$`), `size`, `cpus`, `memory_gib`, `walltime_s` | Enforced by the endpoint's schema; one user endpoint process per distinct config |
| Result from a task process | at most 32 MiB over the pipe to the agent | More fails the task as `CHILD_CRASHED`; Globus Compute refuses results over about 10 MB anyway |
| Task-role credentials | 1-hour sessions, renewed by the agent with 15 min left | Isolated pools; tasks refresh them by themselves |
| `cpus`, `memory_gib` | numbers > 0 | The task goes to the cheapest pool that gives at least that much |
| `resource_specification` | `num_nodes: 1` at most; no MPI ranks or `launcher_options` | The Globus Compute web service requires `num_nodes` and drops every non-MPI key, so aegc options can't go here |

## Delivery

| Limit | Value |
|---|---|
| Task claim (visibility timeout) | 300 s |
| Claim heartbeat | every 100 s |
| Deliveries before a `WORKER_LOST_REPEATEDLY` failure | 4 (the 5th delivery fails the attempt) |
| Dead-letter queue `maxReceiveCount` | 6 |
| Resubmissions after a system failure | 1 (`max_retries_on_system_failure`) |
| Completed tasks remembered per endpoint process | 10,000 |
| Task and dead-letter queue retention | 14 days |
| Result queue retention | 14 days |
| Result queue visibility timeout | 120 s |
| Payload object expiry | 15 days (S3 lifecycle) |

## Scaling and workers

| Limit | Value |
|---|---|
| Scaler interval | 10 s, and on each submission |
| Result long poll | 20 s |
| Worker idle drain | 300 s by default (`idle=`) |
| Worker lifetime | 86,400 s (24 h) by default, 3,600–259,200 s (`lifetime=`); then it stops claiming and leaves once idle, or is replaced at the group's minimum |
| Stall breaker | 15 min with capacity wanted and none in service, then a 10 min pause |
| `AEGC_NO_CAPACITY` | after 3,600 s with capacity wanted and none in service (`stall_fail_s`) |
| Watchdog | every 2 min; acts if the worker is down or its heartbeat file is over 600 s old, once the instance is 900 s old, or when the instance is older than lifetime + max walltime + 900 s (35.75 h by default) |
| `aegc destroy` wait per Auto Scaling group | 600 s |
| User data size | 16 KiB (EC2 limit) |
| Default pool | `max_vcpus` 8, `min_vcpus` 0, 2–8 vCPU per worker, at least 4096 MiB, Spot, one slot per vCPU |
| Size classes | `small` 1 vCPU, 2 GiB; `medium` 2 vCPUs, 8 GiB; `mem` 2 vCPUs, 16 GiB; `large` 8 vCPUs, 32 GiB; `whole` one task per instance, one worker size ([table](configuration.md#size-classes)); need images built with aegc ≥ 0.3 |
| Slots with a size class | min(vCPUs ÷ task vCPUs, memory ÷ (task GiB × 0.9)), at least 1 |
| Largest worker | at most `max_vcpus` vCPUs (the top of `vcpu=`) |
| Pool capacity unit | vCPUs (`DesiredCapacityType=vcpu`); v0.1 pools count workers until redeployed |
| Root volume | 30–16,384 GiB gp3 (default 30) at 125–1,000 MiB/s (default 125); 3,000 IOPS, or 4 × the throughput above 750 MiB/s |
| Instances | Graviton (arm64) only; no GPUs |
| Spot capacity | your account's Spot vCPU quota for standard instances (`L-34B43A08`; AWS's default is 5), checked by `aegc deploy` and `aegc status` against the sum of the pools' `max_vcpus` |

## Lambda pools

| Limit | Value |
|---|---|
| Function memory (`memory=`) | 1,024–10,240 MiB, default 2,048; aegc's minimum is 1 GiB (Lambda's is 128 MB) |
| vCPUs per task | memory ÷ 1,769 MB, at most 6 (about 1.16 at 2,048 MB); one task per execution environment |
| Function timeout (`timeout=`) | 60–1,800 s, default 900 |
| Mapping concurrency (`concurrency=`) | 2–1,000, default 10 (Lambda's `MaximumConcurrency` range) |
| `/tmp` (`tmp=`) | 512–10,240 MiB, default 512 |
| Walltime | default and maximum: the timeout less 20 s (1,780 s at most); at run time, at most the invocation's remaining time less 20 s |
| Task queue visibility | 6 × the timeout, at most 43,200 s; a started record's: its invocation's remaining time + 60 s |
| Batch size | 1 record per invocation, with partial batch responses |
| Deployment state, per execution environment | read at most every 60 s |
| Image size | refused by `deploy` above 10 GiB compressed (as ECR reports it); Lambda's limit is 10 GB uncompressed |
| Function environment | 4 KB for `AEGC_WORKER_CONFIG_B64` and `AEGC_IMAGE` (Lambda's limit), which bounds the grants a deployment with Lambda pools can have |
| `deploy` wait for a function to settle | up to 900 s |
| Mapping follows the state | every engine scaler pass (10 s) and every cloud endpoint `aegc evaluate` (5 min) |
| Spend from Lambda's metrics | hourly sums (windows rounded out to whole hours), re-read every 300 s while limits are set |
| Price | static, arm64: $0.0000133334 per GB-second, $0.20 per million requests (us-west-2, 2026-11-23) |
| Account concurrency | not checked by aegc; Lambda's default is 1,000 per region, and new accounts can have less |
| Isolation | none: tasks run with the function role (`PoolSpec` refuses `isolate_tasks` for Lambda pools) |

## Spend, state and lease

| Limit | Value |
|---|---|
| Limit periods | UTC day (`--max-usd-day`, `--max-instance-hours-day`), UTC month (`--max-usd-month`) |
| Admission hold | 1.2 × the expected rate over the next hour, at any time of day or month |
| Waiting with no worker admitted | 600 s, then `AEGC_BUDGET_EXCEEDED` |
| Engine re-reads the state | every 30 s |
| Engine re-reads the manifest | every 60 s (redeploys and new limits) |
| Engine re-reads the cost ledger | every 300 s, while limits are set |
| Worker reads the state | every 60 s |
| Worker writes its ledger record | at start, every 300 s, and at exit |
| A worker counts as still running | if its record was written in the last 1,200 s |
| Ledger lookback | 4 days before the period starts (records are dated by instance boot) |
| Failed price lookups | retried at most hourly; the static table is used meanwhile |
| Spot price cache | 1 h (latest `DescribeSpotPriceHistory` price per type and AZ) |
| On-demand price cache | 24 h (Price List API) |
| `aegc stop --hard` grace | 120 s before remaining instances are terminated |
| Lease | 48 h by default, at least 3 h (`--lease`); renewed every min(1 h, lease / 3) by each running engine while running or capped, and by a cloud endpoint's `aegc evaluate` while Globus reports it online |
| Cloud endpoint's `aegc evaluate` | 3 min after boot, then every 5 min; each pass times out after 240 s |
| Cloud endpoint offline alarm | `EndpointOnline` below 1, or missing, for 3 periods of 5 min; fires about 20 to 25 min after the endpoint goes offline |
| TTL | none by default (`--ttl`); never renewed; not scheduled once passed |
| Lease and TTL schedule retries | 10 (EventBridge Scheduler `MaximumRetryAttempts`) |
| Ledger record expiry | 400 days (S3 lifecycle) |

## Names

| Limit | Value |
|---|---|
| Deployment name | 1–20 characters of `[a-z0-9-]`; starts with a letter; no trailing `-`, no `--` |
| Pool name | the same rules, up to 40 characters; derived as `<env>-py<NNN>`, `<env>-py<NNN>-<size>`, or `<env>-py<NNN>-lambda` |
| Env name usable in a deployment | at most 34 characters, so the pool name fits in 40; with `size=`, 27 for `medium`, 30 for `mem`, 28 for the others; 27 for a Lambda pool |
| Lambda function and function role names | `aegc-<d>-<pool>` and `aegc-<d>-<pool>-<region>`, at most 64 characters each |
| Pools per env per deployment | one per recorded Python minor and size class |
| Recorded images per env | one per Python minor |
| Images kept per ECR repository | the 10 most recent, plus any with an `inuse-` tag; untagged images expire after a day; shared by all of an env's minors |
| Worker image sources | aarchsci environments, aarchbio tools, any pinned `linux/arm64` image with Python ≥ 3.11 (`--base`), or an image you built with aegc ≥ 0.3 in it, running as root or uid 57439 (`image register`) |
| Task isolation, JSON logs, metrics, provenance | images with aegc ≥ 0.4 |
| Lambda pools | images with aegc ≥ 0.7 |

## Grants and outputs

| Limit | Value |
|---|---|
| Grants per deployment | at most 32, and no more than fit in the worker and task roles' inline policies (10,240 characters in all on each, about 2,110 used by `aegc-worker`, 180 by `aegc-task`) |
| Grant prefix | an S3 key prefix, at most 1,024 bytes; no `*`, `?` or `${` |
| Grant buckets | general purpose bucket names; not aegc's own `aegc-<account>-<region>`; write grants only in the deployment's account |
| Read grant | `s3:GetObject`, `s3:GetObjectVersion`, and `s3:ListBucket` for keys under the prefix |
| Write grant | `s3:PutObject` and multipart uploads; no reads or deletes |
| `aegc.outputs` keys | `<grant prefix>/<task uuid>/<name>` |
| Grants seen by `aegc.outputs` | the deployment's manifest, re-read at most every 60 s |
| Presigned URL from a task | valid until `expires_s`, or until the credentials that signed it expire, whichever is first: at most an hour with the task role (a few hours with the instance role, on workers from older images) |

## Housekeeping

| Limit | Value |
|---|---|
| `aegc reap` | deletes result queues that are empty and unmodified for 14 days; a cloud endpoint's `aegc evaluate` does the same every 5 min |
| Result queue keep-alive | a running engine refreshes its queue daily |
| Worker log retention | 14 days |
| `aegc provenance` search | 14 days back by default (`--since`) |
| `aegc metrics` period | 1 min for windows up to 3 h, 5 min up to a day, 1 h beyond |
| Metric dimensions | `[Deployment, Pool]`, `[Deployment]`, and `[Deployment, Pool, Outcome]` on task counts |
| `aegc destroy` verification wait | up to 600 s for instances, volumes and network interfaces to go |
