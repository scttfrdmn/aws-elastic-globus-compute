# Performance

Results from the live Globus Compute end-to-end run on 2026-11-14 in us-west-2
([#15](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/15)). They come from one
deployment on one day; Spot capacity, instance types and image size all change the numbers.

## Setup

- Endpoint: a container on a laptop, Globus Compute endpoint 4.21.0, Python 3.14.
- Deployment `e2e`: one pool, `geospatial` (aarchsci 2026.11.13.154112, Python 3.14), at most 3
  workers.
- Spot Graviton workers with 2 slots each.
- Worker image pulled from ECR; its base came through the pull-through cache.

## Results

| Check | Result |
|---|---|
| Cold start, 0 workers to first result | **39 s** (46 s on a second cold run); 20 tasks done in 43–50 s |
| 100 tiny tasks, 3 warm workers | **13.2 s** |
| 3 MB task payload, then 3 MB result (S3 path) | **8.0 s** for both |
| Exception propagation | remote `ValueError` and traceback returned |
| Python mismatch (3.13 submitter) | clear error: `no worker image for Python 3.13 in env 'geospatial'; available: 3.14` |
| SIGKILL the user endpoint process mid-task, then submit anything | the new process recovered the result through `unacked_results`; the submitter got it (162 s in total) |
| Terminate all workers mid-task (through Auto Scaling, without decrementing) | the busy worker released its task on SIGTERM (`released=1`); a replacement was up 24 s after launch; the task reran on another host and was delivered |
| Idle self-drain | workers leave the group about 5 min after going idle |
| Scale to 0 after the last task | **5.5 min** (300 s idle plus about 30 s) |
| `aegc destroy e2e --yes` | no e2e instances, volumes, network interfaces, queues, parameters, roles or log groups left; the tagging API still lists terminated instances for a while |

## Reading the numbers

- **Cold start** covers the scaler's scale-out, instance launch, boot, the image pull and the first
  claim. Bigger images and scarcer Spot capacity make it longer.
- **Per-task overhead.** 100 tasks that do almost nothing took 13.2 s on 6 slots: about 0.13 s per
  task overall, or about 0.8 slot-seconds per task if every slot was busy throughout. Each task passes through Globus, the endpoint, two SQS
  queues and a fresh process. Batch work so each task runs for at least about 10 s.
- **Large payloads** go through S3 in both directions. 8 s for 3 MB each way includes the Globus
  hops, which carry the full payload too.
- **Scale to zero** takes the idle period (`idle=`, default 300 s) plus instance shutdown.

## Bugs found by the live run

Fixed before v0.1 (see the [changelog](../../CHANGELOG.md)):

- User endpoint processes idle-stopped with tasks in flight: replayed `unacked_results` counted as
  forwarded. The template now sets `idle_heartbeats_soft: 0`.
- Image tags now include the aegc wheel's hash, so a code change gets a new immutable tag.
- The Auto Scaling group's `aegc:role` tag no longer overrides the workers' role tag.
- `aegc endpoint start` waits for a first-time registration.
- The endpoint image runs Python 3.14.
- Workers log each task.
- A Spot rebalance recommendation 5 s after launch made a worker drain and exit while its instance
  stayed in the group, holding the pool above zero for 14 minutes until the watchdog removed it.
  Workers now keep working; capacity rebalance replaces the instance, and SIGTERM releases tasks.
