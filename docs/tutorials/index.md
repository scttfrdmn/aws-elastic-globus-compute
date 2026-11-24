# Tutorials

Each tutorial builds something real on aegc and measures what it gained: bytes not moved,
dollars per run, time, or work that survived an interruption. They pick up where the
[quickstart](../quickstart.md) leaves off, and each one stands alone.

| Tutorial | You'll build | What it measures | Region |
|---|---|---|---|
| [Sentinel-2 NDVI, from a notebook to nightly](sentinel2-ndvi.md) | NDVI for a field-sized area from the latest clear scene, from the SDK, then nightly with Globus Flows and Timers on a cloud-hosted endpoint | Bytes read vs the full scene, $/run, scale to zero | us-west-2 |
| [Slice before you ship: 1000 Genomes](genomics-slice.md) | A 1 Mb slice of the 27.9 GB chr22 VCF with allele frequencies, using your own bcftools image | Bytes shipped, egress avoided, wall time, $/run | us-east-1 |
| [A Spot sweep that survives interruptions](spot-sweep.md) | A 200-task parameter sweep, interrupted mid-run by AWS Fault Injection Service | Tasks completed and correct, interruptions and releases, time added, Spot vs on-demand | us-west-2 |
| [One flow, two endpoints](two-endpoints.md) | A Globus flow whose first step runs on a laptop endpoint and second on aegc, next to the data in S3 | Laptop vs cloud time, $/run | us-west-2 |
| [Genotype a gene across 1000 Genomes](cohort-genotype.md) | A Globus flow that genotypes APOL1 in all 2,504 high-coverage samples on a Lambda pool, then merges on Spot | Bytes read vs 44 TB, time per step, $/run, Lambda vs Spot for the fan-out | us-east-1 |

**What you need for all of them:** the quickstart done once (a bootstrapped region, the endpoint
image and your Globus login), a clone of this repository with its `.venv` on Python 3.14, and a
bucket of your own for outputs. The code is in [examples/](../../examples/), one directory per
tutorial: the task functions in a module, and a script that runs them.

**The numbers.** Each page has a **Results** box with the figures from a live run. The first four
were run on 2026-11-23, the cohort tutorial on 2026-11-24. Your numbers will differ with the
day's Spot prices, the instance types Spot provides, and your network.

!!! warning "Cost and teardown"
    The tutorials create AWS resources that cost money while they run, usually cents, and each
    lists its cost up front. Each deploys with `--max-usd-day` and a `--ttl`, so a forgotten
    deployment stops itself. Workers leave about 5 minutes after their last task, but the
    deployment, a cloud endpoint, your outputs and the worker images stay until you remove them,
    so finish each tutorial with its **Teardown**: `aegc destroy <d> --yes`, your test objects,
    and anything else the tutorial made (a FIS template, a flow, a timer).
