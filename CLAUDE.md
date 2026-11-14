# CLAUDE.md: aws-elastic-globus-compute

A Globus Compute endpoint engine (`AWSEngine`, package `aegc`) that runs Globus Compute tasks on
ephemeral AWS capacity, with no Parsl involved.

**Owner:** Scott Friedman (github.com/scttfrdmn). The goal is Globus Compute; Parsl is out of scope.
Globus identity: `scofri@globusid.org`.

## Tracking
All status lives in GitHub: issues, milestones "v0.N — Title", labels (`priority:*`, `area:*`,
`spike`) and the project board. Don't create plan, status or TODO files locally. Record decisions as
issue comments. The version stays 0.x.

## Architecture
```
endpoint container (laptop) → UEP → AWSEngine
   _submit:  SendMessage → SQS aegc-<d>-<pool>-tasks   (synchronous, before RabbitMQ acks)
   scaler:   SetDesiredCapacity → ASG aegc-<d>-<pool>  (scale-out only)
   poller:   SQS aegc-<d>-res-<uep12> → resolve futures; orphans → <run_dir>/unacked_results/
workers: ECS-optimized AL2023 arm64 (stock) → docker run aegc/<env>@digest aegc-worker run
   claim (visibility 300 s, heartbeat 100 s) → execute_task → result → delete → idle self-drain
   (TerminateInstanceInAutoScalingGroup, decrementing desired capacity)
payloads > ~700 KB → S3 aegc-<acct>-<region>/payloads/<d>/...
```

## Design decisions (why)
- **No Parsl or HTEX.** HTEX workers dial in to the submit host, which needs tunnels or exposed ports.
  An SQS queue makes every connection outbound.
- **boto3, not CloudFormation or CDK.** Every create and delete is an idempotent `ensure_*` /
  `destroy_*` function.
- **Ownership.** Tag every resource when it's created, via TagSpecifications on the create call:
  `aegc:managed=true`, `aegc:deployment`, `aegc:pool`, `aegc:role`, `aegc:version`. **Delete only
  when the name prefix and the tags both match. Never touch resources this project didn't create.**
  Destructive commands default to a dry run.
- **Containers on stock AMIs.** No custom AMIs. Workers run thin layers over
  `quay.io/aarchsci/<env>` or `quay.io/aarchbio/<tool>`, pulled by digest through ECR.
- **Python matching.** The SDK client and the worker image must have the same Python minor version.
  The image is selected per submitter; a mismatch fails the task with a clear message.
- **Engine robustness.** The UEP is idle-stopped and restarted, so:
  - `start()` is fast and never raises; it never provisions IAM;
  - `shutdown()` never touches AWS;
  - `get_status_report()` never raises and only reads cached data.
- **Engine registration.** Globus Compute 4.x only resolves `engine: type:` from
  `globus_compute_endpoint.engines`, so a `.pth` hook registers `AWSEngine`.
- **AMQPS on 5671.** Port 443 suffered cut connections and a reconnect race (globus/globus-compute#2232).
- **SQS limits.** A claim can't be extended past 12 hours, so v0.1 caps walltime at 11 hours. Messages
  max out at 1 MiB, so larger payloads go to S3.

## Environment
- `AWS_PROFILE=aws`, account 942542972736, region us-west-2. The account holds unrelated workloads,
  for example aws-elastic-gcs DTNs.
- Dev venv: `uv venv --python 3.13 .venv && uv pip install -e '.[engine,cli,dev]'`, then `make tools`.
- **Tests use the user's substrate AWS emulator, not moto** (pytest-substrate; pinned binary in `.tools/`).
  If substrate lacks a capability, file an issue on scttfrdmn/substrate and shim it in
  `tests/substrate_gaps.py` with the issue number. Remove the shim when it lands.
- Checks: `ruff check . && ruff format --check . && mypy && pytest`. Live tests need `-m live` and
  `AEGC_LIVE=1`.
- Reuse from aws-elastic-parsl: `globus-compute/{Dockerfile,gce.sh,user_config_template.yaml.j2}` and
  `src/aws_elastic_parsl/gc_smoke.py`. Patterns to port, not import: spore-host `spawn/pkg/taskpool`
  and `calque/internal/pool`.
