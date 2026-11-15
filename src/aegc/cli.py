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

    services = ("iam", "ec2", "autoscaling", "sqs", "logs", "ssm", "ecr", "scheduler")
    return Clients(**{s: awsutil.client(sess, s) for s in services})  # type: ignore[arg-type]


def parse_pool(text: str) -> dict[str, Any]:
    """``env[:key=value,...]``, e.g. ``geospatial:max_vcpus=32,vcpu=4-16,mem=8192,on-demand``.

    Only the options given are returned, so a redeploy keeps the rest from the last deploy.
    ``max=``/``min=`` (workers) are the v0.1 spelling, converted to vCPUs by the caller.
    """
    env, _, opts = text.partition(":")
    out: dict[str, Any] = {"env": env}
    for opt in filter(None, opts.split(",")):
        key, _, value = opt.partition("=")
        if key == "max_vcpus":
            out["max_vcpus"] = int(value)
        elif key == "min_vcpus":
            out["min_vcpus"] = int(value)
        elif key == "max":
            out["_max_workers"] = int(value)
        elif key == "min":
            out["_min_workers"] = int(value)
        elif key == "walltime":
            out["walltime_s"] = int(value)
        elif key == "max_walltime":
            out["max_walltime_s"] = int(value)
        elif key == "lifetime":
            out["max_lifetime_s"] = int(value)
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
        elif key == "spot":
            out["spot"] = True
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
    import time
    from dataclasses import asdict

    from aegc import awsutil, images, lease, naming, state
    from aegc.awsutil import is_not_found
    from aegc.infra import deployment

    sess, region, account = _ctx(args)
    ssm = awsutil.client(sess, "ssm")  # type: ignore[arg-type]
    try:
        prev = deployment.read_manifest(ssm, args.deployment)
    except Exception as e:
        if not is_not_found(e):
            raise
        prev = {}
    prev_pools: dict[str, dict[str, Any]] = prev.get("pools", {})
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
        if _version(rec.aegc_version) < (0, 2) and not args.allow_old_image:
            raise SystemExit(
                f"aegc: the {env!r} image was built with aegc {rec.aegc_version}; its workers "
                "ignore spend limits, stop and the lease, and write no cost ledger. Rebuild it: "
                f"aegc image build --env {env} --base-tag ... (or pass --allow-old-image)"
            )
        name = f"{env}-py{rec.python_minor.replace('.', '')}"
        base: dict[str, Any] = {}
        if name in prev_pools:  # a redeploy keeps the options it isn't given (#30)
            base = asdict(deployment.PoolSpec.from_manifest(name, prev_pools[name]))
        vcpu_min = int(opts.get("vcpu_min", base.get("vcpu_min", 2)))
        for legacy, field_ in (("_max_workers", "max_vcpus"), ("_min_workers", "min_vcpus")):
            if legacy in opts:
                n = opts.pop(legacy)
                opts.setdefault(field_, n * vcpu_min)
                print(
                    f"warning: {'max' if 'max' in legacy else 'min'}={n} counts workers; "
                    f"pools are sized in vCPUs now: using {field_}={n * vcpu_min} "
                    f"({n} x vcpu={vcpu_min})",
                    file=sys.stderr,
                )
        kw = {**base, **opts, "name": name, "env": env, "python_minor": rec.python_minor}
        # A small max_vcpus with the default size range: keep workers within the pool's cap.
        if "vcpu_max" not in opts and "max_vcpus" in kw:
            default_max = deployment.PoolSpec.__dataclass_fields__["vcpu_max"].default
            vmax = int(kw.get("vcpu_max", default_max))
            if kw["max_vcpus"] and vmax > kw["max_vcpus"]:
                kw["vcpu_max"] = max(int(kw["max_vcpus"]), int(kw.get("vcpu_min", 2)))
        kw["image"] = rec.image
        pools.append(deployment.PoolSpec(**kw))
    named = {p.name for p in pools}
    omitted = sorted(n for n in prev_pools if n not in named)
    pruning = bool(omitted and args.prune and args.yes and not args.dry_run)
    if omitted and args.prune and not pruning:
        print(
            f"note: --prune would remove pool(s) {', '.join(omitted)}; this is a dry run of "
            "that part (add --yes to remove them)",
            file=sys.stderr,
        )
    elif omitted and not args.prune:
        print(
            f"warning: keeping pool(s) not listed here: {', '.join(omitted)} "
            "(pass --prune --yes to remove them)",
            file=sys.stderr,
        )
    if omitted and args.prune and not args.force:
        c0 = _clients(sess)
        busy = {n: deployment.pool_tasks(c0, prev_pools[n]) for n in omitted}  # type: ignore[arg-type]
        busy = {n: k for n, k in busy.items() if k}
        if busy:  # before anything changes: a refused prune mustn't strand a pool
            raise SystemExit(
                "aegc: can't prune pools with queued or running tasks: "
                + ", ".join(f"{n} ({k})" for n, k in busy.items())
                + "; wait, or add --force"
            )
    if not pruning:  # kept pools stay in the manifest, the lease and stop/resume
        pools += [deployment.PoolSpec.from_manifest(n, prev_pools[n]) for n in omitted]
    limits = state.Limits.from_dict(prev.get("limits")).merged(
        max_usd_day=args.max_usd_day,
        max_usd_month=args.max_usd_month,
        max_instance_hours_day=args.max_instance_hours_day,
        on_cap=args.on_cap,
    )
    prev_lease = prev.get("lease") or {}
    try:
        lease_s = lease.check_lease(
            lease.parse_duration(args.lease)
            if args.lease is not None
            else int(prev_lease.get("lease_s", lease.DEFAULT_LEASE_S))
        )
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    if args.ttl is not None:
        ttl = lease.parse_duration(args.ttl)
        ttl_at = time.time() + ttl if ttl else 0.0
    else:
        ttl_at = float(prev_lease.get("ttl_at") or 0.0)
    spec = deployment.DeploymentSpec(
        deployment=args.deployment,
        account=account,
        region=region,
        bucket=naming.bucket_name(account, region),
        pools=tuple(pools),
        limits=limits.to_dict(),
        lease_s=lease_s,
        ttl_at=ttl_at,
        grants=deploy_grants(
            args.grant, prev, deployment=args.deployment, account=account, region=region
        ),
    )
    errors = _quota_preflight(sess, [asdict(p) for p in pools])
    if errors and not args.dry_run:
        raise SystemExit("aegc: " + "\naegc: ".join(errors))
    if not limits.any:
        print(
            "note: no spend limits; set one with --max-usd-day / --max-usd-month",
            file=sys.stderr,
        )
    c = _clients(sess)
    report = deployment.deploy(c, spec, dry_run=args.dry_run)  # type: ignore[arg-type]
    if omitted and args.prune:
        for n in omitted:
            deployment.prune_pool(
                c,  # type: ignore[arg-type]
                args.deployment,
                n,
                prev_pools[n],
                report,
                dry_run=not pruning,
                force=args.force,
            )
    print(report)
    return 0


