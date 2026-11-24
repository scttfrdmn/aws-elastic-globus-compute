# Genotype a gene across 1000 Genomes: a flow on Lambda and Spot

The code for the tutorial [Genotype a gene across 1000 Genomes](../../docs/tutorials/cohort-genotype.md)
([site](https://scttfrdmn.github.io/aws-elastic-globus-compute/tutorials/cohort-genotype/)). It uses
the `genomics` image from [genomics-slice](../genomics-slice/).

| File | What it is |
|---|---|
| `cohort_tasks.py` | The three steps: `plan`, `genotype` (Lambda pool) and `merge` (Spot pool) |
| `run_cohort.py` | Run the steps from the SDK, without a flow; `--limit N` for the first N samples |
| `register_cohort.py` | Register the three functions for Globus Flows |
| `cohort.json` | The flow: plan, genotype every shard, merge |
| `input.json` | A template for the flow's input |
