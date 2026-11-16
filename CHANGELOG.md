# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x until further notice).

## [Unreleased]

### Added
- **Task isolation** for pools whose images are built (or registered) with aegc 0.4 or later:
  task code no longer runs with the worker role. The worker container runs as root with
  `no-new-privileges` and only `SETUID`, `SETGID`, `KILL`, `CHOWN`, `DAC_OVERRIDE` and `FOWNER`;
  each task process drops to uid 57439 before it deserializes anything; host iptables rejects
  instance metadata for that uid, and launch templates turn the IPv6 metadata endpoint off. A new
  per-deployment **task role**, `aegc-<d>-task-<region>`, trusted only by the worker role, holds
  the grants (`aegc-grants`) and the manifest read (`aegc-task`); the agent assumes it and serves
  its credentials on 127.0.0.1 in the container-credentials format
  (`AWS_CONTAINER_CREDENTIALS_FULL_URI` and a token file), with `AWS_REGION` and
  `AWS_DEFAULT_REGION` set, so boto3, the AWS CLI and GDAL work unchanged. Results come back over
  a pipe instead of a file in the task's directory, and the engine ignores a result whose S3
  payload isn't that task's own result key. The worker policy gains `sts:AssumeRole` on the task
  role. Pools with older images keep the old behaviour, and `aegc deploy` warns about them; the
  worker role keeps `aegc-grants` while such pools, or workers launched before their pool was
  isolated (instances without the new tag `aegc:isolated=true`), remain, and a later deploy
  removes it. Tasks on one worker still share uid 57439, so they can see and signal each other's
  processes and read each other's run directories (#32).
