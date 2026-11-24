# AWS HealthOmics as a flow step

The code for [Run AWS HealthOmics from a flow](../../docs/how-to/healthomics.md).

| File | What it is |
|---|---|
| `omics_flow.json` | The flow: start a run (`aegc.omics.start_run`), wait 5 min, check it (`aegc.omics.run_status`), loop until it's done |
| `input.json` | A template for the flow's input |
| `main.wdl` | A small private workflow for trying it: count one sample's reads in a region |
