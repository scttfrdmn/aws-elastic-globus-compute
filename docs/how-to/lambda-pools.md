# Run short tasks on Lambda

A Lambda pool runs an env's worker image as an AWS Lambda function instead of on EC2 workers.
The pool's SQS task queue feeds the function through an event source mapping, and Lambda starts
execution environments as tasks arrive, up to the pool's concurrency. There is no boot wait and
no idle tail, so short, bursty or infrequent tasks start in seconds and cost nothing between
bursts. In exchange, a task gets at most 30 minutes, Lambda time costs several times Spot's
per-vCPU price, and tasks on a Lambda pool are [not isolated](#security) from the pool's role.

Lambda pools came with v0.7 ([#21](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/21)).
They use on-demand Lambda functions on arm64, from the same worker image as EC2 pools.

## When to use one

| | Lambda pool | EC2 pool (Spot Graviton) |
|---|---|---|
| First result from idle | **3.5 s** in the live run (cold init about 5 s per execution environment) | about 40 s (boot and image pull) |
| Longest task | the function's timeout less 20 s: at most 1,780 s | 41,400 s (11.5 h) |
| What you pay for | each task's run time and each environment's init, per millisecond, plus requests | each worker from boot until 5 minutes after its last task |
| Price per vCPU-hour | about $0.083 (arm64, 1 vCPU per 1,769 MB) | about $0.017–0.024 on Spot (a `.large` worker with its volume and IPv4) |
| Size per task | 1,024–10,240 MB, with vCPUs in proportion (up to about 6) | [size classes](configuration.md#size-classes) up to 8 vCPUs and 32 GiB, or a whole instance |
| Task isolation | none: tasks run with the pool's function role | tasks run as uid 57439 with the task role |
| Scaling | Lambda, up to `concurrency=` | the engine, up to `max_vcpus` |

Use a Lambda pool for tasks of seconds to a few minutes that come in bursts or now and then: a
flow step, a quick lookup, a fan-out of small functions that shouldn't wait for workers to boot.
Use an EC2 pool for long tasks, steady streams of work where workers stay busy, anything over
30 minutes, and untrusted code. A deployment can have both, and tasks go to a Lambda pool only
when they [ask for it](#send-tasks-to-it).

In the live run (us-west-2, 2026-11-23, 2048 MB, concurrency 5), 100 tasks of 1 to 3 s came back
in 71.7 s, the first after 3.5 s, with no errors. See
[Performance](../reference/performance.md#v07-live-run-2026-11-23).

## Before you start

- **A worker image built with aegc 0.7 or later.** The function runs `aegc-worker lambda`, which
  older images lack; `deploy` refuses them (`a Lambda pool needs a worker image built with aegc
  0.7 or later ...`). Rebuild with `aegc image build`, or register an image with aegc 0.7 in it.
  Builds push a single image manifest (`--provenance=false --sbom=false`), not an index with
  attestations, so a Lambda pool runs the same digest an EC2 pool would. No Lambda runtime
  package is added: `aegc-worker lambda` speaks Lambda's Runtime API itself.
- **An image under Lambda's 10 GB limit.** `deploy` refuses an image whose compressed size in ECR
  is over 10 GiB; Lambda itself refuses one over 10 GB uncompressed when it creates or updates
  the function. Large science images may only fit EC2 pools.
- **The lambda-deployer policy** next to the deployer policy, for whoever runs `aegc deploy`,
  `destroy`, `stop`, `resume`, `status` and `footprint` on a deployment with Lambda pools:
  `aegc iam-policy lambda-deployer`. See [IAM policies](../reference/iam.md#lambda-deployer).
  The endpoint's operator policy already covers what engines do with Lambda pools.
- **An endpoint with aegc 0.7.** Older engines and evaluators expect every pool to have an Auto
  Scaling group. Rebuild the endpoint image (`aegc endpoint ... build`, then `stop` and `start`),
  and run `aegc endpoint ... deploy` again for a cloud endpoint.

## Deploy one

Add `lambda` to a pool's options:

```sh
aegc deploy e2e --pool geospatial --pool geospatial:lambda,memory=4096,timeout=300,concurrency=20
```

This keeps the EC2 pool `geospatial-py314` and adds the Lambda pool `geospatial-py314-lambda`.
Lambda pools are named `<env>-py<NNN>-lambda`, one per Python minor of the env, like EC2 pools.

| Option | Default | Meaning |
|---|---|---|
| `lambda` | | Make this a Lambda pool |
| `memory=MiB` | 2048 | The function's memory, 1,024 to 10,240. Lambda gives one vCPU per 1,769 MB, so 2048 MB is about 1.16 vCPUs and 10,240 MB about 5.8 |
| `timeout=S` | 900 | The function's timeout, 60 to 1,800 s |
| `concurrency=N` | 10 | The mapping's maximum concurrency, 2 to 1,000: at most this many tasks run at once |
| `tmp=MiB` | 512 | The function's `/tmp`, 512 to 10,240 |
| `max_walltime=S` | timeout − 20 | The most walltime a task may ask for; at most timeout − 20 |
| `walltime=S` | `max_walltime` | Walltime for tasks that don't pass `walltime_s` |
| `python=3.NN` | every minor | Only the env's image for this Python minor |

Each invocation keeps 20 s for posting the result, hence timeout − 20. EC2 options such as
`max_vcpus`, `vcpu`, `size` or `on-demand` are refused (`max_vcpus is for EC2 pools, not Lambda
pools ...`), and so are Lambda options on an EC2 pool (`memory need a Lambda pool:
geospatial:lambda,...`). A redeploy keeps the options it isn't given, as for EC2 pools; a new
`timeout=` moves walltimes that were derived from the old one along with it.

`deploy` creates, for each Lambda pool:

- the task queue and dead-letter queue, with the task queue's visibility timeout at 6 times the
  function's timeout (AWS's advice for SQS event sources, so a delivery Lambda throttles isn't
  handed out again while Lambda retries it), at most 12 hours;
- the function role `aegc-<d>-<pool>-<region>`, with the pool's own policy and the deployment's
  grants;
- the function `aegc-<d>-<pool>` from the pool's image by digest, arm64, its command
  `aegc-worker lambda`, logging to `/aegc/<d>`. `deploy` waits until Lambda has finished
  preparing the image, which can take a few minutes after each change;
- the event source mapping from the task queue to the function: batch size 1, partial batch
  responses, and maximum concurrency `concurrency=`;
- the statement `AegcLambdaPull` in the `aegc/<env>` repository policy, so Lambda may pull the
  image for `aegc-*` functions.

See [Resources and tags](../reference/resources.md#lambda-pool-aegc-deploy-pool-envlambda).
Function and role names are limited to 64 characters, so long deployment and env names are
refused before anything changes.
Every deploy with a Lambda pool prints a note that Lambda pools aren't isolated (see
[Security](#security)). `--prune --yes` removes a Lambda pool you no longer list: its mapping,
function, role and queues.

## Send tasks to it

The engine sends a task to a Lambda pool only in two cases, so that a task without a walltime, or
a long one, isn't cut off at 30 minutes:

- **It names the pool.** `user_endpoint_config={"pool": "geospatial-py314-lambda"}`.
- **Only Lambda pools serve its env and Python.** If the env has no EC2 pool for the submitter's
  Python minor, its tasks go to the env's Lambda pool.

Otherwise `env`, `size`, `cpus` and `memory_gib` choose among the env's EC2 pools, as before. Use
one executor per pool:

```python
from globus_compute_sdk import Executor

quick = Executor(
    endpoint_id=ENDPOINT_ID,
    amqp_port=5671,
    user_endpoint_config={"pool": "geospatial-py314-lambda", "walltime_s": 120},
)
heavy = Executor(
    endpoint_id=ENDPOINT_ID, amqp_port=5671, user_endpoint_config={"env": "geospatial"}
)
```

The submitter's Python must still match the pool's. `cpus` and `memory_gib`, if given with
`pool`, must fit what the function gives one task: its memory, and memory ÷ 1,769 MB vCPUs
(at 4096 MB, `pool 'geospatial-py314-lambda' gives a task 2.32 vCPUs and 4 GiB, less than
...`). Each execution environment runs one task at a time.

## Walltime

A task's walltime is checked twice:

- **When it's submitted.** A `walltime_s` above the pool's `max_walltime` (at most the timeout
  less 20 s) fails at once with `AEGC_CONFIGURATION`: `walltime_s 600 is more than Lambda pool
  'plain-py314-lambda' allows: its function aegc-lam-plain-py314-lambda times out after 300 s, so
  a task gets at most 280 s (20 s are kept for posting the result). Ask for less, or use an EC2
  pool for longer tasks.`
- **When it starts.** The handler clamps it to the invocation's remaining time less 20 s, so a
  task whose record waited, or started late in an invocation, is still stopped in time to post
  its result. In the live run, a task sleeping 400 s with `walltime_s=280` on a 300 s function
  was killed at 279 s, and its `AEGC_WALLTIME_EXCEEDED` result arrived before the function timed
  out. A record with less than a second left goes back to the queue for another invocation.

The 20 s cover the walltime enforcement (5 s from Globus Compute's timeout to `SIGTERM`, 10 s
more to `SIGKILL`) and posting the result.

## Stop, caps and the lease

The engine doesn't scale Lambda pools; Lambda does, up to `concurrency=`. Wherever aegc would
lower an EC2 pool's minimum, it disables a Lambda pool's event source mapping instead, so nothing
new is delivered, and wherever it would restore a minimum, it enables the mapping again:

| Event | What happens to the mapping |
|---|---|
| `aegc stop` (and `--hard`) | Disabled. Invocations already running finish: Lambda can't stop them early, and `--hard` (like `--on-cap kill`) doesn't reach them |
| A spend cap trips (an engine, or a cloud endpoint's `aegc evaluate`) | Disabled |
| The lease or TTL fires | Disabled by the `lease-<pool>` or `ttl-<pool>` schedule, through EventBridge Scheduler's universal target `aws-sdk:lambda:updateEventSourceMapping` with `{"Uuid": ..., "Enabled": false}` |
| `aegc resume`, `aegc limits --resume` | Enabled |
| A cap ends at the end of its day or month | Enabled by the next engine pass or `aegc evaluate` |

The handler also reads the deployment state, at most a minute apart in each execution
environment, and refuses records that arrive while it isn't `running` with the state's code
(`AEGC_STOPPED`, `AEGC_BUDGET_EXCEEDED`, `AEGC_EXPIRED`), as an EC2 worker does. So a few records
delivered in the moments after a stop can still be refused rather than run.

**Mappings follow the deployment state.** Every engine checks each Lambda pool's mapping on each
scaler pass (every 10 s), and a cloud endpoint's `aegc evaluate` every 5 minutes: while the
deployment reads `running`, a disabled mapping is enabled again; while it doesn't, an enabled one
is disabled. That's how a cap that ends by itself turns its Lambda pools back on, and how a
freeze whose update failed is retried. It also means a mapping you disable by hand, in the console
or with the AWS CLI, is re-enabled within seconds while an endpoint runs. To stop a Lambda pool,
use `aegc stop`, or remove the pool with `deploy --prune --yes`. A redeploy never enables a
mapping itself; it reports a disabled one as `kept`.

In the live run, `aegc stop` disabled the mapping and new tasks were refused, `aegc resume`
enabled it again, a $0.01 daily cap tripped by `aegc evaluate` disabled it and
`limits --max-usd-day 5 --resume` re-enabled it, and a 3-minute TTL disabled it through the
Scheduler target.

## Status, metrics, logs and provenance

`aegc status` shows a Lambda pool's function and mapping instead of workers (values are
illustrative):

```
  pool geospatial-py314-lambda: env geospatial, Python 3.14 | tasks queued 0, running 3, dead-lettered 0 | lambda 2048 MB x 10, event source mapping Enabled
    1.16 vCPU(s), 2 GiB per task | $0.2880/h | running tasks could cost up to $0.07 more
```

`running` is the queue's in-flight count, so it includes records waiting after a throttle (see
[Troubleshooting](#troubleshooting)). The rate is one busy environment per running task. The
mapping's state is `Enabled`, `Disabled`, or on its way (`Enabling`, `Disabling`, `Updating`);
`unknown (...)` means it couldn't be read.

`aegc metrics` replaces the capacity figures with the function's own metrics (`AWS/Lambda`;
values illustrative):

```
  pool geospatial-py314-lambda: 100 task(s) completed (100 ok), 0 failed
    exec avg 2.0 s, max 3.0 s | queue wait avg 1.9 s, max 5.8 s | boot to first claim avg 5.3 s
    releases 0, poison 0 | concurrent executions max 5, throttles 0
```

"Boot to first claim" is an execution environment's init to its first task. The handler also
writes `InitSeconds` (namespace `aegc`, dimensions `Deployment` and `Pool`) once per environment,
before any task runs there; aegc uses it to [count init time](#cost).

**Logs.** The function logs to `/aegc/<d>`, in Lambda's own streams
(`2026/11/23/[$LATEST]0123abcd...`), as JSON lines like a worker's, with `function` instead of the
instance fields and `instance_id` set to `lambda-<environment id>`, next to Lambda's `START`,
`END` and `REPORT` lines. Your tasks' output is in the same stream.

**Provenance.** `details["aegc"]` on a Lambda pool has `backend: "lambda"` and, in place of the
instance fields:

| Key | Meaning |
|---|---|
| `function`, `function_version`, `memory_mib` | The function, its version (`$LATEST`) and memory |
| `request_id`, `log_stream` | The invocation that ran the task, and its log stream |
| `instance_id`, `instance_type`, `slots` | `lambda-<environment id>`, `lambda-<memory>MB`, and 1 |
| `est_cost_usd`, `est_cost_source` | The task's run time at the function's memory, plus one request, at the static price (`static`). Init isn't included |

`aegc provenance` prints it as

```
  attempt 1 (delivery 1): ok in 2.1 s, queued 0.4 s
    Lambda aegc-e2e-geospatial-py314-lambda (2048 MB), request 5c2e7a1b-... | pool geospatial-py314-lambda
    image 123456789012.dkr.ecr.us-west-2.amazonaws.com/aegc/geospatial@sha256:4f6e...
    estimated cost $0.000056 (static)
```

## Cost

Lambda bills arm64 functions per millisecond of handler time at the function's memory, and, for
container images, for the init time of each execution environment too; plus each request. aegc
prices both with a static table for us-west-2, read on 2026-11-23 (us-east-1's is the same):
**$0.0000133334 per GB-second** and **$0.20 per million requests**.

| Memory | One busy environment | 10 busy (the default concurrency) |
|---|---|---|
| 1,024 MB | $0.048/h | $0.48/h |
| 2,048 MB (default) | $0.096/h | $0.96/h |
| 4,096 MB | $0.192/h | $1.92/h |
| 10,240 MB | $0.480/h | $4.80/h |

A 2 s task at 2048 MB costs about $0.000054; a cold start (about 5 s of init) adds about
$0.00014 per environment. A Spot `c8g.large` worker costs about $0.043 an hour with its volume
and IPv4 address, and lives at least 5 minutes past its last task. So Lambda wins for work that
would leave workers idle, and loses for work that would keep them busy: per vCPU-hour it's about
4 to 5 times Spot.

**How aegc counts it.** Not from the cost ledger: tasks run with the function role, which could
rewrite anything it's allowed to write, and an invocation that times out never writes. `aegc
cost`, `aegc status` and the spend limits read Lambda's own `Duration` and `Invocations` metrics
for each function (hourly sums), add the handler's `InitSeconds`, and price them at the pool's
memory. The window is rounded out to whole hours, so a window that doesn't start on the hour runs
a little high. `--by type` shows Lambda pools as `lambda-<memory>MB`:

```
  lambda-2048MB: $0.0139
```

That's the live run's figure: about 500 s at 2 GB, plus init.

**Spend limits.** Lambda pools count toward `--max-usd-day` and `--max-usd-month`, not toward
`--max-instance-hours-day`. Admission doesn't hold a Lambda pool back, since aegc doesn't scale
it; instead, what its running tasks cost now (one busy environment per task in flight) counts
against EC2 pools' admission. A Lambda burst is caught when the cap trips: engines re-read spend
every 5 minutes, and Lambda's metrics arrive a few minutes late, so a pool can overshoot by about
`concurrency` × its hourly rate for 10 minutes, plus invocations already running when the mapping
is disabled. `concurrency=` is the bound: at the default 2048 MB × 10, that's at most $0.96 an
hour. See [Spend limits](../concepts/cost.md#spend-limits).

aegc doesn't check the account's Lambda concurrency or other quotas, and Lambda's free tier isn't
counted.

## Security

**Lambda pools aren't isolated.** Lambda hands the function role's credentials to the process in
environment variables and has no uid to drop to, so task code runs with the pool's function role.
That role holds what `aegc-worker lambda` needs for one pool: its own task queue, the deployment's
result queues, the deployment's task payloads (read) and result payloads (write), its state and
manifest, its log group and its image, plus the deployment's grants. A task on a Lambda pool can
therefore:

- read every task payload of the deployment under `payloads/<d>/tasks/`, including tasks for
  isolated EC2 pools;
- write result payloads and send messages to every result queue of the deployment, so it could
  forge a result for another pool's task;
- use every grant of the deployment, like any task.

It can't reach the cost ledger, Auto Scaling, the task role or other pools' task queues, and it
can read the deployment state but not change it. Per-pool payload prefixes, which would close the
first two, are tracked in [#80](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/80).

So: **keep untrusted functions on isolated EC2 pools, in a deployment without Lambda pools.** Use a
Lambda pool where everyone who can submit to the endpoint is trusted as much as the deployment's
own code. `deploy` prints a note saying so for every Lambda pool, and a pool can't be made both
Lambda and isolated.

Execution environments are reused: `/tmp` (with the task scratch in `/tmp/aegc` and `HOME` in
`/tmp/aegc/home`) persists from one task to the next in the same environment. Don't leave
secrets there.

## Troubleshooting

- **Tasks wait, `aegc metrics` shows throttles.** Lambda limits concurrent executions per
  account and region (1,000 by default; new accounts can have as few as 10), shared by every
  function, and aegc doesn't check it. Lambda retries a throttled delivery for a while (the
  queue's visibility timeout, 6 times the function's timeout, leaves it room); a record still
  not run when that runs out is delivered again, and every delivery counts. A task delivered a
  5th time fails with `WORKER_LOST_REPEATEDLY`, and a message received 6 times without running
  goes to the dead-letter queue. Meanwhile `aegc status` counts it as running. Check the
  account's limit with `aws lambda get-account-settings` (`AccountLimit`), and keep the pools'
  `concurrency=` within it, or ask for more with Service Quotas.
- **`a Lambda pool needs a worker image built with aegc 0.7 or later ...`:** rebuild the env's
  image with `aegc image build`. With several Python minors, `deploy` skips the old ones with
  `warning: no Lambda pool for <env> Python ...: built with aegc < 0.7`. `--allow-old-image`
  deploys the image anyway, but a function without `aegc-worker lambda` fails as soon as Lambda
  starts it.
- **`image ... is N GiB compressed; Lambda allows 10 GiB uncompressed`:** use an EC2 pool for that
  env, or a smaller image. A `CreateFunction` or `UpdateFunctionCode` error about the image's size
  means the same for an image under 10 GiB compressed.
- **`Lambda function aegc-... failed: ...`** from `deploy`: Lambda couldn't prepare the image; the
  reason follows. `deploy` waits up to 15 minutes for it.
- **`... is longer than 64 characters; use a shorter deployment or env name`:** the function or
  role name doesn't fit Lambda's or IAM's limit.
- **`Lambda memory (memory=) must be 1024-10240 MiB`**, **`Lambda timeout (timeout=) must be
  60-1800 s`**, **`Lambda concurrency (concurrency=) must be 2-1000`**, **`Lambda /tmp (tmp=) must
  be 512-10240 MiB`**, **`max_walltime=N doesn't fit timeout=T`:** fix the pool option. aegc
  needs at least 1 GiB: the runtime, a forkserver that preloads Globus Compute, and the task share
  the function's memory.
- **The first task in each environment takes about 5 s longer.** That's the environment's init
  (Python, Globus Compute and the forkserver), which Lambda bills for container images. A burst
  pays it once per environment, up to `concurrency=` times.
- **`AccessDeniedException` from `deploy`, or `couldn't disable the event source mapping` from
  `stop`:** your credentials have the deployer policy but not
  [lambda-deployer](../reference/iam.md#lambda-deployer). `stop` still writes the state, so
  records that reach the function are refused, but the mapping keeps delivering until an
  engine or `aegc evaluate` disables it; `aegc status` shows the mapping as
  `unknown (AccessDeniedException)`. `destroy` removes everything else, reports
  `remaining  lambda pools <d> (not checked: needs the lambda-deployer policy ...)` and exits
  with an error; run it again with both policies.
- **A task ran twice.** Delivery is at least once, as on EC2; see
  [Delivery and idempotency](../concepts/delivery.md#lambda-pools).