- **JSON worker logs:** `aegc-worker run` logs one JSON object per line (stdlib only): `ts`,
  `level`, `logger`, `msg`, the worker's `deployment`, `pool`, `instance_id`, `type`, `az` and
  `lifecycle`, and per-task `task_uuid`, `attempt`, `etid`, `receive_count`. Each task ends with
  a `task finished` line (`"event": "task_finished"`) carrying its `outcome`, whether it was
  `posted`, phase times (`received_at`, `started_at`, `finished_at`, `exec_s`, `queue_wait_s`)
  and its provenance record. That line replaces the old per-task outcome line; other messages
  are unchanged. Task output goes to the worker's stderr. `selftest` and `version` keep plain-text
  logs (#39).
- **EMF metrics:** the worker writes CloudWatch Embedded Metric Format lines to stdout, and user
  data adds `--log-opt awslogs-format=json/emf` so CloudWatch Logs extracts them. No new worker
  IAM. Namespace `aegc`, dimensions `[Deployment, Pool]` and `[Deployment]` only, plus
  `[Deployment, Pool, Outcome]` on `TasksCompleted` (Outcome `ok` or `task_error`) and
  `TasksFailed` (`walltime`, `crashed`, `poison`, `payload_missing`, `refused`); also
  `ExecSeconds`, `QueueWaitSeconds` (from the task message's `submitted_at`),
  `BootToFirstClaimSeconds`, `Interruptions`, `Releases`, `PoisonTasks` and `WorkerExit` (#39).
- **Provenance in results:** each Result's `details` keeps what `execute_task` put there and
  gains `details["aegc"]`: the image digest, instance id and type, Availability Zone, Spot or
  on-demand, vCPUs and slots, attempt and receive count, timings, and `est_cost_usd` (this
  task's share of its instance at the static on-demand price, Spot's ceiling). The SDK sees it
  in `Client().get_task(task_id)["details"]["aegc"]`; Globus Flows' action output doesn't
  include it. User data passes the image to the worker as `AEGC_IMAGE`; result messages also
  carry `timings.queue_wait_s` (#40).
- **`aegc metrics <d> [--since 1h|1d|...] [--json]`:** per-pool task counts by outcome, average
  and maximum execution and queue-wait times, boot-to-first-claim, interruptions, releases,
  poison tasks, worker exits and the group's capacity, read with GetMetricData (already in the
  operator and deployer policies). `aegc deploy` turns on 1-minute Auto Scaling group metrics
  (EnableMetricsCollection, added to the deployer policy); with an older deployer policy it
  reports them as not enabled and carries on (#39).
- **`aegc provenance <d> <task_uuid> [--since 14d] [--json]`:** a task's `task_finished`
  records, one per attempt, from `/aegc/<d>` with FilterLogEvents, for clients that don't see
  `details` (such as Globus Flows). The operator policy gains `logs:FilterLogEvents` on that log
  group only (#40).
- **`aegc status` shows grants and size classes:** the deployment's data-access grants, and each
  pool's size class with the vCPUs and GiB one task gets (also in `--json`) (#69).
- **`aegc endpoint configure --allow-function ID`** (repeatable) writes `allowed_functions` into
  the endpoint's `config.yaml`, so Globus refuses other functions with `403
  FUNCTION_NOT_PERMITTED` once the endpoint restarts; `--allow-function none` removes the list,
  and without the option `configure` leaves it alone (#57).
- **`python` in `user_endpoint_config`** (`"3.13"`), for submissions that don't carry the
  submitter's Python, such as Globus Flows' (verified: Flows sends no `user_runtime`). When a
  submission does carry one, it wins. Rerun `aegc endpoint configure` (and restart the endpoint)
  to get the new schema and template (#55).
- Docs: a how-to for Globus Flows and Timers (#56), a how-to for logs, metrics and provenance,
  task isolation in the security model, the task role in the IAM and resources references,
  "Upgrade from v0.3", and the v0.4 live run on the performance page.

### Changed
- **Submissions without a Python version** (Globus Flows) to an env with images for several
  Python minors now fail with `this submission doesn't say its Python version (Globus Flows
  doesn't send one), and env '...' has worker images for 3.13, 3.14; add "python": "3.14" (your
  function's Python) to user_endpoint_config`, instead of silently going to one of its pools (#55).
- **`aegc deploy` reports role policies accurately:** `aegc-worker`, `aegc-task` and
  `aegc-grants` are compared with what the role has, so an unchanged redeploy reports `exists`
  (not `updated`), a first put `created`, and a dry run `would-create`, `would-update` or
  `would-delete`. Grant lines name their role, e.g. `aegc-grants (task role) (2 grant(s))` (#69).
- **Grants must fit next to `aegc-worker` and `aegc-task`**; the refusal now reads `... but the
  worker and task roles have room for N ...`.
- **Engine error texts** name task options instead of `resource_specification` (they now come from
  `user_endpoint_config`): `unsupported task options [...]`, `task option cpus must be a positive
  number`, `num_nodes, ranks_per_node and num_ranks must be numbers` (#69).
- **Worker user data changes** (the awslogs format and `AEGC_IMAGE`; for isolated pools also the
  container's user and capabilities, `/run/aegc` and the metadata firewall rule), and launch
  templates set `HttpProtocolIpv6=disabled`, so the next `aegc deploy` adds a launch template
  version to every pool. Running workers keep the old one until they leave; workers from images
  built before 0.4 log plain text and emit no metrics.

### Fixed
- **A task that finishes while its worker stops now posts its result.** A SIGTERM racing a claim
  left the task out of the stop's release; when it finished, the worker dropped its result as if
  released, and the message stayed hidden for the full visibility timeout (300 s). Anything
  still claimed when the worker exits now goes back to the queue at once. Found with
  `aegc provenance` in the v0.4 live run.
- **Isolated tasks get the region.** Without instance metadata, boto3 in a task raised
  `NoRegionError`; the agent now sets `AWS_REGION` and `AWS_DEFAULT_REGION` for task processes.
  Found in the v0.4 live run.

## [0.3.0] - 2026-11-15

Run your own work: worker images from any arm64 base, several Python versions per environment,
access to your own S3 data, results larger than Globus Compute's limit, and pools sized for
memory-heavy tasks. Validated live; see the [performance page](docs/reference/performance.md).

### Added
- **Bring your own worker image:** `aegc image build --env NAME --base REF` builds a worker image
  from any `linux/arm64` image with Python 3.11 or later and a shell, not only aarchsci and
  aarchbio (for example `python:3.13-slim@sha256:...`; `latest` and untagged references are
  refused). Bases in the `mambaorg/micromamba` layout use `templates/worker/Dockerfile`; others
  use the new `templates/worker/Dockerfile.pip`, which pip-installs `globus-compute-endpoint` and
  aegc with the base's packages as constraints, puts `aegc-worker` on `PATH`, and runs as uid
  57439. `--python PATH` names the base's Python. Builds are identified by the base's image id,
  so a moved tag builds anew. Local images and images in this account's ECR work as bases (#67).
- **`aegc image register --env NAME --image REF@sha256:...`** records an image you built yourself.
  It runs the image the way workers do, through its own entrypoint and user, and checks that it's
  arm64 and pinned, that `aegc-worker version` reports aegc 0.3 or later and uid 0 or 57439, and
  that `aegc-worker selftest` passes; then it copies the image into `aegc/<env>`. New
  `aegc-worker version` prints what it checks as JSON (#67).
- **Several Python minors per env:** `/aegc/images/<env>/records` keeps one record per Python
  minor, and a build or registration replaces only its own; `/aegc/images/<env>` keeps the newest
  in the 0.2 shape, so 0.2 CLIs keep working, and a record they write is merged in.
  `aegc image list --env NAME` shows an env's images and `aegc image forget --env NAME --python
  3.NN` drops one (the builder policy gains `ssm:DeleteParameter`). The first `aegc deploy --pool
  env` makes one pool per recorded minor (`<env>-py313`, `<env>-py314`), skipping images built
  before aegc 0.2, and notes that the options apply to each; a redeploy updates only the minors
  already deployed and notes new ones, and `python=3.NN` picks or adds one. Each client's tasks go
  to the pool for its Python (#58).
- **Size classes:** the pool option `size=small|medium|mem|large|whole` gives each task 1 vCPU and
  2 GiB, 2 and 8, 2 and 16, 8 and 32, or a whole instance. It names the pool
  `<env>-py<NNN>-<size>`, sets `MemoryGiBPerVCpu` for instance selection, raises the low end of
  `vcpu=` to fit a task, and sets the workers' slots from both vCPUs and memory, so it can't be
  combined with `slots=N`. `size=whole` needs one worker size (`vcpu=N`, 8 by default) and counts
  that many vCPUs per task. `size=` needs an image built with aegc 0.3 or later. Clients choose
  with `user_endpoint_config={"size": ...}`, or `{"cpus": N, "memory_gib": M}` for the cheapest
  pool that fits (a sized pool before a `whole` one on a tie); with `size` or `pool`, `cpus` and
  `memory_gib` are checked against the chosen pool. Unsized pools with fixed `slots=N` offer each
  task their smallest worker divided by N. `vcpus`, `memory` and similar get a "did you mean"
  hint. Pools without `size=` keep their name, launch template and user data. The size keys need
  an endpoint built and configured from this release. Workers record the host's vCPUs in the cost ledger,
  not their slot count (#18).
- **Worker data-access grants:** `aegc deploy <d> --grant read=s3://bucket/prefix --grant
  write=s3://bucket/prefix` (repeatable, at most 32) lets every task in the deployment read or
  write under your own S3 prefixes, through the worker role's inline policy `aegc-grants`. Read
  grants allow `GetObject`, `GetObjectVersion` and `ListBucket` for keys under the prefix, in any
  account; write grants allow `PutObject` and multipart uploads, only in buckets the deployment's
  account owns (`s3:ResourceAccount`). `s3://bucket/` (with the slash) means the whole bucket.
  Wildcards, invalid bucket names and aegc's own bucket are refused, and so are more grants than
  fit in IAM's 10,240 inline-policy characters. The grants are kept in the manifest and across
  redeploys; `--grant` replaces them and `--grant none` removes them. Requester-pays buckets need
  no extra IAM: pass `RequestPayer='requester'` (#53).
- **`aegc.outputs`**, for results larger than Globus Compute's ~10 MB limit: inside a task,
  `outputs.put(data, name=None, *, prefix=None)` uploads bytes, or a file given by path (`str` or
  `Path`; a missing file raises `FileNotFoundError`), to `<write grant prefix>/<task uuid>/<name>`
  (multipart for large files) and returns the `s3://` URI; `outputs.get`, `outputs.download` and
  `outputs.uri_to_https` (plain or presigned) read it back. It reads the deployment's current
  grants from the manifest (cached 60 s), so a redeploy's grants reach running workers; the worker
  policy may now read the manifest. Without a write grant it raises `PermissionError`, naming the
  `--grant` to add. Workers export `AEGC_TASK_UUID` to each task (#54).
- **Task options in `user_endpoint_config`:** the endpoint's schema and template now take `pool`,
  `size`, `cpus`, `memory_gib` and `walltime_s` next to `env`, and pass them to `AWSEngine` as
  defaults for the user endpoint process's tasks, e.g.
  `Executor(endpoint_id=..., user_endpoint_config={"env": "geospatial", "size": "mem"})`. Each
  distinct config gets its own user endpoint process. Rerun `aegc endpoint configure` (and restart
  the endpoint) to get the new schema.
- Docs: a how-to for bringing your own image and for several Python minors, a how-to for grants
  and `aegc.outputs`, size classes in the configuration guide and reference, the `aegc-grants`
  policy in the IAM reference, and a Mermaid diagram in the README.
### Fixed
- **`walltime_s` and `pool` in `resource_specification` never worked against the real Globus
  Compute service**, though 0.1 and 0.2 documented them there. The web service validates
  `resource_specification` as an MPI request: it refuses one without `num_nodes` (HTTP 422
  `REQUEST_MALFORMED`) and drops every key but `num_nodes`, `ranks_per_node`, `num_ranks` and
  `launcher_options`. aegc's options now go in `user_endpoint_config` (above). `AWSEngine` accepts
  the MPI keys when they ask for one instance (`num_nodes: 1`) and refuses more nodes or MPI
  ranks.
- `aegc image build --base` failed on the official `python:*` images: `Dockerfile.pip` linked
  `aegc-worker` onto itself when pip had installed it in `/usr/local/bin`.

## [0.2.0] - 2026-11-15

Spend safety: aegc spends real money, so 0.2 adds limits it enforces, a kill switch, a lease, a
cost view, and clear failures instead of silent waits. Validated live; see the
[performance page](docs/reference/performance.md).

Spend safety: deployments that bound and report what they cost, stop on request, and fail tasks
clearly instead of leaving them queued.

### Added
- **Deployment state:** `/aegc/<d>/state` holds `running`, `capped`, `stopped` or `expired`, with
  the reason and who set it. `deploy` creates it as `running`; a redeploy never changes it, and
  keeps group minimums at 0 while it isn't `running`. When it isn't `running`, engines refuse new
  tasks and stop scaling, and workers (which read it before their first claim) stop claiming, fail
  what they still receive, finish (or, with `kill`, fail) their running tasks, and leave their
  groups (#35, #36, #37).
- **Spend limits:** `aegc deploy` and `aegc limits` take `--max-usd-day`, `--max-usd-month`,
  `--max-instance-hours-day` (`none` removes one) and `--on-cap fail|kill`; they're kept in the
  manifest. Before each scale-out the engine's scaler holds 1.2 × the expected rate over the next
  hour, at any time of day, and admits only the whole workers that fit under every limit. When
  estimated spend reaches a limit the engine sets the state to `capped` and drops group minimums
  to 0. The cap ends at the end of its UTC day or month, when the engine restores the minimums, or
  with `aegc limits <d> --resume`, which refuses while spend is still over a limit (#35).
- **Clear, final failures:** tasks refused or failed because of the state come back as
  `AEGC_BUDGET_EXCEEDED`, `AEGC_STOPPED` or `AEGC_EXPIRED`, with what to do; they aren't retried.
  An engine removes only its own queued messages, in one sweep that receives each other message at
  most once. A pool with demand but no instance in service for `stall_fail_s` (1 h) fails the
  process's queued tasks with `AEGC_NO_CAPACITY` and the `aws service-quotas` command to request
  more Spot vCPUs. Tasks waiting with no worker because the limits admit none fail with
  `AEGC_BUDGET_EXCEEDED` after 10 minutes (#36).
- **`aegc stop` and `aegc resume`:** `stop` sets the state to `stopped` and every group's minimum
  to 0, so workers drain within about a minute; `stop --hard --yes` also fails running tasks and
  terminates any instance still in a group after 2 minutes. `resume` sets `running`, restores the
  minimums and pushes the lease forward (#37).
- **Cost ledger and `aegc cost`:** each worker writes `s3://<bucket>/ledger/<d>/<date>/<instance>.json`
  (type, AZ, Spot or on-demand, volume, boot and last-seen times) at start, every 5 minutes and at
  exit, so boot and image pull are counted; readers look back 4 days, and a record counts as
  running for 20 minutes after it was last written. `bootstrap` expires the records after 400 days. `aegc cost <d> [--since today|month|24h|7d]
  [--by pool|type] [--actual]` prices them with the same function the caps use; `--actual` adds
  Cost Explorer's figure for the `aegc:deployment` tag (#38).
- **Prices:** latest Spot prices from `DescribeSpotPriceHistory` (cached an hour), on-demand prices
  from the Price List API (cached a day), and a static us-west-2 table when neither is reachable
  (failed lookups are retried at most hourly).
  Every rate includes the root volume and the public IPv4 address (#35, #38).
- **Deployment lease and TTL:** `deploy --lease DURATION` (default `48h`, at least `3h`, `none`
  disables) creates EventBridge Scheduler one-shot schedules in a tagged group `aegc-<d>`, run by a
  tagged role `aegc-<d>-scheduler-<region>`, that set the state to `expired` and group minimums to
  0. Running endpoints renew the lease every min(1 h, lease / 3) while the deployment is running or
  capped, so a long cap doesn't turn into an expiry. `--ttl DURATION` adds a hard expiry that
  nothing renews; a TTL that has passed isn't scheduled again, and tasks refused after one are told
  it was the TTL (#42).
- **Pool options:** `walltime=` (the pool's default walltime), `max_walltime=` (the most a task may
  ask for) and `lifetime=` (1–72 h, default 24 h; a worker stops claiming after it, and the host
  watchdog terminates an instance older than lifetime + max walltime + 15 minutes; at the group's
  minimum both terminate for a replacement) (#45). `disk=GiB` (30–16384, default 30) and
  `throughput=MiBps` (125–1000, default 125; IOPS rise above 3,000 past 750 MiB/s, as gp3
  requires) (#33). `spot` undoes an earlier `on-demand` (#30).
- **ECR in-use tags:** `deploy` tags each image it pins `inuse-<d>-<digest12>` (with `PutImage`
  and the existing manifest; repositories stay immutable) and removes that deployment's older
  `inuse-` tags; `destroy` removes all of them. A priority-1 lifecycle rule keeps every image with
  an `inuse-` tag, so "keep the 10 most recent" never expires an image a deployment still uses. The
  lifecycle is re-applied to existing repositories on every build and deploy (#27).
- **`aegc destroy` verifies:** after deleting, `destroy <d> --yes` re-lists the deployment's
  instances, volumes and network interfaces (waiting while they terminate), groups, launch
  templates, security group, queues, log group, `/aegc/<d>/` parameters, worker role and instance
  profile, lease schedules and role, and in-use image tags. Anything left is reported as
  `remaining` and the command exits 1. `--no-verify` skips the check; `--verify` without `--yes`
  only re-checks (#43).
- **`aegc footprint <d>`:** what exists for a deployment, from the Resource Groups Tagging API and
  a name-prefix scan that also finds untagged look-alikes. It changes nothing (#43).
- **`aegc status` v2:** the state and why, estimated spend now, today and this month against the
  limits, lease and TTL expiry, and per pool the workers by instance type and lifecycle, their
  $/h, the oldest waiting task (CloudWatch) and the worst-case cost of running tasks. `--json`
  prints it as JSON (#41).
- **IAM:** a fifth policy, `bootstrap`. The worker policy reads the state and writes the ledger.
  The operator policy writes (and checks the tags of) the state, reads the ledger, prices and Cost
  Explorer, receives and deletes on the deployment's task queues (to remove its own tasks),
  updates the deployment's groups (to lower and restore their minimums) and renews the lease. The
  deployer policy has no ledger access, so `aegc cost`, `aegc limits --resume` and the spend in
  `aegc status` need operator credentials (#35, #38, #42).

### Changed
- **Pools are sized in vCPUs:** groups use `DesiredCapacityType=vcpu`, with `max_vcpus=` (default
  8) and `min_vcpus=` (default 0), so a pool's ceiling doesn't depend on which instance sizes
  `price-capacity-optimized` picks. The default worker size is `vcpu=2-8` (was 2-16), no worker may
  be larger than `max_vcpus`, and `deploy` narrows the default range to fit a small `max_vcpus`.
  The scaler targets demand in vCPUs. `max=` and `min=` still work: they count workers and are
  converted (× the smallest worker's vCPUs) with a warning. Pools deployed by v0.1 keep scaling in
  workers until redeployed, which converts them at their largest size (#29).
- **Redeploys keep what they aren't given:** options left out keep their last values, and pools
  left out are kept, with a warning, in the manifest and the lease. `--prune` is a dry run of their
  removal unless `--yes` is given too, and refuses before changing anything while they have queued
  or running tasks, unless `--force`. Limits, lease and TTL are kept too (#30).
- **Running endpoints pick up redeploys:** engines re-read the state every 30 s and the manifest
  every 60 s (by a content hash, `generation`), so redeploys and new limits apply within a minute
  without restarting the endpoint (#28).
- **Cheaper root volume:** 30 GiB at 125 MiB/s by default (was 50 GiB at 500 MiB/s), about
  $0.0033 per worker-hour instead of $0.026 (#33).
- **The deployer policy is split** into deployer (`deploy`, `destroy`, `stop`, `resume`, `reap`,
  `footprint`) and bootstrap (`bootstrap`, `unbootstrap`), because together they no longer fit the
  6,144-character limit for managed policies. The deployer gains the reads
  `destroy --verify` and `footprint` need, `EcrInUseTags`, and the lease's schedules (#27, #42, #43).
- **`deploy` refuses old images:** images built with aegc before 0.2, whose workers ignore the
  state, the lease and the limits and write no ledger, need `--allow-old-image`.
- `destroy` deletes every aegc-owned parameter under `/aegc/<d>/`, not only the manifest, and the
  lease's schedule group and role.
- The emergency stop is now `aegc stop <d> --hard --yes`, not `destroy --force --yes`.

- **Workers can leave vCPU-weighted groups.** Such a group refuses a decrement larger than its
  desired capacity, so with desired 1 vCPU a 2-vCPU worker couldn't leave. The worker now lowers
  desired by its own weight, then terminates without a decrement (worker policy:
  `ShrinkToLeave`). `aegc stop --hard` sets desired to 0 before terminating stragglers.
- **Tests run on substrate v0.127.0.** The m5 retargeting workaround is gone: Graviton groups,
  vCPU weighting, Spot quotas, the scheduler lease and launch-template metadata are all tested
  directly. Open gaps are strict xfails: substrate#1522 (DescribeNetworkInterfaces), #1523
  (GetResources coverage) and #1524 (ECR lifecycle preview). `make tools` now reinstalls when
  `SUBSTRATE_VERSION` changes.

### Fixed
- `destroy --verify` and `status` no longer fail on a queue `ListQueues` still lists for up to a
  minute after it's deleted.

## [0.1.0] - 2026-11-15

First release: Globus Compute tasks on ephemeral Spot Graviton workers, from an endpoint that runs
in Docker on your machine.

### Added
- **Project scaffold:** the `aegc` package and CLI stub, CI (ruff, mypy, pytest), the documentation
  site, and project tracking (#1).
- **Protocol v1:** JSON task and result messages for SQS. Bodies over 700 KB go to S3 with sha256
  verification. The worker runs only allow-listed calls, and walltime is capped below SQS's 12-hour claim
  limit (#6).
- **Ownership and bootstrap:** every resource is named `aegc-…` and tagged `aegc:managed`/
  `aegc:deployment` on its create call. `naming.is_owned()` requires both before any delete.
  `bootstrap()` creates the payload bucket (TLS-only, Block Public Access, 15-day expiry), the quay.io
  pull-through cache rule with a tagging creation template, and `/aegc/bootstrap`. It's idempotent and
  refuses look-alikes it didn't create. `unbootstrap()` is a dry run by default and refuses while
  deployments remain (#7).
- **Deploy and destroy:**
  - `infra.deployment.deploy()` creates a worker role and instance profile scoped to the
    deployment, a no-ingress security group, the log group, and for each pool a task queue with a
    DLQ, a launch template and a Spot Auto Scaling group. It finishes by writing the manifest.
  - Launch templates use the stock ECS AL2023 arm64 AMI (resolved at deploy time), IMDSv2 with hop
    limit 2 and gp3 at 500 MB/s. Auto Scaling groups use attribute-based Graviton selection with
    `price-capacity-optimized`, protect new instances from scale-in, and suspend `AZRebalance`.
  - Pool names are `<env>-py<NNN>`, up to 40 characters, so env names up to 34 characters work
    (#24).
  - Worker user data follows spike #5. It disables the ECS agent, starts Docker explicitly, pulls
    by digest and runs the worker under systemd. A watchdog terminates the instance, decrementing
    desired capacity.
  - Redeploy is idempotent, with a content hash on each launch template version.
  - `destroy()` removes only tagged resources, refuses while tasks are queued unless forced, and is
    a dry run by default. Every listing that decides ownership follows all pages (#26). Verified
    live: deploy, a no-op redeploy, then destroy left nothing (#8).
- **IAM policies:** `aegc iam-policy {worker,operator,deployer,builder}` prints least-privilege
  documents scoped to a deployment. Tests run them through the IAM policy simulator (allowed and
  denied cases, including tag conditions), and each fits the 6,144-character managed-policy limit.
  IAM Access Analyzer reports no findings (#9, #25).
- **Worker agent (`aegc-worker`):**
  - It claims tasks from the pool's queue with a long poll and renews their visibility in batches.
    Each task runs Globus Compute's `execute_task` in a fresh child process from a forkserver that
    preloads Globus Compute, so a task starts in about 30 ms.
  - The worker enforces walltime by killing the task's process group, because `GC_TASK_TIMEOUT`
    can't interrupt blocking calls (#2).
  - Each task posts `started`, then a result or a `system_failure`, and only then is acked.
  - Failure handling: crashes, poison tasks (delivered more than 4 times) and missing payloads are
    reported. Spot interruption and ASG termination release in-flight tasks immediately. A
    rebalance recommendation is only logged, since capacity rebalance replaces the instance. An
    idle worker leaves its group and decrements desired capacity, staying up if the group is at
    its minimum. Each claim and outcome is logged with its duration.
  - The host watchdog's heartbeat file is touched only while the claim loop is making progress, so
    a wedged worker is replaced instead of running (and billing) indefinitely.
  - `aegc-worker selftest` runs a probe task through `execute_task` (#10).
- **`AWSEngine`:** a Globus Compute endpoint engine.
  - `_submit` resolves the pool from `env` and the submitter's Python minor (or an explicit
    `pool`), and sends the task to SQS before returning.
  - Results come back on a result queue owned by each user endpoint process. The first result per
    task wins, a `system_failure` triggers the base class's retry, and orphaned results are
    written atomically to `unacked_results/` for the interchange to republish.
  - A scale-out-only scaler sizes each pool's Auto Scaling group from queue depth, with a stall
    circuit breaker.
  - `start` never raises: misconfiguration fails tasks with a clear message. The status report is
    cached and never raises, `shutdown` never touches AWS, and endpoint log handlers are shared
    with `aegc.*` (#12).
- **`aegc image build`:**
  - It layers `globus-compute-endpoint` (micromamba, from conda-forge) and the aegc wheel into an
    aarchsci or aarchbio environment. A guard fails the build if any existing package changes
    outside an allowlist (`click`).
  - The build runs the image's own smoke test and `aegc-worker selftest`, reads Python's minor
    version from the image, pushes to ECR `aegc/<env>` and records the pinned digest at
    `/aegc/images/<env>`. Tags are immutable and end in the aegc wheel's hash, so each code change
    gets its own tag and an unchanged rebuild reuses the pushed image.
  - The base comes through an ECR pull-through cache for quay.io, whose creation template runs as
    an aegc role, because ECR's default role can't tag the repositories it creates (#5).
  - Verified on `geospatial` 2026.11.13.154112: the guard allowed only `click` to change, the
    smoke test and selftest passed, and the image runs Python 3.14 (#11).
- **Endpoint:**
  - `aegc endpoint {build,login,configure,start,stop,logs,test,id}` runs a Globus Compute endpoint
    with `AWSEngine` in Docker.
  - A `.pth` hook (`aegc._register`, about 0.3 ms at interpreter start) makes `engine: type:
    AWSEngine` resolvable, because Globus Compute 4.x only looks engines up in its own module (#3).
  - The user config template passes the submitter's Python minor version and `env` (from
    `user_endpoint_config`) to the engine, with `amqp_port: 5671` (globus/globus-compute#2232) and
    `idle_heartbeats_soft: 0` (globus/globus-compute#2233). It's verified with Globus Compute's
    own renderer (#13).
  - The endpoint image runs Python 3.14 by default (`--python`), matching the aarchsci worker
    images; `start` waits for a first-time registration.
- **Operations CLI:**
  - `aegc bootstrap` / `unbootstrap`.
  - `aegc deploy <d> --pool env[:max=,vcpu=,mem=,slots=,idle=,on-demand]` builds pools from
    recorded images, so each pool's Python minor comes from its image.
  - `aegc destroy` (dry run unless `--yes`).
  - `aegc status`: per pool, the queued, running and dead-lettered tasks, the worker count, the last
    scaling activity, and results waiting for a stopped endpoint.
  - A vCPU quota preflight in `deploy` and `status`. It compares the account's Spot (or on-demand)
    vCPU quota and current usage with what the pools can ask for, and warns with the command to
    request more. A pool whose smallest worker can never fit is refused. New accounts get 5 Spot
    vCPUs.
  - `aegc reap`: removes aegc-owned result queues that are empty and idle for 14 days. Live engines
    refresh theirs daily (#14).
- **Documentation for v0.1:** a quickstart; concepts (architecture, when to use aegc and its
  limits, delivery and idempotency, the security model, cost with example prices); how-to guides
  (configuration, worker images, operations, troubleshooting); and reference pages for the CLI,
  endpoint configuration and `resource_specification` keys, limits, IAM policies, resources and
  tags, and the live performance results (#16). The cost page covers the Spot quota, which cost
  allocation tags to activate, and `aegc destroy <d> --force --yes` as the emergency stop.
- **Tests** run against [substrate](https://github.com/scttfrdmn/substrate) (pinned v0.126.0,
  `make tools`), an AWS emulator, instead of moto: real Auto Scaling, ECR pull-through
  cache, SQS, S3, IAM policy simulation, Service Quotas and default VPC. One workaround remains:
  substrate has no Graviton instance types, so tests that need running workers retarget the group
  at `m5` (substrate#1497). Strict xfails mark the open gaps (substrate#1471, #1497, #1498).

### Validated
Live in us-west-2 with the `geospatial` image (Python 3.14), recorded in #15 and #16:
- 35–46 s from zero workers to the first result; 100 tiny tasks in 13.2 s on 3 warm workers; 3 MB
  task and result payloads through S3 in 8 s.
- Exceptions come back with their tracebacks. A submitter on another Python version gets a clear
  error.
- A SIGKILLed user endpoint process recovers its in-flight result after restart. Terminating every
  worker mid-task releases the task, which reruns on a replacement.
- Pools return to zero about 5.5 minutes after the last task, and `destroy` leaves nothing.
- The quickstart works from scratch, from a fresh clone in zsh on macOS.

Bugs found and fixed during validation:
- a rebalance recommendation stranded an instance with no worker for 14 minutes;
- user endpoint processes idle-stopped with tasks still in flight after a recovery;
- image rebuilds collided with immutable tags;
- workers lost their `aegc:role=worker` tag;
- `destroy` could miss resources past the first page of a listing;
- the operator and deployer policies missed actions their commands use;
- env names over 14 characters couldn't be deployed.

[Unreleased]: https://github.com/scttfrdmn/aws-elastic-globus-compute/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/scttfrdmn/aws-elastic-globus-compute/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scttfrdmn/aws-elastic-globus-compute/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/scttfrdmn/aws-elastic-globus-compute/releases/tag/v0.1.0