def deploy_grants(
    given: list[str] | None, prev: dict[str, Any], *, deployment: str, account: str, region: str
) -> tuple[Any, ...]:
    """The grants for a deploy: ``--grant`` values replace the previous manifest's, none given
    keeps them (like the limits and the lease), and ``--grant none`` clears them."""
    from aegc import naming
    from aegc.infra import iam

    try:
        if not given:
            grants = tuple(iam.Grant.from_dict(g) for g in prev.get("grants") or [])
        elif "none" in given:
            if len(given) > 1:
                raise ValueError("--grant none clears the grants; don't combine it with others")
            grants = ()
        else:
            grants = tuple(dict.fromkeys(iam.Grant.parse(g) for g in given))
        if grants:  # all of the worker role's inline policies share 10,240 characters
            worker = iam.worker_policy(
                deployment=deployment,
                region=region,
                account=account,
                bucket=naming.bucket_name(account, region),
            )
            room = iam.INLINE_POLICY_LIMIT - iam.policy_size(worker)
            iam.grants_policy(grants, deployment=deployment, budget=room)
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    return grants


def _version(text: str) -> tuple[int, ...]:
    parts = []
    for p in text.split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def _limit_value(text: str) -> Any:
    return "none" if text == "none" else float(text)


def _limit_args(p: argparse.ArgumentParser) -> None:
    for flag, what in (
        ("--max-usd-day", "estimated $ per UTC day"),
        ("--max-usd-month", "estimated $ per UTC month"),
        ("--max-instance-hours-day", "instance-hours per UTC day"),
    ):
        p.add_argument(flag, type=_limit_value, metavar="N|none", help=f"cap on {what}")
    p.add_argument(
        "--on-cap", choices=["fail", "kill"],
        help="when a cap trips, let running tasks finish (fail, the default) or fail them (kill)",
    )  # fmt: skip


