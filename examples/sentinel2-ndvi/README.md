# Sentinel-2 NDVI, from a notebook to nightly

The code for the tutorial
[Sentinel-2 NDVI, from a notebook to nightly](../../docs/tutorials/sentinel2-ndvi.md)
([site](https://scttfrdmn.github.io/aws-elastic-globus-compute/tutorials/sentinel2-ndvi/)).

| File | What it is |
|---|---|
| `ndvi_tasks.py` | The task, `ndvi_latest(bbox, days, max_cloud)` |
| `run_ndvi.py` | Part 1: submit it with the SDK and fetch the GeoTIFF |
| `register_ndvi.py` | Part 2: register it as source text for Globus Flows |
| `nightly.json` | Part 2: the one-step flow (fill in `ENDPOINT_ID` and `FUNCTION_ID`) |
