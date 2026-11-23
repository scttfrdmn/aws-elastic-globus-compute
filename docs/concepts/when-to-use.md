# When to use aegc

aegc suits bursts of independent Python functions, each running for seconds to hours, submitted
from a laptop or notebook through Globus Compute. You pay for workers only while tasks run, and
nothing needs an inbound port or a cluster to look after.

## Good fits

- **Fan-out from a laptop.** Submit hundreds or thousands of functions; a pool scales out to its
  maximum, then back to zero about 5 minutes after the work stops.
- **Compute next to data.** Workers run in the AWS region, so they can read public datasets there
  (for example Sentinel-2 COGs in us-west-2) without moving them, unsigned, and your own or
  requester-pays buckets through [grants](../how-to/data.md).
- **Pinned, reproducible environments.** Every worker runs an image pinned by digest: a dated
  [aarchsci](https://aarch.science/) or [aarchbio](https://aarch.bio/) release, or any arm64 image
  of your own ([Worker images](../how-to/images.md)).
- **Mixed Globus Compute setups.** The same function can go to a campus endpoint or to an aegc
  endpoint; only the endpoint id changes.
- **Spot pricing.** Pools use Spot by default. An interrupted task is released at once and rerun
  on another worker.
- **Short, bursty tasks without the boot wait.** A [Lambda pool](../how-to/lambda-pools.md) runs
  the same image as a Lambda function: the first result came back in 3.5 s from idle in the live
  run, and nothing runs, or costs, between bursts. Tasks get at most 30 minutes, cost about 4 to
  5 times Spot per vCPU-hour while they run, and aren't isolated.

## Limits and weak spots

These are the current limits. Numbers come from the live run in
[Performance](../reference/performance.md); the full table is in [Limits](../reference/limits.md).

- **Per-task overhead.** 100 tiny tasks took 13.2 s on 3 warm workers. Every task makes several
  SQS round trips and starts a fresh process. Batch work so each task runs for at least about 10 s.
- **Cold start.** From zero workers, the first result took 39 s (46 s on a second cold run). Larger
  images take longer to pull. Set `min_vcpus=` on a pool to keep workers warm, at the cost of paying for
  them, or send short tasks to a [Lambda pool](../how-to/lambda-pools.md) (3.5 s to the first
  result; about 5 s of init per execution environment).
- **The endpoint runs somewhere you keep running.** By default it's a container on your laptop,
  and tasks reach AWS only while it's running. Results produced while it's down wait in SQS for up
  to 14 days and are delivered when it comes back. To take tasks with the laptop closed, for
  Globus Flows and Timers for example, [run it in the cloud](../how-to/cloud-endpoint.md) on a
  small instance (about $12 a month, outside `aegc cost` and the spend limits).
- **Python must match.** The submitter's Python minor version must match a worker image. The
  aarchsci images run 3.14; for other minors, build or register an image per minor (an env can
  have several). A client on a minor with no image gets an error, not a result.
- **Walltime is capped at 41,400 s (11.5 h)**; the default is 39,600 s (11 h). SQS can't hold a
  claim past 12 hours. Longer tasks are tracked in
  [#19](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/19). On a Lambda pool,
  tasks get at most the function's timeout less 20 s, 1,780 s at most.
- **Payload size.** Globus Compute itself limits task and result sizes (about 10 MB). aegc's S3
  offload only covers the hop between the endpoint and the workers. Write large outputs to S3 with
  [`aegc.outputs`](../how-to/data.md#return-large-outputs) and return the URI.
- **Tasks reach only what you grant.** Tasks run with the deployment's task role, which holds
  nothing but your grants (and a read of the manifest); the worker role, for aegc's queues,
  payloads, images and logs, stays with the worker agent (on images built with aegc 0.4 or
  later). Open data is read with **unsigned** requests; your own buckets, requester-pays data and
  output buckets need a `--grant` per prefix (at most 32, and writes only to your own account's
  buckets). Grants are shared by every task in the deployment;
  see [Security model](security.md#data-access).
- **Images must be arm64 with Python 3.11 or later, and have a shell.** Any such image works as a
  base, or as a registered image with aegc 0.3 or later in it; see
  [Worker images](../how-to/images.md).
- **Spend limits are estimates.** They count instance time, volumes and IPv4 addresses from the
  workers' own ledger, and Lambda pools' time from Lambda's metrics, are checked only while an
  endpoint runs, and can be overshot by a few
  minutes of spend plus running tasks. See [Spend limits](cost.md#spend-limits).
- **No GPUs, no MPI, arm64 only.** Pools are Graviton CPU instances, or arm64 Lambda functions.
  Software that only ships for
  x86-64 won't run.
- **Spot vCPU quota.** A pool can't grow past the account's Spot quota for standard instances. When
  it's exhausted the group stays below its desired capacity; `aegc status` shows the scaling error.
- **Fixed size classes.** Tasks get 1 vCPU and 2 GiB by default, or one of five
  [size classes](../how-to/configuration.md#size-classes) up to 8 vCPUs and 32 GiB, or a whole
  instance. Each class is its own pool, and `user_endpoint_config` picks one by `size`, or by
  `cpus` and `memory_gib`, for every task of an executor; use one executor per size. There's no per-task memory enforcement: tasks on a worker share it.
- **At-least-once delivery.** A task can run more than once (after a lost worker or a Spot
  interruption, for example). Write functions so a rerun is harmless. See
  [Delivery and idempotency](delivery.md).
- **No checkpointing.** An interrupted task starts again from the beginning.
- **One trust domain per deployment.** Every task in a deployment runs with the same task role and
  its grants, and tasks on a worker share a uid, so they can see each other's processes and
  files. Give separate groups separate deployments. See
  [Task isolation](security.md#task-isolation).
- **Lambda pools aren't isolated.** Their tasks run with the pool's function role, which can read
  every task payload of the deployment and post results for any of its tasks. Keep untrusted
  functions on isolated EC2 pools, in a deployment without Lambda pools
  ([#80](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/80)). See
  [Lambda pools](security.md#lambda-pools).
- **Not high-assurance.** Task data goes to AWS; `AWSEngine` refuses high-assurance endpoints.
- **Default VPC.** `aegc deploy` places workers in the region's default VPC and default subnets.
  Only us-west-2 has been tested live; see
  [Other regions](../how-to/operations.md#other-regions).

## When to use something else

- **Sub-second interactive calls:** the per-task overhead dominates, even on a
  [Lambda pool](../how-to/lambda-pools.md): every task still makes several SQS round trips and
  starts a fresh process.
- **GPU, MPI or tightly coupled jobs:** use an HPC cluster's Globus Compute endpoint, or AWS
  ParallelCluster or Batch.
- **Tasks longer than 11.5 hours:** split them, or wait for #19.
