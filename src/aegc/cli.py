"""The `aegc` command line."""

from __future__ import annotations

import argparse
import json
import sys

from aegc import __version__


def _iam_policy(args: argparse.Namespace) -> int:
    from aegc import awsutil
    from aegc.infra import iam

    region, account = args.region, args.account
    if not (region and account):
        sess = awsutil.session(args.profile, region)
        region = region or sess.region_name
        account = account or awsutil.account_id(sess)
    if not region:
        print("aegc: no region; pass --region or configure one", file=sys.stderr)
        return 2
    doc = iam.policy(args.kind, deployment=args.deployment, region=region, account=account)
    print(json.dumps(doc, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aegc", description="Globus Compute on ephemeral AWS capacity."
    )
    parser.add_argument("--version", action="version", version=f"aegc {__version__}")
    parser.add_argument("--profile", help="AWS profile (default: the environment's)")
    parser.add_argument("--region", help="AWS region (default: the profile's)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser(
        "iam-policy",
        help="print an IAM policy document for one of aegc's roles",
        description="worker: on worker instances. operator: the endpoint's credentials. "
        "deployer: aegc bootstrap/deploy/destroy.",
    )
    p.add_argument("kind", choices=["worker", "operator", "deployer"])
    p.add_argument("--deployment", default="default", help="deployment name (worker, operator)")
    p.add_argument("--account", help="AWS account id (default: the caller's)")
    p.set_defaults(func=_iam_policy)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
