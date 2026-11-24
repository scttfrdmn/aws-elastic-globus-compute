"""Send a burst of short tasks to one aegc pool and time it.

    python examples/lambda-burst/run_burst.py --endpoint <id> --pool plain-py314-lambda
    python examples/lambda-burst/run_burst.py --endpoint <id> --pool plain-py314

Each task does 1-5 s of CPU work (the same sequence of durations every run, from --seed). Prints
when the first result came back, when the last did, and where the tasks ran; --save writes the
summary as JSON. Run it with Python 3.14, the image's Python.
"""

import argparse
import collections
import json
import random
import sys
import time
from concurrent.futures import as_completed
from pathlib import Path

from globus_compute_sdk import Executor
from globus_compute_sdk.serialize import ComputeSerializer, PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from burst_tasks import crunch


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", required=True, help="the aegc endpoint id")
    p.add_argument("--pool", required=True, help="e.g. plain-py314-lambda or plain-py314")
    p.add_argument("--tasks", type=int, default=500)
    p.add_argument("--min-s", type=float, default=1.0)
    p.add_argument("--max-s", type=float, default=5.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--save", help="write the summary to this JSON file")
    args = p.parse_args()

    rng = random.Random(args.seed)
    durations = [round(rng.uniform(args.min_s, args.max_s), 2) for _ in range(args.tasks)]
    serializer = ComputeSerializer(strategy_code=PureSourceTextInspect())
    started = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"{args.tasks} tasks ({sum(durations):.0f} s of work) to {args.pool} at {started}")
    t0 = time.monotonic()
    first = None
    where: collections.Counter[str] = collections.Counter()
    seen = set()
    with Executor(
        endpoint_id=args.endpoint,
        amqp_port=5671,
        user_endpoint_config={"pool": args.pool},
        serializer=serializer,
    ) as ex:
        futures = [ex.submit(crunch, i, s) for i, s in enumerate(durations)]
        for fut in as_completed(futures):
            r = fut.result()
            if first is None:
                first = time.monotonic() - t0
            seen.add(r["index"])
            where[r["where"]] += 1
            if len(seen) % 100 == 0:
                print(f"  {len(seen)}/{args.tasks} at {time.monotonic() - t0:.0f} s", flush=True)
    total = time.monotonic() - t0
    summary = {
        "pool": args.pool,
        "tasks": args.tasks,
        "work_s": round(sum(durations), 1),
        "started": started,
        "first_result_s": round(first or 0, 1),
        "all_results_s": round(total, 1),
        "where": dict(where),
        "complete": len(seen) == args.tasks,
    }
    print(json.dumps(summary, indent=2))
    if args.save:
        Path(args.save).write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
