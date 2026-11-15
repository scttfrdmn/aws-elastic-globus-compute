# aws-elastic-globus-compute

A [Globus Compute](https://www.globus.org/compute) endpoint engine, `AWSEngine`, that runs tasks on
ephemeral AWS capacity: an SQS work queue, and Graviton EC2 workers that run arm64 science containers
on stock AMIs. Capacity scales to zero when there's no work. No Parsl, no tunnels, no inbound ports,
no CloudFormation.

```
endpoint (laptop container) ─ AWSEngine ─▶ SQS tasks ◀── Graviton workers (Spot, Auto Scaling, scale to 0)
                                  ▲                          │ docker run aegc/<env>  →  execute_task
                                  └──── SQS results ◀────────┘   payloads > 700 KB via S3
```

- **Every connection is outbound.** The endpoint and the workers both talk to AWS APIs; nothing dials
  in to the laptop.
- **Containers on stock AMIs.** Worker images are thin layers over the signed arm64 images from
  [aarch.science](https://aarch.science/) and [aarch.bio](https://aarch.bio/), with
  `globus-compute-endpoint` added. Each image is selected to match the submitter's Python version.
- **Managed with boto3.** Every resource is tagged when it's created, and the tool only ever deletes
  resources that carry its tags.

## Documentation

<https://scttfrdmn.github.io/aws-elastic-globus-compute/>. Start with the
[quickstart](https://scttfrdmn.github.io/aws-elastic-globus-compute/quickstart/).

## Status

Early release (0.x): v0.2.0 adds spend limits, stop and resume, a deployment lease and a cost
ledger to v0.1.0. Work is tracked in this repo's issues, milestones and project board.

Sister projects:
- [aws-elastic-gcs](https://github.com/scttfrdmn/aws-elastic-gcs): elastic Globus Connect Server.
- [aws-elastic-parsl](https://github.com/scttfrdmn/aws-elastic-parsl): fixes for Parsl's
  `AWSProvider`, and the HTEX-based Globus Compute setup this project replaces.

## License

Apache-2.0
