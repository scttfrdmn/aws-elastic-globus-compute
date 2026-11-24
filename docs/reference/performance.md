# Performance

Results from the live Globus Compute end-to-end run on 2026-11-14 in us-west-2
([#15](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/15)). They come from one
deployment on one day; Spot capacity, instance types and image size all change the numbers.
Later runs, for [v0.2](#v02-live-run-2026-11-15), [v0.3](#v03-live-run-2026-11-15),
[v0.4](#v04-live-run-2026-11-16), [v0.6](#v06-live-run-2026-11-23) and
[v0.7](#v07-live-run-2026-11-23), are at the end.

## Setup

- Endpoint: a container on a laptop, Globus Compute endpoint 4.21.0, Python 3.14.
- Deployment `e2e`: one pool, `geospatial` ([aarchsci](https://aarch.science/) 2026.11.13.154112, Python 3.14), at most 3
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

## v0.2 live run (2026-11-15)

us-west-2, deployment `v02`, pool `geospatial:max_vcpus=6` (vCPU-weighted), Spot Graviton workers
with a 30 GiB gp3 root at 125 MiB/s, the endpoint in Docker on a laptop.

| Check | Result |
|---|---|
| Cold start, 0 workers to first result, 125 MiB/s root volume | **38 s** (v0.1 at 500 MiB/s: 35–46 s); 10 tasks done in 41 s |
| vCPU-weighted scale-out | desired 6 vCPUs, filled with 2-vCPU `m9g.large` Spot workers |
| Cost ledger | real Spot prices; about $0.03 for the whole run |
| Spend cap | a $0.0001/day limit set on the running deployment tripped in **42 s**; new tasks refused with `AEGC_BUDGET_EXCEEDED` |
| `limits --resume` | refused while over the limit; raising the limit with `--resume` resumed and renewed the lease |
| `aegc stop` | the running task finished; a new one got `AEGC_STOPPED`; the worker left 3 s after its task |
| `aegc stop --hard --yes` | a 10-minute running task failed with `AEGC_STOPPED` about 45 s after the stop; the worker left by itself |
| Redeploy pickup | the running engine loaded the new manifest within 15 s (no restart) |
| TTL (`--ttl 15m`, a `min_vcpus=2` worker) | at the TTL the schedules set the state to `expired` and the minimum to 0; the worker left within 85 s |
| `destroy --yes` (verifies) and `footprint` | nothing remains |

**Found and fixed during the run:**
- A new scheduler role takes seconds before Scheduler can assume it. Lease creation now retries.
- A task submitted right after `resume` was refused from a 30-second-old copy of the state. The
  engine now re-reads a stale state before refusing.
- The tagging index keeps deleted EC2 resources for a while, and schedule groups delete
  asynchronously. `footprint` and `destroy --verify` no longer report those.

**AWS behaviour checked:** in a vCPU-weighted group, terminating a 2-vCPU worker with the
decrement while desired is 1 succeeds and clamps desired to 0 (substrate#1526).

## v0.3 live run (2026-11-15)

us-west-2, deployment `v03`, the endpoint in Docker on a laptop with Python 3.13. Pools
`byo:max_vcpus=4` and `byo:size=mem,max_vcpus=4`, on Spot Graviton workers. Grants
`read=s3://<test bucket>/in/` and `write=s3://<test bucket>/out/`.

| Check | Result |
|---|---|
| Bring your own image | `aegc image build --env byo --base <local python:3.13-slim + numpy 2.x image>` built with `Dockerfile.pip` (a pip base), detected Python 3.13, passed the selftest, and was pushed and recorded; AWS accepted both SSM record parameters |
| `aegc image register` | a hand-built `python:3.13-slim` image (the aegc wheel, `globus-compute-endpoint` 4.21.0, `USER 57439`) passed its checks and was copied into `aegc/byoreg`; an image running as uid 1000 was refused before anything was pushed |
| Python routing | a 3.13 client's tasks ran on the 3.13 pool |
| Cold start, 0 workers to first result | **38–39 s**; a warm task 4 s |
| Size classes, through `user_endpoint_config` | `size=mem`, and `memory_gib=12`, landed on `r9g.large` (2 vCPUs, 15.3 GiB visible, so 1 slot); `cpus=64` was refused with `no pool ... available: ...`; the default pool used `m7g.large` |
| Read grant | the task read its input under `in/` |
| `aegc.outputs` | `outputs.put` wrote a 20 MiB object to `out/<task uuid>/random.bin` |
| A write outside the grant | `AccessDenied` from IAM, and `PermissionError` from `aegc.outputs`' own check |
| Cost | about **$0.012** (0.29 instance-hours, Spot) |
| `destroy --yes` (verifies) and `footprint` | nothing remains; the test bucket, image records and repositories were removed afterwards |

**Found and fixed during the run:**
- `Dockerfile.pip` linked `aegc-worker` onto itself when pip had installed it in
  `/usr/local/bin`, as on the official `python:*` images, so the build failed.
- The Globus Compute web service validates `resource_specification` as an MPI request: it refuses
  one without `num_nodes` (HTTP 422) and drops every other key. aegc's task options moved to
  `user_endpoint_config`; see [resource_specification](configuration.md#resource_specification).

## v0.4 live run (2026-11-16)

us-west-2, deployment `v04`, the endpoint in Docker on a laptop (`globus-compute-endpoint`
4.21.0), Spot Graviton workers from images built with aegc 0.4, a read and a write grant on a
test bucket, and Globus Flows with the Compute action provider v3.

| Check | Result |
|---|---|
| Task isolation, probed from inside a task | the task's identity was the task role, `aegc-v04-task-us-west-2`; the instance metadata service refused the connection; SQS, Auto Scaling, the deployment's state parameter and the cost ledger returned `AccessDenied`; `/proc/1/environ` was unreadable; reading under the read grant and `aegc.outputs.put` under the write grant worked |
| `aegc metrics` | per-pool task counts, times and capacity reported from the workers' EMF metrics and the group metrics |
| `aegc provenance` | each attempt's record found in the log group; it showed the stop/claim race below |
| `details["aegc"]` | in the SDK's `Client().get_task(task_id)["details"]["aegc"]`; not in the Flows action's output |
| Globus Flows, one Compute action | `SUCCEEDED` in about **37 s** from start on a warm pool (`WaitTime` 900); the submission carried no Python version, and `user_endpoint_config` `{"env": ..., "python": "3.14"}` picked the 3.14 pool; the result was at `details.result[0]` |
| Flows identity | a run started by another identity failed at once with `ENDPOINT_ACCESS_FORBIDDEN` |
| Globus Timers | a timer with `--stop-after-runs 1` ran the flow once; the run `SUCCEEDED` |
| `--allow-function` | the allowed function ran; another registered function got `403 FUNCTION_NOT_PERMITTED` at submission |
| Cost | about **$0.016** (0.42 instance-hours, Spot) |
| `destroy --yes` (verifies) and `footprint` | nothing remains |

**Found and fixed during the run:**
- Isolated tasks got `NoRegionError` from boto3: botocore had found the region through instance
  metadata, which tasks can't reach any more. The agent now sets `AWS_REGION` and
  `AWS_DEFAULT_REGION` for task processes.
- `aegc provenance` showed a task released by a worker that was stopping, though it had finished:
  SIGTERM raced a claim, so the stop released nothing, the task then finished, and its result was
  dropped as if released, with its message hidden for the full visibility timeout (300 s). A task
  that finishes while its worker stops now posts its result, and anything still claimed when the
  worker exits goes back to the queue at once.

## v0.6 live run (2026-11-23)

The [cloud endpoint](../how-to/cloud-endpoint.md), its evaluator and offline alarm, a second
region, and three of the [tutorials](../tutorials/index.md). Deployment `v06` in us-west-2 with a
`t4g.micro` cloud endpoint, a us-east-1 deployment for the 1000 Genomes slice, Spot Graviton
workers from images built with this version.

| Check | Result |
|---|---|
| Cloud endpoint | `aegc endpoint deploy --stop-local` moved a laptop endpoint to EC2 with the **same endpoint id**, running as the user's Globus login from Secrets Manager. With the laptop endpoint stopped, a task from the SDK ran on an isolated worker and came back in **40 s** |
| Host replacement | terminating the host: the group replaced it with the same endpoint id, and the new user endpoint process **recovered a result** the old host had left in the result queue |
| Evaluator, cap without an engine | a $0.0001/day limit, with no user endpoint process running (a freshly replaced host has none until a task arrives): the first `aegc evaluate` pass tripped it, state `capped`, `by: evaluator`, `$0.0087 of $0.0001/day`, group minimums to 0 |
| Evaluator, lease | renewed only while Globus reported the endpoint online |
| Offline alarm | `EndpointOnline` arrived every 5 minutes. With the host scaled to 0, the alarm went to `ALARM` ("no datapoints were received for 3 periods ... treated as Breaching") about **25 minutes** after the last datapoint. Email delivery wasn't checked (the subscription was left unconfirmed) |
| `destroy --yes` with a cloud endpoint | removed the group, launch template, role, secret, alarm and topic, and verified |
| us-east-1 bootstrap | created the payload bucket without a location, then tagged it |
| us-east-1 deploy | `skipped  subnet ... (us-east-1e, use1-az3)`: the zone offers no Graviton |
| [1000 Genomes slice](../tutorials/genomics-slice.md) (us-east-1) | a 696.3 MiB slice of the 26.0 GiB chr22 VCF (2.61 %), 49,943 records; 335.7 KiB to the laptop; bcftools 145.2 s; **195.3 s** from submission to result, about 40 s of it the cold start; `c7g.large` Spot in us-east-1f; $0.0062 (0.13 instance-hours) |
| [Spot sweep](../tutorials/spot-sweep.md) under FIS | 200 tasks in 446 s undisturbed (Spot, 6 instance types, average task 58.3 s) and in 427 s with FIS interrupting 8 workers 99 s in; 16 tasks reran and all 200 answers matched; `aegc metrics`: releases 16, worker exits 8, interruptions 2 (FIS sends a rebalance recommendation first, and Auto Scaling replaced most of the 8 before the notice); $0.2313 for both runs (5.37 instance-hours) |
| [Sentinel-2 NDVI](../tutorials/sentinel2-ndvi.md), part 1 | 1.30 MB read of the scene's 463.2 MB red, NIR and SCL assets (0.28 %); a 4.8 MB NDVI GeoTIFF; 2.0 s in the task, **39.4 s** from submission to result (34.5 s waiting for a cold worker); `c9g.large` Spot |

The cold starts, 35 to 40 s from zero workers to a running task, are in line with earlier runs
(35 to 46 s). The cloud endpoint checks ran before the review fixes in the
[changelog](../../CHANGELOG.md) (the liveness metric was still EMF rather than PutMetricData with
a token, and Session Manager was always on); the alarm's settings are the same. The NDVI tutorial's nightly part and
[One flow, two endpoints](../tutorials/two-endpoints.md) haven't been run live yet.

**Found and fixed during the run:**
- The host wrote the endpoint id (`endpoint.json`) before configuring the endpoint, so configure
  thought the endpoint existed, skipped it, and `config.yaml` was never written. The id is now
  written after configure.
- User endpoint processes, where `AWSEngine` runs, log to files that didn't reach CloudWatch. The
  endpoint container now copies them to its console, prefixed `[uep]`.
- The host needed a shell for debugging without SSH; Session Manager is now available, opt-in with
  `--session-manager`.
- The earth-observation image ships python-dateutil 4.0, which botocore doesn't accept, and the
  pip step pinned every base package, so the build failed. `image build --allow-change` now leaves
  the named packages out of the pip constraints too.
- `bytes_read` came back empty on isolated workers: after `setuid` from root, Linux marks a process
  non-dumpable, and its own `/proc/self` files belong to root. Task processes restore the flag.
- FIS returns the interrupted instances as ARNs; the sweep script now prints their ids.

## v0.7 live run (2026-11-23)

[Lambda pools](../how-to/lambda-pools.md)
([#21](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/21)). us-west-2, deployment
`lam`: a Lambda pool `plain-py314-lambda` (2048 MB, timeout 300 s, concurrency 5, the image `plain`
built over `python:3.14-slim` with aegc 0.7) next to an EC2 pool. Tasks were submitted through a
local `AWSEngine`, without Globus in the loop.

| Check | Result |
|---|---|
| Runtime | `aegc-worker lambda` served invocations; cold init about **5.2 s** per execution environment (the forkserver started at init). A task saw the function role, `HOME=/tmp/aegc/home`, 2 vCPUs and 512 MB of `/tmp` |
| Burst of 100 tasks of 1–3 s | first result **3.5 s** after submission; all 100 back in **71.7 s**; 0 errors. Provenance showed the function, memory, request id, log stream and estimated cost |
| Walltime at submission | `walltime_s=600` refused at once with `AEGC_CONFIGURATION`, naming the 300 s timeout |
| Walltime at run time | a task sleeping 400 s with `walltime_s=280` was killed at 279 s (remaining time less 20 s), and its `AEGC_WALLTIME_EXCEEDED` result posted before the function timed out |
| `aegc stop`, `aegc resume` | `stop` disabled the mapping and new tasks were refused at submission; `resume` enabled it and tasks ran |
| Cap | `aegc limits --max-usd-day 0.01` and `aegc evaluate` capped the deployment and disabled the mapping; `limits --max-usd-day 5 --resume` enabled it again |
| TTL | a 3-minute TTL fired: state `expired`, and the mapping disabled by EventBridge Scheduler's universal target, so the scheduler role's `lambda:FunctionArn` and `aws:ResourceTag` conditions hold in AWS |
| Cost | `aegc cost` showed `lambda-2048MB: $0.0139`, in line with about 500 s at 2 GB plus init |
| `destroy` | the dry run listed the mapping, function, role, queues and schedules; `--yes` deleted them, verification found nothing left, and `footprint` was empty |

**Found and fixed during the run:**
- Scheduler's universal target names its fields as the SDKs serialize them: it refused `UUID`
  ("Request payload is missing the following field(s): Uuid"). The `lease-<pool>` schedules of
  Lambda pools now send `{"Uuid": ..., "Enabled": false}`.
- Lambda's `Duration` metric leaves out init time, which container-image functions are billed
  for (about 5 s per environment). The handler now writes an `InitSeconds` metric before any task
  runs in an environment, and Lambda spend includes it.

### Lambda against Spot EC2: a burst of short tasks (2026-11-24)

A synthetic baseline for [Lambda pools](../how-to/lambda-pools.md): 500 tasks of 1–5 s of CPU work
(SHA-256 rounds; 1,460 s of work in all, the same durations every run), sent through Globus Compute
to a Lambda pool (2048 MB, concurrency 16) and to a Spot pool of up to sixteen 2-vCPU workers
(16 slots) in us-west-2, each from idle. [examples/lambda-burst/](../../examples/lambda-burst/)
has the script. The real-data comparison is the
[cohort tutorial](../tutorials/cohort-genotype.md).

| Run | First result | All 500 |
|---|---|---|
| Lambda, cold functions and a cold endpoint process | 18.8 s | 136.9 s |
| Lambda, warm functions | 4.8 s | 123.5 s |
| Lambda, cold functions, warm endpoint process | 7.1 s | 130.8 s |
| Spot EC2 from zero | 38.5 s | 137.9 s |
| Spot EC2 from zero, again | 35.6 s | 135.2 s |

Cost per run (`aegc cost`): Lambda **$0.041** (three runs, $0.124), Spot **$0.040** (two runs,
$0.079 and 1.9 instance-hours, boot and idle included).

With every slot busy for two minutes, both finish together and cost the same: Spot's boot and
5 idle minutes per worker use up its lower price. Lambda's results start about 30 s sooner. A
smaller or sparser burst favours Lambda (nothing idles between bursts); longer or steadier work
favours Spot, about 4–5 times cheaper per vCPU-hour once its boot and idle time are spread thin.

