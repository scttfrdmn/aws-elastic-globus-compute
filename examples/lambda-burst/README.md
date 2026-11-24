# A burst of short tasks: Lambda against Spot EC2

The synthetic baseline in [Performance](../../docs/reference/performance.md#lambda-against-spot-ec2-a-burst-of-short-tasks-2026-11-24):
500 tasks of 1–5 s of CPU work, sent to one pool at a time.

| File | What it is |
|---|---|
| `burst_tasks.py` | The task, `crunch(index, seconds)` |
| `run_burst.py` | Send the burst to `--pool`, time the first and last results |
