# When to use aegc

aegc suits bursts of independent Python functions, each running for seconds to hours, submitted
from a laptop or notebook through Globus Compute. You pay for workers only while tasks run, and
nothing needs an inbound port or a cluster to look after.

## Good fits

- **Fan-out from a laptop.** Submit hundreds or thousands of functions; a pool scales out to its
  maximum, then back to zero about 5 minutes after the work stops.
- **Compute next to open data.** Workers run in the AWS region, so they can read public datasets
  there (for example Sentinel-2 COGs in us-west-2) without moving them, as long as the reads are
  unsigned (see below).
- **Pinned, reproducible environments.** Every worker runs a science image pinned by digest, built
  over a dated [aarchsci](https://aarch.science/) release.
- **Mixed Globus Compute setups.** The same function can go to a campus endpoint or to an aegc
  endpoint; only the endpoint id changes.
- **Spot pricing.** Pools use Spot by default. An interrupted task is released at once and rerun
  on another worker.

## Limits and weak spots

These are the honest limits of v0.1. Numbers come from the live run in
[Performance](../reference/performance.md); the full table is in [Limits](../reference/limits.md).

- **Per-task overhead.** 100 tiny tasks took 13.2 s on 3 warm workers. Every task makes several
  SQS round trips and starts a fresh process. Batch work so each task runs for at least about 10 s.
- **Cold start.** From zero workers, the first result took 39 s (46 s on a second cold run). Larger
  images take longer to pull. Set `min=` on a pool to keep workers warm, at the cost of paying for
  them.
- **The endpoint runs on your machine.** In v0.1 the endpoint is a container on your laptop. Tasks
  reach AWS only while it's running. Results produced while it's down wait in SQS for up to 14 days
  and are delivered when it comes back. A cloud-hosted endpoint is tracked in
  [#22](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/22).
- **Python 3.14 only, for now.** The submitter's Python minor version must match a worker image, and
  the aarchsci images run 3.14. Each env has one recorded image, so one Python version, at a time.
  A 3.13 client gets an error, not a result.
- **Walltime is capped at 41,400 s (11.5 h)**; the default is 39,600 s (11 h). SQS can't hold a
  claim past 12 hours. Longer tasks are tracked in
  [#19](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/19).
- **Payload size.** Globus Compute itself limits task and result sizes (about 10 MB). aegc's S3
  offload only covers the hop between the endpoint and the workers. Return references to large
  outputs, not the outputs.
- **Workers can reach only aegc's own resources.** The worker role allows aegc's queues, its
  payload prefix, its image repositories and its log group, and nothing else. Reading open data
  works only with **unsigned** requests: a request signed with the worker's credentials is denied,
  because the role has no S3 read permission outside the payload prefix. There is no way in v0.1 to
  grant access to requester-pays data, private buckets, or an output bucket. This is a real
  limitation; see [Security model](security.md#data-access).
- **No GPUs, no MPI, arm64 only.** Pools are Graviton CPU instances. Software that only ships for
  x86-64 won't run.
- **Spot vCPU quota.** A pool can't grow past the account's Spot quota for standard instances. When
  it's exhausted the group stays below its desired capacity; `aegc status` shows the scaling error.
- **Simple pools.** One pool per env in a deployment, chosen by env and Python version.
  `resource_specification` accepts only `env`, `pool` and `walltime_s`; there are no size classes
  yet ([#18](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/18)).
- **At-least-once delivery.** A task can run more than once (after a lost worker or a Spot
  interruption, for example). Write functions so a rerun is harmless. See
  [Delivery and idempotency](delivery.md).
- **No checkpointing.** An interrupted task starts again from the beginning.
- **One trust domain per deployment.** Every task in a deployment runs with the same worker role.
  Give separate groups separate deployments.
- **Not high-assurance.** Task data goes to AWS; `AWSEngine` refuses high-assurance endpoints.
- **Default VPC.** `aegc deploy` places workers in the region's default VPC and default subnets.
  Only us-west-2 has been tested live.

## When to use something else

- **Sub-second interactive calls:** the per-task overhead dominates. A Lambda backend for short tasks
  is proposed in [#21](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/21).
- **GPU, MPI or tightly coupled jobs:** use an HPC cluster's Globus Compute endpoint, or AWS
  ParallelCluster or Batch.
- **Private or requester-pays data in S3:** wait for data-access grants, or copy the data out first.
- **Tasks longer than 11.5 hours:** split them, or wait for #19.
