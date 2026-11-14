# aws-elastic-globus-compute

`AWSEngine` is a [Globus Compute](https://www.globus.org/compute) endpoint engine that runs tasks on
ephemeral AWS capacity. Tasks go onto an SQS queue. Graviton EC2 workers, running arm64 science
containers on stock AMIs, pull them, run them, and post the results back. When the queue is empty, the
workers drain themselves and the Auto Scaling group returns to zero.

It replaces the HTEX-based setup from
[aws-elastic-parsl](https://github.com/scttfrdmn/aws-elastic-parsl). HTEX workers must connect in to
the endpoint, which needs tunnels or exposed ports. Here every connection is outbound.

!!! note "Status"
    Pre-release (0.x), being built toward v0.1. See the
    [milestones](https://github.com/scttfrdmn/aws-elastic-globus-compute/milestones) for progress.

See [Architecture](architecture.md) for how the pieces fit together.
