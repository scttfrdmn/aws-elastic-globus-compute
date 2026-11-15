# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x until further notice).

## [Unreleased]

### Added
- **ECR in-use tags:** `deploy` tags each image it pins `inuse-<deployment>-<digest12>` (adding a
  tag with `PutImage` and the existing manifest; the repository stays immutable), and removes that
  deployment's older `inuse-` tags. `destroy` removes all of them. A new priority-1 lifecycle rule
  keeps every image with an `inuse-` tag, so an older digest a deployment still uses is never
  expired by "keep the 10 most recent". The lifecycle is re-applied to existing repositories on
  every build and deploy. The deployer policy gains `EcrInUseTags` on `aegc/*` (#27).
- **Root volume options:** pools take `disk=GiB` (30–16384) and `throughput=MiBps` (125–1000; IOPS
  rise above 3,000 past 750 MiB/s, as gp3 requires) (#33).
- **`destroy` verification:** after deleting, `aegc destroy <d> --yes` re-lists the deployment's
  instances, volumes and network interfaces (waiting while they terminate), groups, launch
  templates, security group, queues, log group, `/aegc/<d>/` parameters, worker role, instance
  profile and in-use image tags, reports anything left as `remaining`, and exits 1.
  `--no-verify` skips it; `--verify` without `--yes` only re-checks (#43).
- **`aegc footprint <d>`:** a report of what exists for a deployment, from the Resource Groups
  Tagging API plus a name-prefix scan (queues, groups, launch templates, parameters) that also
  finds untagged look-alikes. It changes nothing (#43).

### Changed
- **Cheaper root volume:** 30 GiB at 125 MiB/s by default (was 50 GiB at 500 MiB/s), about
  $0.0033 per worker-hour instead of $0.026 (#33).
- `destroy` deletes every aegc-owned parameter under `/aegc/<d>/`, not only the manifest.

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

[Unreleased]: https://github.com/scttfrdmn/aws-elastic-globus-compute/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/scttfrdmn/aws-elastic-globus-compute/releases/tag/v0.1.0
