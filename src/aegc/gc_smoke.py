"""Submit a few functions to an AWSEngine endpoint and print the results.

    python -m aegc.gc_smoke <endpoint name> [N] [--env ENV]

Run inside the endpoint container (``aegc endpoint test``), where the Globus login lives. Workers
match this interpreter's Python minor version, so the SDK here must match an image that exists.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def whereami() -> str:
    import platform
    import sys

    return f"{platform.node()} python={platform.python_version()} exe={sys.executable}"


def square(x: int) -> int:
    return x * x


def main() -> None:
    from globus_compute_sdk import Executor

    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("n", nargs="?", type=int, default=10)
    ap.add_argument("--env")
    args = ap.parse_args()
    ep_file = Path.home() / ".globus_compute" / args.name / "endpoint.json"
    endpoint_id = json.loads(ep_file.read_text())["endpoint_id"]
    print(f"endpoint {args.name} = {endpoint_id}", flush=True)

    t0 = time.monotonic()
    config = {"env": args.env} if args.env else None
    # AMQPS on 5671 (globus/globus-compute#2232).
    with Executor(endpoint_id=endpoint_id, amqp_port=5671, user_endpoint_config=config) as gce:
        print("worker:", gce.submit(whereami).result(timeout=1800), flush=True)  # type: ignore[no-untyped-call]
        print(f"first result after {time.monotonic() - t0:.0f}s", flush=True)
        futures = [gce.submit(square, i) for i in range(args.n)]  # type: ignore[no-untyped-call]
        print("squares:", [f.result(timeout=600) for f in futures], flush=True)
    print(f"done in {time.monotonic() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
