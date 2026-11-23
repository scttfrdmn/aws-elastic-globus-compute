# Delivery and idempotency

aegc delivers tasks **at least once**. A task can run more than once, and the endpoint keeps the
first result. Write functions so that running them twice is harmless.

## The claim

A worker claims a task with an SQS receive. The message is then invisible to other workers for
300 s (the task queue's visibility timeout). While the task runs, the worker's heartbeat extends the
claim by another 300 s every 100 s. The worker deletes the message (acks it) only after it has
posted the result to the endpoint's result queue. If the worker disappears, the heartbeats stop,
and the message becomes visible again within 300 s for another worker to claim.

Each submission of a task to SQS is one *attempt*, identified as `<task_uuid>:<attempt>`. Each
receive of that message is a *delivery*.

## What happens when something fails

| Event | What aegc does | Can the function run twice? |
|---|---|---|
| The function raises | The exception and traceback are the result. No retry. | No |
| The task exceeds its walltime | The worker kills the task's process group and returns an `AEGC_WALLTIME_EXCEEDED` error. No retry. | No |
| The child process crashes (signal, non-zero exit, out of memory) | The worker posts a `CHILD_CRASHED` system failure. The endpoint retries once. | Yes |
| Spot interruption notice, or Auto Scaling terminating the instance | The worker releases its in-flight tasks at once (visibility 0) and exits. Another worker reruns them from the start. | Yes, and the two runs can overlap until the old instance stops |
| The worker or instance dies without notice | The claim expires within 300 s; another worker reruns the task. | Yes |
| The heartbeat can't reach SQS for 300 s | The claim expires while the task still runs; another worker can start it too. | Yes, concurrently |
| The worker posts the result but can't delete the message | The message is redelivered and the task runs again. The endpoint drops the second result. | Yes |
| The worker can't post the result | It doesn't ack, so the message is redelivered. | Yes |
| A task is delivered more than 4 times | The 5th delivery posts a `WORKER_LOST_REPEATEDLY` system failure instead of running it. The endpoint retries once, as a new attempt. | Up to 4 runs per attempt |
| A large payload has expired from S3 (after 15 days) | `PAYLOAD_MISSING` system failure; the endpoint retries once, uploading the payload again. | — |
| No pool matches (env, Python, walltime, unknown keys) | The task fails at once with an `AEGC_CONFIGURATION` error. No retry. | No |
| The deployment is capped, stopped or expired when the task arrives | The engine fails it at once with `AEGC_BUDGET_EXCEEDED`, `AEGC_STOPPED` or `AEGC_EXPIRED`. No retry. | No |
| The deployment becomes capped, stopped or expired while the task is queued | The engine fails its own queued tasks with the state's code and deletes their messages; a worker that still receives one fails it the same way. No retry. | No |
| The deployment becomes capped, stopped or expired while the task runs | The task finishes and its result is delivered. With the state's `kill` flag (`aegc stop --hard`, `--on-cap kill`) the worker fails it at once with the state's code instead. No retry. | No |
| A pool gets no instance in service for `stall_fail_s` (1 h) while tasks wait | The engine fails its queued tasks for that pool with `AEGC_NO_CAPACITY`, naming the last scaling error and the quota command, and deletes their messages. Tasks already started aren't affected. No retry. | No, except for a message the purge misses (below) |
| The spend limits admit no worker while tasks wait and none is running, for 10 minutes | The engine fails its queued tasks for that pool with `AEGC_BUDGET_EXCEEDED`, naming the limits, and deletes their messages. No retry. | No, except for a message the purge misses (below) |
| A worker reaches its lifetime (24 h by default) | It stops claiming, finishes its running tasks, and leaves the group (at the group's minimum, the group replaces it). Not a failure. | No |
| The endpoint process stops with tasks in flight | Workers keep going and post to the result queue, which keeps messages for 14 days. The next process with the same endpoint id picks them up and writes them to `unacked_results/`, and Globus Compute republishes them. | No |

*Retries once* is the template's `max_retries_on_system_failure: 1`: the endpoint resubmits the task
as a new attempt after a system failure, and reports the failure if the retry fails too. In the
worst case (every delivery of both attempts lost), a function can start 8 times before the task
fails.

## Lambda pools

On a [Lambda pool](../how-to/lambda-pools.md) the claim works differently, and the rest is the
same: the handler runs each message through the worker's own path (refuse it if the deployment is
halted, fail it after 4 deliveries, fetch the payload, post `started`, run, post the result, then
delete the message), so delivery is still at least once and the first result still wins.

- **The claim.** The event source mapping receives one message per invocation (batch size 1).
  The message is then invisible for the queue's visibility timeout, 6 times the function's
  timeout, so a delivery Lambda throttles isn't handed out again while Lambda retries it. When the
  handler starts on the record, it sets the message's visibility to the invocation's remaining
  time plus 60 s. There's no heartbeat: the invocation's deadline bounds the task.
- **The ack.** The handler deletes the message as soon as the result is posted, rather than
  leaving that to the mapping, so a crash after that point can't run the task again.
- **Partial batch responses.** A record whose result couldn't be posted, or whose message couldn't
  be decoded or its payload fetched, is returned in `batchItemFailures`, and SQS delivers it
  again; the dead-letter queue (after 6 receives) catches the rest.

| Event | What aegc does | Can the function run twice? |
|---|---|---|
| The task asks for more walltime than the function allows | The engine fails it at submission with `AEGC_CONFIGURATION`. No retry. | No |
| The task reaches its walltime, or the invocation's remaining time less 20 s | The child is killed and the handler posts `AEGC_WALLTIME_EXCEEDED`. No retry. | No |
| Less than a second of the invocation is left when the record starts | The handler makes the message visible again at once and reports it as a batch item failure. | No, it hadn't started |
| The function times out, or its environment dies, during a task | No result is posted; the message becomes visible again about a minute after the invocation's deadline and runs again. | Yes |
| Lambda throttles the delivery | Lambda retries it; if it still hasn't run when the 6× visibility timeout ends, SQS delivers it again. Every delivery counts toward the 4 before `WORKER_LOST_REPEATEDLY` and the dead-letter queue's 6. | No, until it runs |
| The deployment is capped, stopped or expired | The mapping is disabled, so queued messages stay queued (and the engine fails its own). A record delivered meanwhile is refused with the state's code. Running invocations finish. No retry. | No |

## Final failures

`AEGC_BUDGET_EXCEEDED`, `AEGC_STOPPED`, `AEGC_EXPIRED` and `AEGC_NO_CAPACITY`, like
`AEGC_CONFIGURATION` and `AEGC_WALLTIME_EXCEEDED`, are ordinary task failures, not system
failures, so the endpoint doesn't retry them. Each message says why the task failed and what to
do, and comes back through the task's future like any other task failure. See
[Troubleshooting](../how-to/troubleshooting.md#tasks-fail-because-of-the-deployment-state).

When an engine fails its queued tasks, it removes their messages from the task queue in one sweep
of up to 20 receives of 10 messages. It deletes only messages that carry its own result queue and
task ids. Everyone else's stay hidden until the sweep ends and are then made visible again
together, so each is received at most once by the sweep (every receive counts toward the
dead-letter queue's limit). A message the sweep misses stays queued: if the deployment isn't
running, the next worker to receive it fails it; after `AEGC_NO_CAPACITY`, or a failure because
the limits admitted no worker, it can still run once a worker starts, and the engine drops that
late result.

The task queue's dead-letter queue (after 6 receives) is a backstop for messages a worker can't
decode at all. `aegc status` shows how many are there.

## First result wins

The engine tracks each task by its UUID. The first `result` or `system_failure` for any attempt of
a task resolves it, and later results for the same task are discarded, along with their S3
payloads. The engine remembers the last 10,000 completed tasks. If the endpoint process restarts
between two results for the same task, the new process doesn't know the first, and passes the
second on to Globus Compute as well.

## Writing idempotent tasks

- Write outputs to names derived from the inputs (or from a key you pass in), and overwrite rather
  than append.
- Make external side effects (database writes, notifications, transfers) conditional on whether the
  work is already done.
- Keep tasks short enough that a rerun is cheap, and checkpoint long ones yourself if a restart from
  the beginning would hurt.
- Don't rely on a task running exactly once per submission for counting or billing.

## When the laptop sleeps

The endpoint process is stopped or suspended, but nothing in AWS depends on it. Workers finish their
tasks and post results to the result queue. When the endpoint comes back, the same process resumes
polling and delivers them. If the endpoint container was restarted, the process starts again with
the next task submission (any task will do) and recovers the waiting results. In the live run, a
user endpoint process was killed mid-task and the result reached the submitter after the next
submission (162 s in total).

Results wait for up to 14 days. `aegc status` lists result queues with waiting results.
