"""Genotype one region across the 1000 Genomes high-coverage cohort, from the SDK (no flow).

    python examples/cohort-genotype/run_cohort.py --endpoint <id> --out s3://$BUCKET/cohort/run1/

Plans the shards here, runs ``genotype`` for each on --genotype-pool (default the Lambda pool),
then ``merge`` once on --merge-pool (default the Spot pool), and prints timings, bytes read and
the variant sites' allele frequencies for --show positions. --limit N runs the first N samples.
Run it with Python 3.14, the image's Python.
"""

import argparse
import json
import sys
import time
from concurrent.futures import as_completed
from pathlib import Path

from globus_compute_sdk import Executor
from globus_compute_sdk.serialize import ComputeSerializer, PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from cohort_tasks import genotype, merge, plan

#: APOL1 on GRCh38, where the G1 (rs73885319, rs60910145) and G2 (rs71785313) kidney-disease
#: risk variants are.
APOL1 = "chr22:36253000-36268000"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", required=True)
    p.add_argument("--out", required=True, help="s3://bucket/prefix/ for shards and results")
    p.add_argument("--region", default=APOL1)
    p.add_argument("--genotype-pool", default="genomics-py314-lambda")
    p.add_argument("--merge-pool", default="genomics-py314")
    p.add_argument("--per-shard", type=int, default=20)
    p.add_argument("--limit", type=int, default=0, help="only the first N samples")
    p.add_argument("--save", help="write the summary to this JSON file")
    args = p.parse_args()

    # The plan runs here: it reads the cohort's index and writes the manifest to --out.
    tasks = plan(args.region, args.out, "local", per_shard=args.per_shard, limit=args.limit)
    manifest = tasks[0]["kwargs"]["manifest"]
    print(f"{len(tasks)} shards, region {args.region}, manifest {manifest}", flush=True)
    serializer = ComputeSerializer(strategy_code=PureSourceTextInspect())
    t0 = time.monotonic()
    first = None
    shards = []
    with Executor(endpoint_id=args.endpoint, amqp_port=5671, serializer=serializer,
                  user_endpoint_config={"pool": args.genotype_pool}) as ex:  # fmt: skip
        futures = [ex.submit(genotype, **t["kwargs"]) for t in tasks]
        errors = []
        for fut in as_completed(futures):
            try:
                shards.append(fut.result())
            except Exception as e:  # a whole shard failed: report it, merge the rest
                errors.append(str(e)[-300:])
                continue
            first = first or time.monotonic() - t0
            if len(shards) % 25 == 0:
                print(f"  {len(shards)}/{len(tasks)} shards at {time.monotonic() - t0:.0f} s",
                      flush=True)  # fmt: skip
    genotyped = time.monotonic() - t0
    with Executor(endpoint_id=args.endpoint, amqp_port=5671, serializer=serializer,
                  user_endpoint_config={"pool": args.merge_pool}) as ex:  # fmt: skip
        summary = ex.submit(merge, *shards, manifest=manifest).result()
    total = time.monotonic() - t0
    out = {
        **summary,
        "shards": len(shards),
        "failed_shards": errors,
        "genotype_pool": args.genotype_pool,
        "merge_pool": args.merge_pool,
        "first_shard_s": round(first or 0, 1),
        "genotype_wall_s": round(genotyped, 1),
        "total_wall_s": round(total, 1),
        "where": sorted({s["where"] for s in shards}),
    }
    print(json.dumps(out, indent=2))
    if args.save:
        Path(args.save).write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
