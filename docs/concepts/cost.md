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
| Worker root volume (30 GiB gp3, 3,000 IOPS, 125 MiB/s by default) | While the instance exists | $0.08 per GB-month storage; 3,000 IOPS and 125 MiB/s are included, and each MiB/s above that is $0.04 per month: about **$0.0033 per worker-hour** (v0.1's 50 GiB at 500 MiB/s was $0.026) |
| Public IPv4 address per worker | While the instance exists | $0.005 per hour |
| SQS requests | Every send, receive, delete, visibility change and attribute read; each 64 KB of a message counts as one request | $0.40 per million |
| S3 payloads (bodies over 700 KB) | Until deleted after use, or expired after 15 days | $0.023 per GB-month, plus request charges |
| ECR storage | Worker images in `aegc/<env>` (the 10 most recent builds, plus any older image a deployment still uses) and the cached bases in `aegc-quay/…` | $0.10 per GB-month |
| CloudWatch Logs | Worker logs, kept 14 days. **Your tasks' stdout and stderr go here too**, so a chatty task costs more than aegc's own few lines per task | $0.50 per GB ingested |
| CloudWatch metrics | The workers' [metrics](../how-to/observability.md#metrics) in namespace `aegc`: up to 16 per pool and 9 per deployment, each billed only for the hours it gets data. Auto Scaling group metrics are free. `aegc metrics` reads 23 metrics per pool | $0.30 per metric-month, prorated by the hour; GetMetricData $0.01 per 1,000 metrics read |
| Data transfer | Task and result bytes between your machine and AWS | Internet data transfer out applies to results; traffic between workers and S3, SQS and ECR in the region is free |

The cost ledger adds one small S3 object per worker, rewritten every 5 minutes (about $0.00006 per
worker-hour in PUT requests). SSM parameters are standard-tier. Auto Scaling, the Price List API,
Spot price history, and the lease's EventBridge Scheduler schedules (which fire at most once each)
cost nothing in practice. `aegc cost --actual` makes one Cost Explorer request, which costs $0.01.

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

The root volume defaults to 30 GiB at 125 MiB/s, the gp3 baseline, which adds no throughput
charge. v0.1 provisioned 500 MiB/s to make image pulls fast; on a `.large` Spot worker that cost
about $0.021 an hour, a large share of the total. A pool can still ask for more with
`disk=GiB,throughput=MiBps` (125–1000; each MiB/s above 125 adds about $0.000055 per worker-hour).
How much 125 MiB/s slows the first image pull will be measured in the live validation.

## A worked example

Three `c8g.large` Spot workers (a pool with `max_vcpus=6`) run a 20-minute burst. Each lives for
about 26 minutes: roughly 30 s to boot, 20 minutes of work, and the 5-minute idle period before it
leaves the group.

- Worker-hours: 3 × 0.43 h ≈ 1.3 h.
- Per worker-hour: about $0.035 (Spot) + $0.0033 (volume) + $0.005 (IPv4) ≈ $0.043.
- Total: about **$0.06**, plus a few cents of SQS requests.

The same run on demand would be about 1.3 × ($0.080 + $0.008) ≈ $0.11.

## While nothing runs

- **No instances.** Pools return to zero about 5.5 minutes after the last task (300 s idle plus
  shutdown), unless you set `min_vcpus=` on a pool.
- **A running endpoint polls SQS.** Each user endpoint process long-polls its result queue every
  20 s and reads each pool's queue depth every 10 s: about 13,000 requests a day, or 390,000 a month
  (about $0.16) for one process and one pool. It also reads the deployment state and manifest from
  SSM (no charge for standard parameters) and, with spend limits set, the cost ledger every
  5 minutes.
- **Storage.** ECR images (science images are often several GB, stored in both `aegc/<env>` and the
  pull-through cache), anything left in the payload bucket, and the ledger (a few KB per worker).

## Spend limits

A deployment can have three limits, set with `aegc deploy` or changed later with `aegc limits`:

```sh
aegc deploy e2e --pool geospatial --max-usd-day 5 --max-usd-month 50
aegc limits e2e --max-instance-hours-day 24 --on-cap kill
aegc limits e2e --max-usd-month none        # remove a limit
aegc limits e2e                             # show them
```

| Limit | Counts |
|---|---|
| `--max-usd-day N` | Estimated dollars since 00:00 UTC |
| `--max-usd-month N` | Estimated dollars since the 1st of the month, 00:00 UTC |
| `--max-instance-hours-day N` | Instance-hours since 00:00 UTC, whatever the instance size |

The limits live in the deployment's manifest and are kept across redeploys. Running endpoints pick
up a change within a minute. `aegc deploy` prints a note when a deployment has no limit.

Limits are enforced in two steps, both by the engine in each running endpoint process.

**Admission, before scaling out.** Each time the scaler wants more capacity, it works out how much
the deployment may still spend: for each dollar limit, the headroom (the limit minus what's spent
in the period) spread over the next hour, with a 1.2× margin. It subtracts the rate of the workers
already running and admits only the additional workers whose price fits in what's left, in whole
workers. A new worker is priced high on purpose: the most expensive allowed instance family's Spot
(or on-demand) price per vCPU, times the pool's smallest worker, plus its volume and IPv4 address.
The instance-hour limit works the same way with instances instead of dollars. When admission holds
a pool back, the engine logs `spend limits admit N of M more vCPUs`, and tasks wait in the queue.

For example, with `--max-usd-day 5`, $1 spent, and nothing running, the deployment may add workers
costing up to $4 / (1 h × 1.2) ≈ $3.33 an hour. The window is a full hour at any time of day, even
in the last hour of a day or month, so a burst admitted just before midnight doesn't carry a
period's whole headroom into the next one.

If the limits leave no room for even one worker while tasks wait and none is running, the engine
fails its waiting tasks for that pool with `AEGC_BUDGET_EXCEEDED` after 10 minutes, rather than
leave them queued below a cap that may never trip. Raise a limit to run them.

**The cap, once a limit is reached.** When estimated spend in the period reaches a limit, the
engine sets the deployment's state to `capped` and lowers every group's minimum to 0. Then:

- new tasks fail at once with `AEGC_BUDGET_EXCEEDED` and a message saying which limit, until when,
  and how to raise it;
- each engine fails its own queued tasks the same way and removes them from the task queues;
- workers see the state within a minute, stop claiming, and fail any task they still receive;
- with `--on-cap fail` (the default), running tasks finish and then their workers leave; with
  `--on-cap kill`, running tasks fail at once with `AEGC_BUDGET_EXCEEDED` and the workers leave.

None of these failures is retried. A cap lasts until the end of its UTC day or month, when the
deployment runs again by itself and the engine puts back the group minimums it lowered. To end it
sooner, raise the limit and resume:

```sh
aegc limits e2e --max-usd-day 10 --resume
```

`--resume` refuses while spend is still over a limit (`still over a limit (...); raise it`), so
raise the limit in the same command. While capped, running endpoints keep renewing the
[lease](architecture.md#lease-and-ttl), so a deployment capped for the rest of the month doesn't
expire meanwhile.

**What limits don't do.**

- **They're estimates.** The engine counts what the cost ledger records, priced as described
  below. Your AWS bill can differ.
- **They can be overshot.** Workers update the ledger every 5 minutes and the engine re-reads it
  every 5 minutes, so a cap trips up to about 10 minutes after the limit is reached. With
  `--on-cap fail`, running tasks then finish, which can take up to their walltime. Auto Scaling can
  also launch an instance larger than the vCPUs asked for. Use `--on-cap kill` for a firmer limit.
- **They need a running endpoint.** Only engines check spend, and the engine that trips a cap
  writes the state with the [operator policy](../reference/iam.md#operator)'s permissions. If that
  write is denied, the engine still keeps the cap for its own tasks, but workers don't see it. With no endpoint running nothing scales
  out, but workers kept by `min_vcpus` keep running until the lease expires (48 hours by default; see
  [Architecture](architecture.md#lease-and-ttl)).
- **They aren't a security boundary.** In [isolated](security.md#task-isolation) pools tasks can't
  write ledger records, but anyone who can submit can still keep a pool busy up to its
  `max_vcpus`. On workers from images built before aegc 0.4, task code runs with the worker role,
  which can write ledger records, so a task can make the estimate wrong. See
  [Security model](security.md).
- **They don't cover the account.** aegc doesn't create AWS Budgets or billing alarms. Set an AWS
  Budget on the account as well.

## Cost ledger

Each worker writes a record to `s3://aegc-<account>-<region>/ledger/<d>/<yyyy-mm-dd>/<instance-id>.json`
when it starts, every 5 minutes, and when it exits. The record holds the instance type, Availability
Zone, Spot or on-demand, the root volume's size and throughput, when the instance booted and when
the worker was last seen, the tasks it claimed, its busy time and why it exited. The date in the key
is the day the instance booted (UTC). Readers look back 4 days before a period starts, which covers
the longest an instance can live (a 72-hour lifetime plus an 11.5-hour task). `aegc bootstrap` adds a lifecycle rule that deletes records after 400 days.
`aegc destroy` leaves them, so you can still read a deployment's cost after it's gone.

`aegc cost` reads the ledger and prices it:

```sh
aegc cost e2e                      # today (UTC)
aegc cost e2e --since month        # this month
aegc cost e2e --since 7d --by type # the last 7 days, by instance type
aegc cost e2e --actual             # also ask Cost Explorer
```

```
deployment e2e, 2026-11-14 00:00 UTC to 2026-11-14 16:20 UTC: about $0.4120 (9.52 instance-hours; prices: spot)
  geospatial-py314: $0.4120
  estimate from the cost ledger: instance time x Spot or on-demand price, plus root volume and IPv4; SQS, S3, ECR, logs and data transfer aren't included
```

Each record is charged from the instance's boot, so boot and the image pull count, to its last
write. A worker still running (seen in the last 20 minutes, with no exit recorded) is charged up to
now. `aegc cost`, `aegc status` and the
caps use the same pricing function, so what you see is what's enforced.

**Prices.** Spot prices come from `DescribeSpotPriceHistory` (the latest price for the instance type
and Availability Zone, cached for an hour), on-demand prices from the AWS Price List API (cached for
a day). If neither is reachable (a failed lookup is retried at most hourly), aegc uses a small
static table of on-demand prices from November 2026 (us-west-2's, which are also us-east-1's for
these types), and Spot is charged at the on-demand price, its ceiling. The report lists which
sources it used: `spot`, `on-demand` or `static`. Every rate adds the root volume and the public
IPv4 address.

**`--actual`** asks Cost Explorer for the cost of resources tagged `aegc:deployment=<d>`, in whole
UTC days. It works only once that [cost allocation tag](#cost-explorer-and-cost-allocation-tags)
is active, its data lags by up to a day, and each request costs $0.01.

### What the estimates leave out

- The time after a worker's last write (up to 5 minutes for a worker that dies without exiting
  cleanly, and the shutdown after a clean exit), and instances whose worker never started at all.
- Price changes: past usage is priced at the current Spot price, not the one in effect at the time.
- SQS, S3, ECR, CloudWatch Logs, data transfer, and Cost Explorer requests.
- Discounts, credits, Savings Plans and taxes.

## Cost Explorer and cost allocation tags

Cost Explorer can break spend down by aegc's tags, and `aegc cost --actual` uses that, but only
for tags you activate. Tag keys show up in billing a day or so after the first deployment uses
them. Activate them once per account. In an AWS Organization, that's the management (payer)
account; the caller needs `ce:UpdateCostAllocationTagsStatus` (and
`ce:StartCostAllocationTagBackfill` for the backfill).

```sh
# Are the keys known to billing yet, and active?
aws ce list-cost-allocation-tags --region us-east-1 \
  --tag-keys aegc:deployment aegc:pool aegc:role aws:autoscaling:groupName

# Activate them.
aws ce update-cost-allocation-tags-status --region us-east-1 --cost-allocation-tags-status \
  TagKey=aegc:deployment,Status=Active TagKey=aegc:pool,Status=Active \
  TagKey=aegc:role,Status=Active TagKey=aws:autoscaling:groupName,Status=Active

# Optional: apply them to past usage too (up to 12 months back; takes up to a day).
aws ce start-cost-allocation-tag-backfill --region us-east-1 \
  --backfill-from 2026-11-01T00:00:00Z
aws ce list-cost-allocation-tag-backfill-history --region us-east-1
```

Don't activate `aegc:managed` (every aegc resource has it) or `aegc:version` (it changes with
every release). The Cost Explorer API is served from `us-east-1` whatever region aegc runs in. The
[Billing console](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html)
does the same thing. Without a backfill, activation applies only to usage from then on.

Even with a backfill, don't count on Cost Explorer for usage from before activation: in our account
a deployment that ran a few hours before activation never appeared under its tag, although the
backfill succeeded. Deployments after activation matched the ledger closely (v03: $0.0150 billed,
$0.0154 estimated). Cost Explorer also lags by up to a day; `aegc cost` doesn't.

## Keeping costs predictable

- Set a spend limit, `--max-usd-day` or `--max-usd-month`, on every deployment.
- Set `max_vcpus=` on every pool (the default is 8). The scaler never asks for more, and Auto
  Scaling enforces it, so a pool's cost is bounded whichever instance sizes Spot picks.
- Keep the default `idle=300`. A longer idle period keeps workers, and their bills, around longer.
- Don't set `min_vcpus=` unless you need warm workers; those workers run all the time.
- Batch tiny tasks: per-task overhead costs worker time (see
  [When to use aegc](when-to-use.md#limits-and-weak-spots)).
- Check the Spot vCPU quota. A new account's default is **5 vCPUs**, enough for two `.large`
  workers. `aegc deploy` and `aegc status` compare it with what your pools can ask for, warn when
  scale-out would stop short, and print the `aws service-quotas` command to request more. A pool
  whose smallest worker doesn't fit is refused.
- Keep the lease (the default, 48 hours), and add `--ttl` for a deployment that should end on a
  date. See [Architecture](architecture.md#lease-and-ttl).
- Set an AWS Budget on the account; aegc doesn't create one.
- Activate aegc's [cost allocation tags](#cost-explorer-and-cost-allocation-tags) so Cost Explorer
  (and `aegc cost --actual`) can show its spend.

## Stopping spend now

```sh
aegc stop <d>                # queued tasks fail, running tasks finish, workers leave
aegc stop <d> --hard --yes   # running tasks fail too; stragglers are terminated after 2 minutes
```

`aegc stop` sets the deployment's state to `stopped` and every group's minimum to 0. It works
without a running endpoint, because workers read the state themselves. `aegc resume <d>` undoes it.
See [Operations](../how-to/operations.md#stop-and-resume).

`aegc destroy <d> --force --yes` also stops spending, by deleting the deployment: it deletes the
Auto Scaling groups (terminating their instances, including any running tasks) and everything else,
even with tasks queued. Lowering a group's desired capacity by hand isn't enough: workers are
protected from scale-in. Images and the region bootstrap stay until you remove them (see
[Operations](../how-to/operations.md#removing-everything)).
