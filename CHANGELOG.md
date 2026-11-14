# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x until further notice).

## [Unreleased]

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
  - Worker user data follows spike #5. It disables the ECS agent, starts Docker explicitly, pulls
    by digest and runs the worker under systemd. A watchdog terminates the instance, decrementing
    desired capacity.
  - Redeploy is idempotent, with a content hash on each launch template version.
  - `destroy()` removes only tagged resources, refuses while tasks are queued unless forced, and is
    a dry run by default. Verified live: deploy, a no-op redeploy, then destroy left nothing (#8).
- **IAM policies:** `aegc iam-policy {worker,operator,deployer}` prints least-privilege documents
  scoped to a deployment. Tests run them through the IAM policy simulator (allowed and denied cases,
  including tag conditions). IAM Access Analyzer reports no findings (#9).
- **Worker agent (`aegc-worker`):**
  - It claims tasks from the pool's queue with a long poll and renews their visibility in batches.
    Each task runs Globus Compute's `execute_task` in a fresh child process from a forkserver that
    preloads Globus Compute, so a task starts in about 30 ms.
  - The worker enforces walltime by killing the task's process group, because `GC_TASK_TIMEOUT`
    can't interrupt blocking calls (#2).
  - Each task posts `started`, then a result or a `system_failure`, and only then is acked.
  - Failure handling: crashes, poison tasks (delivered more than 4 times) and missing payloads are
    reported. Spot interruption and ASG termination release in-flight tasks immediately. A
    rebalance recommendation drains. An idle worker leaves its group and decrements desired
    capacity, staying up if the group is at its minimum.
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
    version from the image, pushes to ECR `aegc/<env>` (immutable tags, scan on push, lifecycle),
    and records the pinned digest at `/aegc/images/<env>`.
  - Verified on `geospatial` 2026.11.13.154112: the guard allowed only `click` to change, the
    smoke test and selftest passed, and the image runs Python 3.14 (#11).
- **Endpoint:**
  - `aegc endpoint {build,login,configure,start,stop,logs,test,id}` runs a Globus Compute endpoint
    with `AWSEngine` in Docker.
  - A `.pth` hook (`aegc._register`, about 0.3 ms at interpreter start) makes `engine: type:
    AWSEngine` resolvable, because Globus Compute 4.x only looks engines up in its own module (#3).
  - The user config template passes the submitter's Python minor version and `env` (from
    `user_endpoint_config`) to the engine, with `amqp_port: 5671`. It's verified with Globus
    Compute's own renderer (#13).

### Fixed
- **The pull-through cache works with tagged repositories:** the creation template now runs as an aegc
  role (`/aegc/aegc-ecr-template-<region>`: `ecr:CreateRepository` and `ecr:TagResource` on
  `aegc-quay/*`, trusted by ECR for this account only). ECR's default role is denied
  `ecr:TagResource`, which made cache pulls fail with only "repository not found" (#5).

### Changed
- **Tests use [substrate](https://github.com/scttfrdmn/substrate)** (pinned v0.124.0, `make tools`)
  instead of moto. Missing capabilities are filed upstream (substrate#1445, #1460–#1465) and shimmed
  in `tests/substrate_gaps.py` until they land.
