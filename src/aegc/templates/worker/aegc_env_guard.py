"""Fail the image build if installing globus-compute-endpoint changed the base environment.

usage: aegc_env_guard.py BEFORE.json AFTER.json ALLOW   (ALLOW: comma-separated package names)

BEFORE/AFTER are `micromamba list --json` outputs. Packages that were already present may not
change version or disappear, except those in ALLOW. New packages are fine.
"""

import json
import sys


def packages(path: str) -> dict[str, str]:
    with open(path) as f:
        data = json.load(f)
    # micromamba 1.x lists dicts at the top level; 2.x may wrap them.
    if isinstance(data, dict):
        data = data.get("packages", data.get("result", []))
    return {str(p["name"]): str(p["version"]) for p in data}


def main(argv: list[str]) -> int:
    before, after = packages(argv[1]), packages(argv[2])
    allow = {a.strip() for a in argv[3].split(",") if a.strip()} if len(argv) > 3 else set()
    changed = sorted(
        name for name, version in before.items() if name not in allow and after.get(name) != version
    )
    if changed:
        details = ", ".join(f"{n} {before[n]} -> {after.get(n, 'removed')}" for n in changed)
        print(f"base env modified outside allowlist: {details}", file=sys.stderr)
        return 1
    allowed = sorted(n for n in allow if n in before and after.get(n) != before[n])
    added = sorted(set(after) - set(before))
    print(f"env guard ok: {len(added)} added, allowlisted changes: {allowed or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
