# Limits

Values in aegc 0.1.0. Most are constants in the source; the SQS and Globus Compute limits are the
services'.

## Tasks

| Limit | Value | Notes |
|---|---|---|
| Walltime, default | 39,600 s (11 h) | `resource_specification={"walltime_s": N}` |
| Walltime, maximum | 41,400 s (11.5 h) | SQS can't extend a claim past 12 hours |
| Walltime enforcement | Globus Compute's own timeout at `walltime_s`; `SIGTERM` to the task's process group 5 s later; `SIGKILL` to the task process 10 s after that | The task fails with `AEGC_WALLTIME_EXCEEDED` |
| Inline payload | up to 700,000 bytes | Larger task and result bodies go through S3 |
| SQS message size | 1 MiB (1,048,576 bytes) | SQS limit; base64 adds a third, hence the inline limit |
| Globus Compute payload | about 10 MB | Globus Compute's limit on tasks and results, not aegc's |
| Python | submitter's minor version must equal the image's | aarchsci images: 3.14 |
| `resource_specification` keys | `env`, `pool`, `walltime_s` | Others fail the task |
| `user_endpoint_config` keys | `env` (`^[a-z][a-z0-9-]{0,40}$`) | Enforced by the endpoint's schema |

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
| Stall breaker | 15 min with capacity wanted and none in service, then a 10 min pause |
| Watchdog | every 2 min; acts if the worker is down or its heartbeat file is over 600 s old, once the instance is 900 s old |
| `aegc destroy` wait per Auto Scaling group | 600 s |
| User data size | 16 KiB (EC2 limit) |
| Default pool | `max` 4, `min` 0, 2–16 vCPU, at least 4096 MiB, Spot, one slot per vCPU |
| Instances | Graviton (arm64) only; no GPUs |
| Spot capacity | your account's Spot vCPU quota for standard instances (`L-34B43A08`; AWS's default is 5), checked by `aegc deploy` and `aegc status` |

## Names

| Limit | Value |
|---|---|
| Deployment name | 1–20 characters of `[a-z0-9-]`; starts with a letter; no trailing `-`, no `--` |
| Pool name | the same rules, up to 40 characters; derived as `<env>-py<NNN>` |
| Env name usable in a deployment | at most 34 characters, so the pool name fits in 40 |
| Pools per env per deployment | 1 |
| Recorded images per env | 1 (one Python version) |
| Images kept per ECR repository | the 10 most recent; untagged images expire after a day |

## Housekeeping

| Limit | Value |
|---|---|
| `aegc reap` | deletes result queues that are empty and unmodified for 14 days |
| Result queue keep-alive | a running engine refreshes its queue daily |
| Worker log retention | 14 days |
