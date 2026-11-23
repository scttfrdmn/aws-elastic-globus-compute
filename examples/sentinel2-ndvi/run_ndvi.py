"""Part 1 of the Sentinel-2 NDVI tutorial: run `ndvi_latest` on aegc from a script or notebook.

    python examples/sentinel2-ndvi/run_ndvi.py --endpoint <endpoint id>

Prints the task's summary, the wall time from submission to result and where the task ran, and
downloads the NDVI GeoTIFF. Run it with Python 3.14, the earth-observation image's Python.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from globus_compute_sdk import Client, Executor
from globus_compute_sdk.serialize import ComputeSerializer, PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from ndvi_tasks import ndvi_latest

# A field-scale area of interest in California's Central Valley, about 9 km x 11 km.
DEFAULT_BBOX = [-120.35, 36.75, -120.25, 36.85]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", required=True, help="the aegc endpoint id")
    p.add_argument("--env", default="earth-observation")
    p.add_argument("--bbox", type=float, nargs=4, default=DEFAULT_BBOX, metavar="W S E N")
    p.add_argument("--days", type=int, default=30, help="look this far back for a scene")
    p.add_argument("--max-cloud", type=float, default=20, help="eo:cloud_cover below this")
    p.add_argument("--download", default=".", help="directory for the GeoTIFF ('' to skip)")
    args = p.parse_args()

    # Source text, as for Flows: the worker doesn't need this module installed.
    serializer = ComputeSerializer(strategy_code=PureSourceTextInspect())
    t0 = time.monotonic()
    with Executor(
        endpoint_id=args.endpoint,
        amqp_port=5671,
        user_endpoint_config={"env": args.env},
        serializer=serializer,
    ) as ex:
        fut = ex.submit(ndvi_latest, args.bbox, args.days, args.max_cloud)
        summary = fut.result()
    wall = time.monotonic() - t0

    print(json.dumps(summary, indent=2))
    print(f"task {fut.task_id}: {wall:.1f} s from submission to result")
    info = Client().get_task(fut.task_id).get("details", {}).get("aegc", {})
    if info:
        print(
            f"ran on {info.get('instance_type')} ({info.get('lifecycle')}), "
            f"exec {info.get('timings', {}).get('exec_s')} s, "
            f"queued {info.get('timings', {}).get('queue_wait_s')} s, "
            f"estimated ${info.get('est_cost_usd')} (static on-demand price: an upper bound)"
        )
    if summary.get("bytes_read") and summary.get("asset_bytes"):
        print(
            f"read {summary['bytes_read'] / 1e6:.1f} MB of {summary['asset_bytes'] / 1e6:.1f} MB "
            f"in the red, NIR and SCL COGs ({summary['bytes_read'] / summary['asset_bytes']:.2%})"
        )
    if summary.get("found") and args.download:
        from aegc import outputs

        dest = Path(args.download) / summary["uri"].rsplit("/", 1)[-1]
        outputs.download(summary["uri"], str(dest))
        print(f"downloaded {dest}")


if __name__ == "__main__":
    main()
