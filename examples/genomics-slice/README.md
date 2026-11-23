# Slice before you ship: 1000 Genomes

The code for the tutorial [Slice before you ship](../../docs/tutorials/genomics-slice.md)
([site](https://scttfrdmn.github.io/aws-elastic-globus-compute/tutorials/genomics-slice/)).

| File | What it is |
|---|---|
| `Dockerfile` | The base image: `python:3.14-slim` with bcftools and tabix |
| `slice_tasks.py` | The task, `slice_vcf(region, url)` |
| `run_slice.py` | Submit it with the SDK, report the bytes shipped, fetch the results |
