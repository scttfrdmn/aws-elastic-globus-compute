"""The `aegc` command line. Subcommands arrive with their v0.1 issues."""

from __future__ import annotations

import argparse
import sys

from aegc import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aegc", description="Globus Compute on ephemeral AWS capacity."
    )
    parser.add_argument("--version", action="version", version=f"aegc {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
