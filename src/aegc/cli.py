"""The `aegc` command line."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

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


def _image_build(args: argparse.Namespace) -> int:
    from pathlib import Path

    from aegc import awsutil, images

    spec = images.ImageSpec(
        env=args.env, base_tag=args.base_tag, source=args.source, gce_version=args.gce_version
    )
    sess = awsutil.session(args.profile, args.region)
    region = sess.region_name
    if not region:
        print("aegc: no region; pass --region or configure one", file=sys.stderr)
        return 2
    result = images.build(
        spec,
        account=awsutil.account_id(sess),
        region=region,
        ecr=awsutil.client(sess, "ecr"),
        ssm=awsutil.client(sess, "ssm"),
        project_dir=Path(args.project_dir),
        push_image=not args.no_push,
        through_cache=not args.direct,
    )
    print(result if isinstance(result, str) else result.image)
    return 0


def _endpoint(args: argparse.Namespace) -> int:
    from pathlib import Path

    from aegc import awsutil
    from aegc import endpoint as ep

    profile = args.profile or ep.default_profile()
    region = args.region or awsutil.session(profile or None).region_name or ""
    spec = ep.EndpointSpec(
        name=args.name,
        deployment=args.deployment,
        region=region,
        default_env=args.env or "",
        aws_profile=profile,
    )
    cmd = args.endpoint_command
    if cmd == "build":
        ep.build(Path(args.project_dir), python=args.python)
    elif cmd == "login":
        ep.login(spec)
    elif cmd == "configure":
        if not (region and args.env):
            print("aegc: configure needs --region (or a profile region) and --env", file=sys.stderr)
            return 2
        ep.configure(spec)
        print(
            f"configured {spec.name}: deployment {spec.deployment}, region {region}, env {args.env}"
        )
    elif cmd == "start":
        ep.start(spec)
        print(f"started {spec.container}; endpoint id {ep.wait_for_id(spec)}")
    elif cmd == "stop":
        ep.stop(spec)
    elif cmd == "logs":
        ep.logs(spec)
    elif cmd == "test":
        ep.smoke(spec, args.n, args.env)
    elif cmd == "id":
        print(ep.endpoint_id(spec))
    return 0


def _ctx(args: argparse.Namespace) -> tuple[object, str, str]:
    from aegc import awsutil

    sess = awsutil.session(args.profile, args.region)
    if not sess.region_name:
        raise SystemExit("aegc: no region; pass --region or configure one")
    return sess, str(sess.region_name), awsutil.account_id(sess)


def _bootstrap(args: argparse.Namespace) -> int:
    from aegc import awsutil
    from aegc.infra import bootstrap

    sess, region, account = _ctx(args)
    c = {s: awsutil.client(sess, s) for s in ("s3", "ecr", "ssm", "iam")}  # type: ignore[arg-type]
    if args.command == "bootstrap":
        report = bootstrap.bootstrap(**c, account=account, region=region, dry_run=args.dry_run)
    else:
        report = bootstrap.unbootstrap(**c, account=account, region=region, dry_run=not args.yes)
    print(report)
    return 0


def _clients(sess: object) -> object:
    from aegc import awsutil
    from aegc.infra.deployment import Clients

    services = ("iam", "ec2", "autoscaling", "sqs", "logs", "ssm", "ecr")
    return Clients(**{s: awsutil.client(sess, s) for s in services})  # type: ignore[arg-type]


def parse_pool(text: str) -> dict[str, Any]:
    """``env[:key=value,...]``, e.g. ``geospatial:max=8,vcpu=4-16,mem=8192,disk=50,on-demand``."""
    env, _, opts = text.partition(":")
    out: dict[str, Any] = {"env": env}
    for opt in filter(None, opts.split(",")):
        key, _, value = opt.partition("=")
        if key == "max":
            out["max_workers"] = int(value)
        elif key == "min":
            out["min_workers"] = int(value)
        elif key == "vcpu":
            lo, _, hi = value.partition("-")
            out["vcpu_min"] = int(lo)
            out["vcpu_max"] = int(hi or lo)
        elif key == "mem":
            out["memory_mib_min"] = int(value)
        elif key == "slots":
            out["slots"] = value
        elif key == "on-demand":
            out["spot"] = False
        elif key == "idle":
            out["idle_timeout_s"] = int(value)
        elif key == "disk":
            out["root_volume_gib"] = int(value)
        elif key == "throughput":
            out["throughput_mibps"] = int(value)
        else:
            raise SystemExit(f"aegc: unknown pool option {key!r} in {text!r}")
    return out


def _deploy(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from aegc import awsutil, images, naming
    from aegc.infra import deployment

    sess, region, account = _ctx(args)
    ssm = awsutil.client(sess, "ssm")  # type: ignore[arg-type]
    pools = []
    for text in args.pool:
        opts = parse_pool(text)
        env = str(opts.pop("env"))
        try:
            rec = images.read_image(ssm, env)
        except ssm.exceptions.ParameterNotFound:
            raise SystemExit(
                f"aegc: no image recorded for env {env!r}; run `aegc image build --env {env} ...`"
            ) from None
        name = f"{env}-py{rec.python_minor.replace('.', '')}"
        pools.append(
            deployment.PoolSpec(
                name=name, env=env, python_minor=rec.python_minor, image=rec.image, **opts
            )
        )
    spec = deployment.DeploymentSpec(
        deployment=args.deployment,
        account=account,
        region=region,
        bucket=naming.bucket_name(account, region),
        pools=tuple(pools),
    )
    errors = _quota_preflight(sess, [asdict(p) for p in pools])
    if errors and not args.dry_run:
        raise SystemExit("aegc: " + "\naegc: ".join(errors))
    print(deployment.deploy(_clients(sess), spec, dry_run=args.dry_run))  # type: ignore[arg-type]
    return 0


def _quota_preflight(sess: object, pools: list[dict[str, Any]]) -> list[str]:
    """Print vCPU quota warnings; return the errors (pools that can never launch a worker)."""
    from aegc import awsutil, quotas

    checks = quotas.check(
        pools,
        sq=awsutil.client(sess, "service-quotas"),  # type: ignore[arg-type]
        cw=awsutil.client(sess, "cloudwatch"),  # type: ignore[arg-type]
    )
    for c in checks:
        for w in c.warnings:
            print(f"warning: {w}", file=sys.stderr)
    return [e for c in checks for e in c.errors]


def _destroy(args: argparse.Namespace) -> int:
    from aegc.infra import deployment

    sess, region, _ = _ctx(args)
    c = _clients(sess)
    if args.verify and args.no_verify:
        raise SystemExit("aegc: --verify and --no-verify contradict each other")
    if args.verify and not args.yes:
        # Verification only: re-check a destroy that ran earlier. Deletes nothing.
        report = deployment.verify_destroyed(
            c,  # type: ignore[arg-type]
            deployment=args.deployment,
            region=region,
            wait_s=0,
        )
    else:
        report = deployment.destroy(
            c,  # type: ignore[arg-type]
            deployment=args.deployment,
            region=region,
            dry_run=not args.yes,
            force=args.force,
            verify=not args.no_verify,
        )
    print(report)
    if not args.yes and not args.verify:
        print("dry run; pass --yes to delete")
    left = deployment.remaining(report)
    if left:
        print(f"aegc: {len(left)} resource(s) of {args.deployment} remain", file=sys.stderr)
        return 1
    if args.verify or (args.yes and not args.no_verify):
        print(f"verified: nothing of {args.deployment} remains")
    return 0


def _footprint(args: argparse.Namespace) -> int:
    from aegc import awsutil, ops

    sess, region, _ = _ctx(args)
    fp = ops.footprint(
        **{
            k: awsutil.client(sess, svc)  # type: ignore[arg-type]
            for k, svc in (
                ("tagging", "resourcegroupstaggingapi"),
                ("sqs", "sqs"),
                ("autoscaling", "autoscaling"),
                ("ec2", "ec2"),
                ("ssm", "ssm"),
            )
        },
        deployment=args.deployment,
        region=region,
    )
    print(fp.render())
    return 0


def _status(args: argparse.Namespace) -> int:
    from aegc import awsutil, ops

    sess, region, _ = _ctx(args)
    st = ops.status(
        ssm=awsutil.client(sess, "ssm"),  # type: ignore[arg-type]
        sqs=awsutil.client(sess, "sqs"),  # type: ignore[arg-type]
        autoscaling=awsutil.client(sess, "autoscaling"),  # type: ignore[arg-type]
        deployment=args.deployment,
        region=region,
        sq=awsutil.client(sess, "service-quotas"),  # type: ignore[arg-type]
        cw=awsutil.client(sess, "cloudwatch"),  # type: ignore[arg-type]
    )
    print(st.render())
    return 0


def _reap(args: argparse.Namespace) -> int:
    from aegc import awsutil, ops

    sess, _, _ = _ctx(args)
    sqs = awsutil.client(sess, "sqs")  # type: ignore[arg-type]
    print(ops.reap(sqs=sqs, deployment=args.deployment, dry_run=not args.yes))
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
        "deployer: aegc deploy/destroy/stop/resume/reap/footprint. "
        "bootstrap: aegc bootstrap/unbootstrap. builder: aegc image build.",
    )
    p.add_argument("kind", choices=["worker", "operator", "deployer", "bootstrap", "builder"])
    p.add_argument("--deployment", default="default", help="deployment name (worker, operator)")
    p.add_argument("--account", help="AWS account id (default: the caller's)")
    p.set_defaults(func=_iam_policy)

    img = sub.add_parser("image", help="worker images").add_subparsers(
        dest="image_command", required=True
    )
    b = img.add_parser("build", help="build, push and record a worker image for an environment")
    b.add_argument("--env", required=True, help="aarchsci env or aarchbio tool, e.g. geospatial")
    b.add_argument("--base-tag", required=True, help="immutable base tag, e.g. 2026.10.17.115051")
    b.add_argument("--source", default="aarchsci", choices=["aarchsci", "aarchbio"])
    b.add_argument("--gce-version", default="4.21.0", help="globus-compute-endpoint version")
    b.add_argument("--no-push", action="store_true", help="build and test locally only")
    b.add_argument(
        "--direct", action="store_true", help="pull the base from quay.io, not the ECR cache"
    )
    b.add_argument("--project-dir", default=".", help="aegc source tree to build the wheel from")
    b.set_defaults(func=_image_build)

    bs = sub.add_parser("bootstrap", help="create the region-wide resources (once per region)")
    bs.add_argument("--dry-run", action="store_true")
    bs.set_defaults(func=_bootstrap)
    ub = sub.add_parser("unbootstrap", help="remove the region-wide resources (dry run by default)")
    ub.add_argument("--yes", action="store_true", help="actually delete")
    ub.set_defaults(func=_bootstrap)

    dp = sub.add_parser("deploy", help="create or update a deployment and its worker pools")
    dp.add_argument("deployment")
    dp.add_argument(
        "--pool", action="append", required=True,
        help="env[:max=N,min=N,vcpu=LO-HI,mem=MiB,slots=N|auto,idle=S,disk=GiB,"
        "throughput=MiBps,on-demand]; repeatable. disk: root gp3 GiB (30-16384, default 30); "
        "throughput: its MiB/s (125-1000, default 125)",
    )  # fmt: skip
    dp.add_argument("--dry-run", action="store_true")
    dp.set_defaults(func=_deploy)

    ds = sub.add_parser("destroy", help="delete a deployment (dry run by default)")
    ds.add_argument("deployment")
    ds.add_argument("--yes", action="store_true", help="actually delete")
    ds.add_argument("--force", action="store_true", help="even with tasks queued or running")
    ds.add_argument(
        "--verify", action="store_true",
        help="without --yes: only check that nothing remains (exit 1 if something does)",
    )  # fmt: skip
    ds.add_argument(
        "--no-verify", action="store_true", help="with --yes: skip the check after deleting"
    )
    ds.set_defaults(func=_destroy)

    fp = sub.add_parser(
        "footprint",
        help="list what exists for a deployment: owned resources and untagged look-alikes",
    )
    fp.add_argument("deployment")
    fp.set_defaults(func=_footprint)

    st = sub.add_parser("status", help="pools, queues, workers and waiting results")
    st.add_argument("deployment")
    st.set_defaults(func=_status)

    rp = sub.add_parser("reap", help="delete idle result queues of finished endpoint processes")
    rp.add_argument("deployment")
    rp.add_argument("--yes", action="store_true", help="actually delete")
    rp.set_defaults(func=_reap)

    e = sub.add_parser("endpoint", help="run a Globus Compute endpoint with AWSEngine in Docker")
    e.add_argument(
        "endpoint_command",
        choices=["build", "login", "configure", "start", "stop", "logs", "test", "id"],
    )
    e.add_argument("--name", default="aegc", help="Globus Compute endpoint name")
    e.add_argument("--deployment", default="default", help="aegc deployment the engine uses")
    e.add_argument("--env", help="default science environment (configure); env to test (test)")
    e.add_argument("-n", type=int, default=10, help="tasks to submit (test)")
    e.add_argument("--project-dir", default=".", help="aegc source tree (build)")
    e.add_argument(
        "--python",
        default="3.14",
        help="Python in the endpoint image; `test` submits with it (build)",
    )
    e.set_defaults(func=_endpoint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    from aegc.infra import OwnershipError
    from aegc.naming import NameError_

    try:
        return int(args.func(args))
    except (NameError_, OwnershipError) as e:
        raise SystemExit(f"aegc: {e}") from None


if __name__ == "__main__":
    sys.exit(main())
