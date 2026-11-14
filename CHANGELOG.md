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

### Fixed
- **The pull-through cache works with tagged repositories:** the creation template now runs as an aegc
  role (`/aegc/aegc-ecr-template-<region>`: `ecr:CreateRepository` and `ecr:TagResource` on
  `aegc-quay/*`, trusted by ECR for this account only). ECR's default role is denied
  `ecr:TagResource`, which made cache pulls fail with only "repository not found" (#5).

### Changed
- **Tests use [substrate](https://github.com/scttfrdmn/substrate)** (pinned v0.124.0, `make tools`)
  instead of moto. Missing capabilities are filed upstream (substrate#1445, #1460–#1465) and shimmed
  in `tests/substrate_gaps.py` until they land.
