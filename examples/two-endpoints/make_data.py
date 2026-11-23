"""Make the sensor CSVs for the two-endpoints tutorial (docs/tutorials/two-endpoints.md).

    python examples/two-endpoints/make_data.py ~/aegc-two-endpoints/data --files 20 --mb 25

Writes FILES files of about MB megabytes each, `sensor,ts,value` rows from a fixed seed, so the
same arguments always give the same bytes.
"""

import argparse
import random
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", help="directory to write the CSVs to")
    p.add_argument("--files", type=int, default=20)
    p.add_argument("--mb", type=float, default=25, help="about this many MB per file")
    args = p.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for i in range(args.files):
        rng = random.Random(i)
        path = out / f"run-{i:03d}.csv"
        target = int(args.mb * 1e6)
        with path.open("w") as f:
            f.write("sensor,ts,value\n")
            ts, size = 1_790_000_000 + i * 86_400, 16
            while size < target:
                line = f"s{rng.randrange(16):02d},{ts},{rng.gauss(20.0, 5.0):.4f}\n"
                f.write(line)
                size += len(line)
                ts += 1
        print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
