"""Run the 200-task Collatz sweep on aegc, optionally interrupting Spot workers with FIS mid-run.

    python examples/spot-sweep/run_sweep.py --endpoint <id> --save baseline.json
    python examples/spot-sweep/run_sweep.py --endpoint <id> --compare baseline.json \\
        --fis-deployment sweep --fis-pool plain-py314

Prints the wall time, checks that every task came back once with the right bounds, and with
--compare, that every answer matches an earlier run's. With --fis-deployment and --fis-pool it
starts the pool's FIS experiment (see fis.py; run `fis.py setup` first) once --fis-after results
are in. --provenance asks Globus Compute where each task ran, and counts the tasks that ran more
than once. Run it with Python 3.14, the image's Python.
"""

import argparse
import collections
import json
import sys
import threading
import time
from concurrent.futures import as_completed
from pathlib import Path

from globus_compute_sdk import Client, Executor
from globus_compute_sdk.serialize import ComputeSerializer, PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
import fis
from sweep_tasks import collatz_chunk

ANSWER = ("index", "lo", "hi", "best_n", "best_steps", "total_steps")  # not "seconds"


def answers(results):
    return {r["index"]: {k: r[k] for k in ANSWER} for r in results}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", required=True, help="the aegc endpoint id")
    p.add_argument("--env", default="plain")
    p.add_argument("--tasks", type=int, default=200)
    p.add_argument("--size", type=int, default=5_000_000, help="start values per task")
    p.add_argument("--save", help="write the results to this JSON file")
    p.add_argument("--compare", help="check the answers against this earlier run's file")
    p.add_argument("--fis-deployment", help="start this deployment's FIS experiment mid-run")
    p.add_argument("--fis-pool", help="... for this pool, e.g. plain-py314")
    p.add_argument("--fis-after", type=int, default=40, help="results in before FIS starts")
    p.add_argument("--profile", help="AWS profile for FIS")
    p.add_argument("--region", help="AWS region for FIS")
    p.add_argument("--provenance", action="store_true", help="fetch each task's details")
    args = p.parse_args()
    if bool(args.fis_deployment) != bool(args.fis_pool):
        p.error("give both --fis-deployment and --fis-pool, or neither")

    sess = fis._session(args.profile, args.region) if args.fis_deployment else None
    experiment = {}

    def inject():
        experiment["id"] = fis.start(sess, args.fis_deployment, args.fis_pool)
        experiment["at"] = time.monotonic() - t0

    serializer = ComputeSerializer(strategy_code=PureSourceTextInspect())
    results, task_ids = [], []
    print(f"started {args.tasks} tasks at {time.strftime('%H:%M:%S')}", flush=True)
    t0 = time.monotonic()
    with Executor(
        endpoint_id=args.endpoint,
        amqp_port=5671,
        user_endpoint_config={"env": args.env},
        serializer=serializer,
    ) as ex:
        futures = [ex.submit(collatz_chunk, i, args.size) for i in range(args.tasks)]
        injector = None
        for fut in as_completed(futures):
            results.append(fut.result())
            task_ids.append(fut.task_id)
            n = len(results)
            if n % 20 == 0 or n == args.tasks:
                print(f"  {n}/{args.tasks} done at {time.monotonic() - t0:.0f} s", flush=True)
            if sess is not None and injector is None and n >= args.fis_after:
                injector = threading.Thread(target=inject)
                injector.start()
    wall = time.monotonic() - t0
    if injector is not None:
        injector.join()

    got = answers(results)
    bounds = {i: (r["lo"], r["hi"]) for i, r in got.items()}
    bad = [
        i for i in range(args.tasks) if bounds.get(i) != (i * args.size + 1, (i + 1) * args.size)
    ]
    if len(results) != len(got):
        bad.append("duplicates")
    print(f"{len(results)} results for {args.tasks} tasks in {wall:.0f} s ({wall / 60:.1f} min)")
    print(
        "every task came back once with its own bounds" if not bad else f"wrong or missing: {bad}"
    )
    exec_s = [r["seconds"] for r in results]
    print(f"in-task time: avg {sum(exec_s) / len(exec_s):.1f} s, max {max(exec_s):.1f} s")
    if args.compare:
        before = {int(k): v for k, v in json.loads(Path(args.compare).read_text()).items()}
        diff = sorted(i for i in got if got[i] != before.get(i))
        print(
            f"all {len(got)} answers match {args.compare}"
            if not diff and len(before) == len(got)
            else f"answers differ from {args.compare} for {diff or 'some indexes'}"
        )
    if args.save:
        Path(args.save).write_text(json.dumps(got, indent=1, sort_keys=True))
        print(f"saved {args.save}")
    if experiment:
        print(f"FIS experiment {experiment['id']} started at {experiment['at']:.0f} s")
        fis.wait_for(sess, experiment["id"])
    if args.provenance:
        gcc = Client()
        lifecycles, reruns, types = collections.Counter(), 0, collections.Counter()
        for tid in task_ids:
            info = gcc.get_task(tid).get("details", {}).get("aegc", {})
            lifecycles[info.get("lifecycle", "?")] += 1
            types[info.get("instance_type", "?")] += 1
            reruns += int(info.get("attempt", 1)) > 1 or int(info.get("receive_count", 1)) > 1
            time.sleep(0.05)  # stay well under the API's rate limits
        print(f"ran on: {dict(types)} ({dict(lifecycles)}); {reruns} task(s) ran more than once")


if __name__ == "__main__":
    main()
