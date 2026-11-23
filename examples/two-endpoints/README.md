# One flow, two endpoints

The code for the tutorial [One flow, two endpoints](../../docs/tutorials/two-endpoints.md)
([site](https://scttfrdmn.github.io/aws-elastic-globus-compute/tutorials/two-endpoints/)).

| File | What it is |
|---|---|
| `make_data.py` | Make the sensor CSVs that you upload with `aws s3 cp` |
| `steps.py` | The steps: `make_manifest` (laptop) and `summarize` (aegc) |
| `register_steps.py` | Register both as source text; prints their function ids |
| `two_endpoints.json` | The two-step flow |
| `input.json` | The run's input: fill in the ids and your bucket |
