# Cost

With no tasks, an aegc deployment has no running instances. You pay for workers while they run,
plus small amounts for storage and API requests. There is no NAT gateway, load balancer or
always-on instance.

!!! note "Example prices"
    The figures below were read from the AWS Price List and Spot price history APIs for **us-west-2
    on 2026-11-14**. Prices change, Spot prices change hourly and differ by Availability Zone, and
    other regions differ. Treat them as examples and check current prices for your own estimates.

## What you pay for

| Item | When | Example price (us-west-2, 2026-11-14) |
|---|---|---|
| Worker instances | From launch until the worker terminates itself, per second | Spot; see the table below |
| Worker root volume (50 GiB gp3, 3,000 IOPS, 500 MiB/s) | While the instance exists | $0.08 per GB-month storage plus $0.04 per MiB/s-month above the included 125 MiB/s: about **$0.026 per worker-hour** |
| Public IPv4 address per worker | While the instance exists | $0.005 per hour |
| SQS requests | Every send, receive, delete, visibility change and attribute read; each 64 KB of a message counts as one request | $0.40 per million |
| S3 payloads (bodies over 700 KB) | Until deleted after use, or expired after 15 days | $0.023 per GB-month, plus request charges |
| ECR storage | Worker images in `aegc/<env>` (the 10 most recent builds are kept) and the cached bases in `aegc-quay/…` | $0.10 per GB-month |
| CloudWatch Logs | Worker logs, kept 14 days | $0.50 per GB ingested |
| Data transfer | Task and result bytes between your machine and AWS | Internet data transfer out applies to results; traffic between workers and S3, SQS and ECR in the region is free |

SSM parameters are standard-tier. Auto Scaling has no charge of its own.

## Instance prices

Default pools accept Graviton `c7g`, `m7g`, `c8g`, `m8g`, `r8g`, `c9g`, `m9g` and `r9g` types with
2 to 16 vCPUs and at least 4 GiB, and let `price-capacity-optimized` choose. That strategy weighs
available capacity as well as price, so it doesn't always pick the cheapest type.

| Type | vCPU | Memory | On-demand $/h | Spot $/h across us-west-2 AZs, 2026-11-14 |
|---|---|---|---|---|
| c7g.large | 2 | 4 GiB | 0.0725 | 0.028–0.051 |
| m7g.large | 2 | 8 GiB | 0.0816 | 0.025–0.044 |
| c8g.large | 2 | 4 GiB | 0.0798 | 0.030–0.040 |
| m8g.large | 2 | 8 GiB | 0.0898 | 0.026–0.049 |
| c8g.xlarge | 4 | 8 GiB | 0.1595 | 0.064–0.107 |

On a `.large` Spot worker, the root volume's provisioned throughput (about $0.021 of the $0.026 per
hour) is a large share of the total. It's there to make image pulls fast.

## A worked example

Three `c8g.large` Spot workers run a 20-minute burst. Each lives for about 26 minutes: roughly 30 s
to boot, 20 minutes of work, and the 5-minute idle period before it leaves the group.

- Worker-hours: 3 × 0.43 h ≈ 1.3 h.
- Per worker-hour: about $0.035 (Spot) + $0.026 (volume) + $0.005 (IPv4) ≈ $0.066.
- Total: about **$0.09**, plus a few cents of SQS requests.

The same run on demand would be about 1.3 × ($0.080 + $0.031) ≈ $0.14.

## While nothing runs

- **No instances.** Pools return to zero about 5.5 minutes after the last task (300 s idle plus
  shutdown), unless you set `min=` on a pool.
- **A running endpoint polls SQS.** Each user endpoint process long-polls its result queue every
  20 s and reads each pool's queue depth every 10 s: about 13,000 requests a day, or 390,000 a month
  (about $0.16) for one process and one pool.
- **Storage.** ECR images (science images are often several GB, stored in both `aegc/<env>` and the
  pull-through cache) and anything left in the payload bucket.

## Keeping costs predictable

- Set `max=` on every pool. The scaler never asks for more than that, and Auto Scaling enforces it.
- Keep the default `idle=300`. A longer idle period keeps workers, and their bills, around longer.
- Don't set `min=` unless you need warm workers; `min` workers run all the time.
- Batch tiny tasks: per-task overhead costs worker time (see
  [When to use aegc](when-to-use.md#limits-and-weak-spots)).
- aegc doesn't create AWS Budgets or cost alarms. Set a budget on the account yourself.
- `aegc destroy <d> --yes` removes a deployment completely. Images and the region bootstrap stay
  until you remove them.
