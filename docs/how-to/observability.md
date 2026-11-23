# Watch tasks: logs, metrics and provenance

Workers report three things about the tasks they run:

- **JSON logs** in the deployment's log group, `/aegc/<d>`: one object per line, with a
  `task finished` line for every task.
- **CloudWatch metrics** in the namespace `aegc`: task counts by outcome, execution and queue-wait
  times, boot-to-first-claim, interruptions and releases, per pool. `aegc metrics` sums them up.
- **Provenance:** where and how each task ran (image, instance, Availability Zone, Spot or
  on-demand, timings and an estimated cost), in the task's result as `details["aegc"]` and in its
  log line. `aegc provenance` finds it by task id.

All three need workers from images built with aegc 0.4 or later. Workers from older images log
plain text and emit no metrics or provenance; rebuild their images with `aegc image build` and
redeploy (see [Upgrade from v0.3](operations.md#upgrade-from-v03)).

## Worker logs

Each worker logs to the stream `<pool>/<instance-id>` in `/aegc/<d>`, kept for 14 days:

```sh
aws logs tail /aegc/e2e --follow
```

Every line from `aegc-worker run` is one JSON object (written with the standard library only):

| Field | On | Meaning |
|---|---|---|
| `ts`, `level`, `logger`, `msg` | every line | UTC time (ISO 8601, milliseconds), level, logger name and the message |
| `deployment`, `pool`, `instance_id` | every line | Where the worker runs |
| `type`, `az`, `lifecycle` | every line, once known | Instance type, Availability Zone, `spot` or `on-demand`, from instance metadata |
| `task_uuid`, `attempt`, `etid`, `receive_count` | task lines | The Globus Compute task id, the engine's attempt number, the attempt's id (`<task_uuid>:<attempt>`) and how often SQS has delivered it |
| `event` | `task finished` lines | `task_finished` |
| `outcome` | `task finished` lines | `ok`, `task_error` (the function raised), `walltime`, `crashed` or `released` (handed back to the queue for another worker) |
| `posted` | `task finished` lines | Whether the worker posted the result; `false` for a released attempt |
| `received_at`, `started_at`, `finished_at`, `exec_s`, `queue_wait_s` | `task finished` lines | Phase times: epoch seconds, then the run time and the wait from submission to claim (by the engine's clock) |
| `provenance` | `task finished` lines | The same record as the result's [`details["aegc"]`](#provenance) |
| `exc` | errors | The traceback |

A `task finished` line looks like this (one line in the log, shown wrapped):

```json
{"ts": "2026-11-16T21:12:09.481Z", "level": "INFO", "logger": "aegc.worker",
 "msg": "task finished: 5f0c8e4a-3b8e-4d0e-9a51-6f1f0b7d2c11:1 ok in 12.4 s",
 "deployment": "e2e", "pool": "geospatial-py314", "instance_id": "i-0abc123def4567890",
 "type": "m8g.large", "az": "us-west-2b", "lifecycle": "spot",
 "event": "task_finished", "task_uuid": "5f0c8e4a-3b8e-4d0e-9a51-6f1f0b7d2c11", "attempt": 1,
 "etid": "5f0c8e4a-3b8e-4d0e-9a51-6f1f0b7d2c11:1", "receive_count": 1, "outcome": "ok",
 "posted": true, "received_at": 1794863516.912, "started_at": 1794863516.951,
 "finished_at": 1794863529.351, "exec_s": 12.4, "queue_wait_s": 3.1,
 "provenance": {"image": "...", "instance_type": "m8g.large", "...": "..."}}
```

The same stream also carries:

- **Your tasks' output.** Whatever a task prints, on stdout or stderr, goes to the worker's stderr
  as plain lines (so a partial line from a task can't split a metric line on stdout). It costs
  CloudWatch Logs' ingestion price like everything else in the stream; see
  [Cost](../concepts/cost.md#what-you-pay-for).
- **The metric documents** described [below](#metrics), as JSON objects with an `_aws` key.

CloudWatch Logs Insights reads the JSON fields directly. In the console, or with
`aws logs start-query` and credentials that allow it (aegc's policies don't), for example:

```
fields @timestamp, task_uuid, outcome, exec_s, queue_wait_s, type, lifecycle
| filter event = "task_finished"
| sort @timestamp desc
| limit 50
```

```
filter event = "task_finished"
| stats count(*) as tasks, avg(exec_s) as avg_exec_s, max(queue_wait_s) as max_wait_s by pool, outcome
```

`aegc-worker selftest` and `aegc-worker version`, which `aegc image build` and `register` run,
keep plain-text logs.

## Metrics

Workers write their metrics as CloudWatch Embedded Metric Format (EMF) lines on stdout, and the
worker container's log driver (`awslogs-format=json/emf`) has CloudWatch Logs turn them into
metrics. So workers need no `cloudwatch:PutMetricData`.

| Metric | Unit | When |
|---|---|---|
| `TasksCompleted` | Count | The task ran and returned a result (`Outcome` `ok`, or `task_error` if it raised) |
| `TasksFailed` | Count | aegc failed it (`Outcome` `walltime`, `crashed`, `poison`, `payload_missing` or `refused`) |
| `ExecSeconds` | Seconds | Each finished task's run time |
| `QueueWaitSeconds` | Seconds | Each claimed task's wait from submission (the engine's clock) to claim |
| `BootToFirstClaimSeconds` | Seconds | Once per worker: from the instance's boot to its first claim |
| `Interruptions` | Count | Spot interruption notices |
| `Releases` | Count | In-flight tasks handed back to the queue (a stop, an interruption, an exit) |
| `PoisonTasks` | Count | Tasks failed without running because they were delivered too often |
| `WorkerExit` | Count | Workers exiting |

`refused` counts tasks a worker failed because the deployment was stopped, capped or expired.

**Dimensions** are `[Deployment, Pool]` and `[Deployment]`, and on `TasksCompleted` and
`TasksFailed` also `[Deployment, Pool, Outcome]`. Task and instance ids never become dimensions
(each distinct value would be a metric of its own); they're in the log lines.

**Auto Scaling group metrics.** `aegc deploy` turns on each group's 1-minute group metrics
(`GroupInServiceCapacity`, `GroupDesiredCapacity` and the others, namespace `AWS/AutoScaling`),
which are free. This needs `autoscaling:EnableMetricsCollection` in the
[deployer policy](../reference/iam.md#deployer); with an older one, `deploy` reports the metrics
as `kept` (not enabled) and carries on.

### aegc metrics

```sh
aegc metrics e2e              # the last hour
aegc metrics e2e --since 1d   # the last day; also 90m, 7d, today or month
aegc metrics e2e --json       # the same, as JSON
```

```
metrics for e2e (us-west-2), 2026-11-16 20:00 UTC to 2026-11-16 21:00 UTC (CloudWatch namespace aegc; a few minutes behind)
  pool geospatial-py314: 118 task(s) completed (116 ok, 2 task_error), 2 failed (1 walltime, 1 crashed)
    exec avg 41.3 s, max 612.0 s | queue wait avg 6.4 s, max 44.1 s | boot to first claim avg 37.8 s
    interruptions 1, releases 2, poison 0, worker exits 3 | vCPUs in service max 8, desired max 8
  pool geospatial-py314-mem: 0 task(s) completed, 0 failed
    interruptions 0, releases 0, poison 0, worker exits 0
  total: 118 completed, 2 failed
```

It reads every pool in the manifest with GetMetricData, at 1-minute periods for windows up to 3
hours, 5 minutes up to a day, and an hour beyond (CloudWatch keeps 1-minute data for 15 days). The
capacity figures are the group's, in vCPUs. With nothing in the window it says so, and suggests
the workers' images may predate 0.4. Metrics arrive a few minutes after the fact.
`cloudwatch:GetMetricData` is in both the deployer and the operator policy.

### Alarms

aegc creates no alarms on these metrics (only a [cloud endpoint](cloud-endpoint.md#the-offline-alarm)
gets one, on its own liveness metric), but they work with ordinary CloudWatch alarms. For example, to be
told about any failed task in `e2e`:

```sh
aws cloudwatch put-metric-alarm --alarm-name aegc-e2e-failed-tasks \
  --namespace aegc --metric-name TasksFailed --dimensions Name=Deployment,Value=e2e \
  --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 0 --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-west-2:123456789012:my-alerts
```

Delete it yourself when you destroy the deployment; `aegc destroy` doesn't know about it.

## Provenance

Each result's `details` keeps what Globus Compute's `execute_task` puts there (the worker's OS,
Python and SDK versions, the endpoint id) and gains `details["aegc"]`:

| Key | Meaning |
|---|---|
| `image` | The worker image, `<registry>/aegc/<env>@sha256:...` |
| `instance_id`, `instance_type`, `availability_zone`, `lifecycle` | The instance, and `spot` or `on-demand` |
| `vcpus`, `slots` | The instance's vCPUs and the worker's concurrent tasks |
| `deployment`, `pool`, `aegc_version` | Where it ran, and the worker's aegc |
| `attempt`, `receive_count` | The engine's attempt number, and how often SQS delivered this attempt |
| `timings` | `received_at`, `started_at`, `finished_at` (epoch seconds), `exec_s`, `queue_wait_s` |
| `est_cost_usd`, `est_cost_source` | This task's share of its instance: the instance's $/h (with its volume and IPv4 address) ÷ slots × `exec_s`. Workers have no pricing access, so it uses aegc's static on-demand table (`static`): for Spot tasks it's an upper bound. [`aegc cost`](../concepts/cost.md#cost-ledger) is the better figure for totals |

### From the SDK

Globus Compute passes `details` through to the SDK's task record:

```python
from globus_compute_sdk import Client, Executor


def tile_stats(tile):
    return len(tile)


with Executor(
    endpoint_id=ENDPOINT_ID, amqp_port=5671, user_endpoint_config={"env": "geospatial"}
) as ex:
    fut = ex.submit(tile_stats, "10SEG")
    fut.result()

info = Client().get_task(fut.task_id)["details"]["aegc"]
print(info["instance_type"], info["lifecycle"], info["timings"]["exec_s"], info["est_cost_usd"])
```

[Globus Flows](flows.md) doesn't include `details["aegc"]` in the Compute action's output; use
`aegc provenance` with the task id from the action's `details.results`.

### aegc provenance

```sh
aegc provenance e2e 5f0c8e4a-3b8e-4d0e-9a51-6f1f0b7d2c11
```

```
task 5f0c8e4a-3b8e-4d0e-9a51-6f1f0b7d2c11 in e2e: 1 attempt(s) finished
  attempt 1 (delivery 1): ok in 12.4 s, queued 3.1 s
    m8g.large spot us-west-2b i-0abc123def4567890 | pool geospatial-py314, 2 slot(s), 2 vCPUs
    image 123456789012.dkr.ecr.us-west-2.amazonaws.com/aegc/geospatial@sha256:4f6e...
    estimated cost $0.000169 (static)
    finished 2026-11-16T21:12:09.481Z in log stream geospatial-py314/i-0abc123def4567890
```

It searches `/aegc/<d>` for the task's `task_finished` lines (FilterLogEvents), one per attempt
that finished. A released attempt has one too, marked `(released: not reported)`, so a task that
was interrupted and ran again shows both. `--since` limits the search (default `14d`, the log
retention), and `--json` prints the records, each with its `log_stream`. With no line found it
exits 1:

```
aegc: no task_finished line for <uuid> in /aegc/e2e since 14d (logs are kept 14 days; workers from images built before aegc 0.4 don't write them)
```

It needs `logs:FilterLogEvents` on the deployment's log group, which the
[operator policy](../reference/iam.md#operator) has and the deployer policy doesn't.

## Status

[`aegc status`](operations.md#check-status) shows what's happening now rather than what happened:
queues, workers, spend, the deployment's grants and each pool's size class.
