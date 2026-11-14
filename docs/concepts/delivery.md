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
| The endpoint process stops with tasks in flight | Workers keep going and post to the result queue, which keeps messages for 14 days. The next process with the same endpoint id picks them up and writes them to `unacked_results/`, and Globus Compute republishes them. | No |

*Retries once* is the template's `max_retries_on_system_failure: 1`: the endpoint resubmits the task
as a new attempt after a system failure, and reports the failure if the retry fails too. In the
worst case (every delivery of both attempts lost), a function can start 8 times before the task
fails.

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
