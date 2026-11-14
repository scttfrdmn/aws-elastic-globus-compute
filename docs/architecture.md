# Architecture

```
laptop: endpoint container (globus-compute-endpoint + aegc)
  user endpoint process → AWSEngine ── SendMessage ──▶ SQS aegc-<d>-<pool>-tasks ◀── long poll ── workers
                            │  └─ SetDesiredCapacity ─▶ Auto Scaling group aegc-<d>-<pool>
                            └── long poll ◀── SQS aegc-<d>-res-<uep> ◀── started / result / system_failure
  payloads over ~700 KB → S3 aegc-<account>-<region>/payloads/<d>/…
workers: stock ECS-optimized Amazon Linux 2023 arm64 → docker run aegc/<env>@digest aegc-worker run
```

## Task path

1. The endpoint receives a task from Globus and calls `AWSEngine.submit()`. The engine sends the task
   to the pool's SQS queue before it returns, because Globus acknowledges the task as soon as
   `submit()` returns.
2. The engine raises the Auto Scaling group's desired capacity to match demand. It never scales in.
3. A worker claims the message and renews its visibility while the task runs. It runs Globus Compute's
   own `execute_task` inside the science image, then posts the packed result to the endpoint process's
   result queue and deletes the task message.
4. The engine resolves the task's future. If the endpoint process restarted in the meantime, the
   engine writes the result to `unacked_results/`, and the endpoint republishes it.
5. A worker that has been idle for 5 minutes terminates itself and decrements the desired capacity.
   With no work, capacity is zero.

## Delivery guarantees

Delivery is at least once:
- A lost worker's task becomes visible again after the 300 s visibility timeout and is retried.
- The first result for each task wins.
- A task delivered more than 4 times fails with a system error instead of being retried forever.

## Resources

All resources are created with boto3 and tagged `aegc:managed=true` plus `aegc:deployment=<d>`.
`aegc destroy` removes only resources that carry those tags. The resource inventory is documented as
it's implemented.
