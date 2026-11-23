"""The two steps of the two-endpoints flow (docs/tutorials/two-endpoints.md).

`make_manifest` runs on a laptop endpoint, where the data was made: it lists the CSVs and their
checksums. `summarize` runs on aegc, next to the copy in S3: it reads every file the manifest
names under a read grant, checks it against the laptop's checksum, sums it up per sensor, and
writes the summary with `aegc.outputs.put`. Both are registered as source text, so each imports
what it needs inside itself, and both take and return JSON, as Globus Flows needs.
"""


def make_manifest(data_dir, bucket, prefix):
    """The CSVs in `data_dir` as S3 keys under `prefix`, with their sizes and SHA-256 sums."""
    import hashlib
    import platform
    import time
    from pathlib import Path

    t0 = time.monotonic()
    items = []
    for path in sorted(Path(data_dir).expanduser().glob("*.csv")):
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        items.append({"key": prefix + path.name, "bytes": path.stat().st_size, "sha256": digest})
    return {
        "bucket": bucket,
        "items": items,
        "made_on": platform.node(),
        "seconds": round(time.monotonic() - t0, 2),
    }


def summarize(manifest):
    """Per-sensor count, mean, min and max over the manifest's files, checked against its sums.

    On aegc the summary goes to S3 with `aegc.outputs.put`; anywhere else (the laptop endpoint,
    for the comparison) to a local temporary file. Returns where it went and what was read.
    """
    import csv
    import hashlib
    import io
    import json
    import os
    import platform
    import tempfile
    import time
    from concurrent.futures import ThreadPoolExecutor

    import boto3

    t0 = time.monotonic()
    s3 = boto3.client("s3")
    bucket, items = manifest["bucket"], manifest["items"]

    def fetch(item):
        return item, s3.get_object(Bucket=bucket, Key=item["key"])["Body"].read()

    sensors = {}  # sensor -> [count, sum, min, max]
    verified, mismatched, nbytes, rows = 0, [], 0, 0
    with ThreadPoolExecutor(8) as pool:
        for i in range(0, len(items), 8):  # eight files in memory at a time
            for item, body in pool.map(fetch, items[i : i + 8]):
                nbytes += len(body)
                if hashlib.sha256(body).hexdigest() != item["sha256"]:
                    mismatched.append(item["key"])
                    continue
                verified += 1
                for row in csv.DictReader(io.StringIO(body.decode())):
                    v = float(row["value"])
                    s = sensors.setdefault(row["sensor"], [0, 0.0, v, v])
                    s[0] += 1
                    s[1] += v
                    s[2] = min(s[2], v)
                    s[3] = max(s[3], v)
                    rows += 1
    summary = {
        "files": len(items),
        "rows": rows,
        "sensors": {
            k: {"count": c, "mean": round(t / c, 4), "min": lo, "max": hi}
            for k, (c, t, lo, hi) in sorted(sensors.items())
        },
    }
    data = json.dumps(summary, indent=1).encode()
    if os.environ.get("AEGC_TASK_UUID"):  # on an aegc worker
        from aegc import outputs

        where = outputs.put(data, name="summary.json")
    else:
        fd, path = tempfile.mkstemp(prefix="aegc-two-endpoints-", suffix=".json")
        os.write(fd, data)
        os.close(fd)
        where = "file://" + path
    return {
        "summary": where,
        "files": len(items),
        "verified": verified,
        "mismatched": mismatched,
        "rows": rows,
        "bytes": nbytes,
        "ran_on": platform.node(),
        "seconds": round(time.monotonic() - t0, 2),
    }
