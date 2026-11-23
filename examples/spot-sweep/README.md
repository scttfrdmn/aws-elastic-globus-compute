# A Spot sweep that survives interruptions

The code for the tutorial [A Spot sweep that survives interruptions](../../docs/tutorials/spot-sweep.md)
([site](https://scttfrdmn.github.io/aws-elastic-globus-compute/tutorials/spot-sweep/)).

| File | What it is |
|---|---|
| `sweep_tasks.py` | The task, `collatz_chunk(index, size)`: CPU-bound and deterministic |
| `run_sweep.py` | Submit the 200 tasks, check the answers, start FIS mid-run |
| `fis.py` | Create, start and clean up the FIS role and experiment template (boto3) |

`tests/unit/test_examples.py` checks the experiment template that `fis.py` builds, without AWS.
