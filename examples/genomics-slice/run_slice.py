"""Run the 1000 Genomes slice on aegc in us-east-1 and report what was shipped.

    python examples/genomics-slice/run_slice.py --endpoint <endpoint id>

Prints the task's summary, the wall time, the bytes shipped against the whole file, and the
internet egress the slice avoided; downloads the allele-frequency table (and, with
--download-slice, the slice). Run it with Python 3.14, the image's Python.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from globus_compute_sdk import Client, Executor
from globus_compute_sdk.serialize import ComputeSerializer, PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from slice_tasks import CHR22, slice_vcf

EGRESS_USD_PER_GB = 0.09  # internet data transfer out, first 10 TB a month; AWS bills per GiB


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", required=True, help="the aegc endpoint id (us-east-1)")
    p.add_argument("--env", default="genomics")
    p.add_argument("--locus", default="chr22:20000000-21000000", help="the region to slice")
    p.add_argument("--url", default=CHR22, help="a bgzipped, tabix-indexed VCF over HTTPS")
    p.add_argument("--download-slice", action="store_true", help="also fetch the slice")
    args = p.parse_args()

    serializer = ComputeSerializer(strategy_code=PureSourceTextInspect())
    t0 = time.monotonic()
    with Executor(
        endpoint_id=args.endpoint,
        amqp_port=5671,
        user_endpoint_config={"env": args.env},
        serializer=serializer,
    ) as ex:
        fut = ex.submit(slice_vcf, args.locus, args.url)
        r = fut.result()
    wall = time.monotonic() - t0

    print(json.dumps(r, indent=2))
    print(f"task {fut.task_id}: {wall:.1f} s from submission to result")
    info = Client().get_task(fut.task_id).get("details", {}).get("aegc", {})
    if info:
        print(
            f"ran on {info.get('instance_type')} ({info.get('lifecycle')}) in "
            f"{info.get('availability_zone')}, exec {info.get('timings', {}).get('exec_s')} s"
        )
    gib = 2**30
    whole, part = r["source_bytes"], r["slice_bytes"]
    print(
        f"the whole file: {whole / gib:.1f} GiB; the slice: {part / 2**20:.1f} MiB "
        f"({part / whole:.2%}); the allele-frequency table: {r['af_bytes'] / 2**10:.1f} KiB"
    )
    print(
        f"downloading the whole file would be {whole / gib:.1f} GiB of internet egress "
        f"(${whole / gib * EGRESS_USD_PER_GB:.2f} at ${EGRESS_USD_PER_GB}/GB from a bucket you pay "
        f"for); the slice is {part / gib:.2f} GiB (${part / gib * EGRESS_USD_PER_GB:.3f})"
    )

    from aegc import outputs

    for key in ("af_uri", "index_uri", "slice_uri") if args.download_slice else ("af_uri",):
        dest = Path(r[key].rsplit("/", 1)[-1])
        outputs.download(r[key], str(dest))
        print(f"downloaded {dest}")


if __name__ == "__main__":
    main()