def _stop(args: argparse.Namespace) -> int:
    from aegc import awsutil, control

    sess, _, _ = _ctx(args)
    if args.hard and not args.yes:
        raise SystemExit("aegc: --hard fails running tasks; add --yes to confirm")
    print(
        control.stop(
            ssm=awsutil.client(sess, "ssm"),  # type: ignore[arg-type]
            autoscaling=awsutil.client(sess, "autoscaling"),  # type: ignore[arg-type]
            deployment=args.deployment,
            hard=args.hard,
        )
    )
    return 0


def _resume(args: argparse.Namespace) -> int:
    from aegc import awsutil, control

    sess, _, _ = _ctx(args)
    print(
        control.resume(
            ssm=awsutil.client(sess, "ssm"),  # type: ignore[arg-type]
            autoscaling=awsutil.client(sess, "autoscaling"),  # type: ignore[arg-type]
            deployment=args.deployment,
            scheduler=awsutil.client(sess, "scheduler"),  # type: ignore[arg-type]
        )
    )
    return 0


def _limits(args: argparse.Namespace) -> int:
    from aegc import awsutil, control, naming, pricing
    from aegc.spend import SpendGuard

    sess, region, account = _ctx(args)
    spend = SpendGuard(
        s3=awsutil.client(sess, "s3"),  # type: ignore[arg-type]
        bucket=naming.bucket_name(account, region),
        deployment=args.deployment,
        prices=pricing.Prices(
            region=region,
            ec2=awsutil.client(sess, "ec2"),  # type: ignore[arg-type]
            pricing=awsutil.client(awsutil.session(args.profile, "us-east-1"), "pricing"),
        ),
    )
    try:
        report = control.set_limits(
            ssm=awsutil.client(sess, "ssm"),  # type: ignore[arg-type]
            autoscaling=awsutil.client(sess, "autoscaling"),  # type: ignore[arg-type]
            deployment=args.deployment,
            resume_now=args.resume,
            spend=spend if args.resume else None,
            scheduler=awsutil.client(sess, "scheduler"),  # type: ignore[arg-type]
            max_usd_day=args.max_usd_day,
            max_usd_month=args.max_usd_month,
            max_instance_hours_day=args.max_instance_hours_day,
            on_cap=args.on_cap,
        )
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    print(report)
    return 0


