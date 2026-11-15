# aws-elastic-globus-compute

`AWSEngine` is a [Globus Compute](https://www.globus.org/compute) endpoint engine that runs tasks on
ephemeral AWS capacity. The package and CLI are called `aegc`.

Tasks go onto an SQS queue. Graviton EC2 workers pull them, run them inside arm64 science containers
on stock AMIs, and post the results back. When the queue is empty the workers terminate themselves,
and each Auto Scaling group returns to zero.

```
endpoint (laptop container) ─ AWSEngine ─▶ SQS tasks ◀── Graviton workers (Spot, Auto Scaling, scale to 0)
                                  ▲                          │ docker run aegc/<env>  →  execute_task
                                  └──── SQS results ◀────────┘   payloads > 700 KB via S3
```

- **Every connection is outbound.** The endpoint talks to Globus over AMQPS and to AWS APIs over
  HTTPS. Workers talk to AWS APIs. Nothing connects in to the laptop, and the workers' security group
  has no ingress rules.
- **Containers on stock AMIs.** Worker images are thin layers over the arm64 environments from
  [aarch.science](https://aarch.science/), with `globus-compute-endpoint` and the aegc worker added.
  Images are pinned by digest.
- **Managed with boto3.** No CloudFormation. Every resource is tagged when it's created, and aegc
  only deletes resources that carry its tags. Destructive commands are dry runs unless you pass
  `--yes`.

It replaces the HTEX-based setup from
[aws-elastic-parsl](https://github.com/scttfrdmn/aws-elastic-parsl). HTEX workers connect in to the
submit host, which needs tunnels or exposed ports.

!!! note "Status"
    Version 0.1.0, an early release; expect changes before later 0.x versions. Work is tracked in the repository's
    [issues](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues) and
    [milestones](https://github.com/scttfrdmn/aws-elastic-globus-compute/milestones).

## Where to start

- **[Quickstart](quickstart.md):** bootstrap a region, build one worker image, deploy a pool, and run
  tasks through a laptop endpoint, then tear it down.
- **[When to use aegc](concepts/when-to-use.md):** what it's good at, and the limits of v0.1.
- **[Architecture](concepts/architecture.md):** how a task travels from the SDK to a worker and back.

The site is organised into concepts (how it works and why), how-to guides (tasks you'll do), and
reference (exact commands, keys, limits, policies and resources).

!!! info "Tutorials (coming)"
    Worked, measured tutorials (Sentinel-2 composites, CMIP6 ensembles, Spot sweeps and others) are
    proposed in [#16](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/16) and will be
    added later.