def _cost(args: argparse.Namespace) -> int:
    from aegc import awsutil, control, naming, pricing

    sess, region, account = _ctx(args)
    prices = pricing.Prices(
        region=region,
        ec2=awsutil.client(sess, "ec2"),  # type: ignore[arg-type]
        pricing=awsutil.client(awsutil.session(args.profile, "us-east-1"), "pricing"),
    )
    report = control.cost(
        s3=awsutil.client(sess, "s3"),  # type: ignore[arg-type]
        bucket=naming.bucket_name(account, region),
        deployment=args.deployment,
        prices=prices,
        since=args.since,
        by=args.by,
    )
    if args.actual:
        report.actual = control.actual_cost(
            awsutil.client(awsutil.session(args.profile, "us-east-1"), "ce"),
            args.deployment,
            report.start,
            report.end,
        )
    print(report.render())
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
    from aegc import pricing

    st = ops.status(
        ssm=awsutil.client(sess, "ssm"),  # type: ignore[arg-type]
        sqs=awsutil.client(sess, "sqs"),  # type: ignore[arg-type]
        autoscaling=awsutil.client(sess, "autoscaling"),  # type: ignore[arg-type]
        deployment=args.deployment,
        region=region,
        sq=awsutil.client(sess, "service-quotas"),  # type: ignore[arg-type]
        cw=awsutil.client(sess, "cloudwatch"),  # type: ignore[arg-type]
        s3=awsutil.client(sess, "s3"),  # type: ignore[arg-type]
        prices=pricing.Prices(
            region=region,
            ec2=awsutil.client(sess, "ec2"),  # type: ignore[arg-type]
            pricing=awsutil.client(awsutil.session(args.profile, "us-east-1"), "pricing"),
        ),
        scheduler=awsutil.client(sess, "scheduler"),  # type: ignore[arg-type]
    )
    if args.json:
        print(json.dumps(st.to_dict(), indent=2, sort_keys=True))
    else:
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
        help="env[:max_vcpus=N,min_vcpus=N,vcpu=LO-HI,mem=MiB,slots=N|auto,idle=S,disk=GiB,"
        "throughput=MiBps,walltime=S,max_walltime=S,lifetime=S,on-demand|spot]; repeatable. "
        "max_vcpus caps the pool (default 8); vcpu is each worker's size range (default 2-8, "
        "at most max_vcpus); disk: root gp3 GiB (30-16384, default 30); throughput: its MiB/s "
        "(125-1000, default 125); lifetime: 3600-259200 s (default 86400). On a redeploy, "
        "options not given keep their last values.",
    )  # fmt: skip
    _limit_args(dp)
    dp.add_argument(
        "--grant", action="append", metavar="read|write=s3://BUCKET/PREFIX|none",
        help="let every task in the deployment read or write under an S3 prefix of yours "
        "(repeatable; s3://BUCKET/ is the whole bucket). Replaces the previous grants; without "
        "--grant a redeploy keeps them, and --grant none removes them.",
    )  # fmt: skip
    dp.add_argument(
        "--lease", metavar="DURATION",
        help="freeze the deployment if no endpoint renews it for this long (default 48h; "
        "'none' disables); running endpoints renew hourly",
    )  # fmt: skip
    dp.add_argument(
        "--ttl", metavar="DURATION",
        help="freeze the deployment this long from now, renewed or not (e.g. 7d; 'none' clears)",
    )  # fmt: skip
    dp.add_argument(
        "--prune", action="store_true",
        help="remove pools not listed (a dry run of that part unless --yes)",
    )  # fmt: skip
    dp.add_argument("--yes", action="store_true", help="with --prune: actually remove them")
    dp.add_argument("--force", action="store_true", help="--prune even with tasks queued")
    dp.add_argument(
        "--allow-old-image", action="store_true",
        help="deploy images built before aegc 0.2 (their workers ignore limits and stop)",
    )  # fmt: skip
    dp.add_argument("--dry-run", action="store_true")
    dp.set_defaults(func=_deploy)

    sp = sub.add_parser("stop", help="stop a deployment's spending now (kill switch)")
    sp.add_argument("deployment")
    sp.add_argument(
        "--hard", action="store_true",
        help="also fail running tasks and terminate workers after 2 minutes (needs --yes)",
    )  # fmt: skip
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=_stop)

    rs = sub.add_parser("resume", help="resume a stopped, capped or expired deployment")
    rs.add_argument("deployment")
    rs.set_defaults(func=_resume)

    lm = sub.add_parser("limits", help="show or change a deployment's spend limits")
    lm.add_argument("deployment")
    _limit_args(lm)
    lm.add_argument("--resume", action="store_true", help="also end a capped state now")
    lm.set_defaults(func=_limits)

    co = sub.add_parser("cost", help="estimated spend from the cost ledger")
    co.add_argument("deployment")
    co.add_argument("--since", default="today", help="today (default), month, or e.g. 24h, 7d")
    co.add_argument("--by", choices=["pool", "type"], default="pool")
    co.add_argument(
        "--actual", action="store_true",
        help="also ask Cost Explorer (costs $0.01; needs the aegc:deployment tag active)",
    )  # fmt: skip
    co.set_defaults(func=_cost)

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

    st = sub.add_parser(
        "status", help="state, spend, limits, lease, pools, queues, workers and waiting results"
    )
    st.add_argument("deployment")
    st.add_argument("--json", action="store_true", help="machine-readable output")
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
