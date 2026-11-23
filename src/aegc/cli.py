"""The `aegc` command line."""

from __future__ import annotations

import argparse
import json
import subprocess
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

    try:
        spec = images.ImageSpec(
            env=args.env,
            base_tag=args.base_tag or "",
            source=args.source,
            gce_version=args.gce_version,
            base=args.base or "",
            python=args.python or "",
            **(
                {"allow_change": tuple(sorted({*images.DEFAULT_ALLOW_CHANGE, *args.allow_change}))}
                if args.allow_change
                else {}
            ),
        )
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    sess = awsutil.session(args.profile, args.region)
    region = sess.region_name
    if not region:
        print("aegc: no region; pass --region or configure one", file=sys.stderr)
        return 2
    try:
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
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"aegc: `{' '.join(map(str, e.cmd))}` failed (exit {e.returncode})"
        ) from None
    print(result if isinstance(result, str) else result.image)
    return 0


def _image_register(args: argparse.Namespace) -> int:
    from aegc import awsutil, images

    sess = awsutil.session(args.profile, args.region)
    region = sess.region_name
    if not region:
        print("aegc: no region; pass --region or configure one", file=sys.stderr)
        return 2
    try:
        rec = images.register(
            args.env,
            args.image,
            account=awsutil.account_id(sess),
            region=region,
            ecr=awsutil.client(sess, "ecr"),
            ssm=awsutil.client(sess, "ssm"),
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"aegc: `{' '.join(map(str, e.cmd))}` failed (exit {e.returncode})"
        ) from None
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    print(rec.image)
    return 0


def _image_forget(args: argparse.Namespace) -> int:
    from aegc import awsutil, images

    ssm = awsutil.client(awsutil.session(args.profile, args.region), "ssm")
    try:
        left = images.forget(ssm, args.env, args.python)
    except KeyError as e:
        raise SystemExit(f"aegc: {e.args[0]}") from None
    except Exception as e:
        if not awsutil.is_not_found(e):
            raise
        raise SystemExit(f"aegc: no image recorded for env {args.env!r}") from None
    print(
        f"forgot {args.env} Python {args.python}; "
        + (f"left: {', '.join(sorted(left))}" if left else "no images left")
        + ". Deployed pools keep running until you redeploy or prune them."
    )
    return 0


def _image_list(args: argparse.Namespace) -> int:
    from aegc import awsutil, images

    sess = awsutil.session(args.profile, args.region)
    try:
        records = images.read_images(awsutil.client(sess, "ssm"), args.env)
    except Exception as e:
        if not awsutil.is_not_found(e):
            raise
        raise SystemExit(f"aegc: no image recorded for env {args.env!r}") from None
    for minor, rec in sorted(records.items()):
        print(f"{minor}\t{rec.base_kind}\taegc {rec.aegc_version}\t{rec.image}")
    return 0


def _endpoint(args: argparse.Namespace) -> int:
    from pathlib import Path

    from aegc import awsutil
    from aegc import endpoint as ep

    profile = args.profile or ep.default_profile()
    region = args.region or awsutil.session(profile or None).region_name or ""
    allowed: tuple[str, ...] | None = None
    if args.allow_function:
        import uuid as _uuid

        if args.allow_function == ["none"]:
            allowed = ()
        else:
            try:
                allowed = tuple(str(_uuid.UUID(f)) for f in args.allow_function)
            except ValueError:
                raise SystemExit(
                    "aegc: --allow-function takes function ids (UUIDs), or none to allow any"
                ) from None
    spec = ep.EndpointSpec(
        name=args.name,
        deployment=args.deployment,
        region=region,
        default_env=args.env or "",
        aws_profile=profile,
        allowed_functions=allowed,
    )
    cmd = args.endpoint_command
    if cmd in ("deploy", "undeploy", "status"):
        return _endpoint_cloud(args, spec)
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
        if allowed is not None:
            print(
                f"allowed functions: {', '.join(allowed) if allowed else 'any'}; restart the "
                "endpoint (stop, start) for Globus to enforce it"
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


def _endpoint_cloud(args: argparse.Namespace, spec: Any) -> int:
    """``aegc endpoint deploy / undeploy / status``: the cloud-hosted endpoint (#22)."""

    sess, region, account = _ctx(args)
    c = _clients(sess)
    d = args.deployment
    try:
        return _endpoint_cloud_cmd(args, spec, sess, c, d, region, account)
    except ValueError as e:  # bad input, an endpoint id used elsewhere, an oversized login...
        raise SystemExit(f"aegc: {e}") from None


def _endpoint_cloud_cmd(
    args: argparse.Namespace, spec: Any, sess: Any, c: Any, d: str, region: str, account: str
) -> int:
    from pathlib import Path

    from aegc import awsutil
    from aegc import endpoint as ep
    from aegc.infra import Report, endpoint_host, secrets

    cmd = args.endpoint_command
    if cmd == "status":
        print(_endpoint_status(c, d, region))
        return 0
    if cmd == "undeploy":
        dry = not args.yes
        r = endpoint_host.undeploy(c, d, region, dry_run=dry)
        secrets.delete_login(c.secretsmanager, d, r, dry_run=dry)
        print(r)
        if dry:
            print("dry run; pass --yes to remove the cloud endpoint")
        return 0
    # deploy
    import secrets as _secrets
    from dataclasses import replace as _replace

    from aegc import naming
    from aegc.infra.deployment import read_manifest

    if not args.env:
        raise SystemExit("aegc: endpoint deploy needs --env (the default science environment)")
    try:
        read_manifest(c.ssm, d)
    except Exception as e:
        if not awsutil.is_not_found(e):
            raise
        raise SystemExit(
            f"aegc: no deployment {d!r} in {region}; `aegc deploy {d} ...` first"
        ) from None
    running = ep.is_running(spec)
    if running and not args.stop_local:
        raise SystemExit(
            f"aegc: the local endpoint {spec.name} is running ({spec.container}); one endpoint "
            "id can run in one place. Stop it, or pass --stop-local"
        )
    if running and not args.dry_run:
        ep.stop(spec)
    login = ep.export_login(spec)
    # The cloud endpoint keeps the local endpoint's allowed functions (#57) unless told otherwise.
    allowed = spec.allowed_functions
    if allowed is None:
        allowed = ep.local_allowed_functions(spec)
    token = secrets.metric_token(c.secretsmanager, d) or _secrets.token_urlsafe(24)
    login = _replace(login, metric_token=token)
    if args.image:
        image = args.image
    elif args.dry_run:
        image = f"<{naming.endpoint_repository()} image, built and pushed on deploy>@sha256:"
    else:
        image = ep.build_push(
            Path(args.project_dir),
            ecr=awsutil.client(sess, "ecr"),
            python=args.python,
        )
    cloud = ep.EndpointSpec(
        name=spec.name, deployment=d, region=region, default_env=spec.default_env,
        allowed_functions=allowed,
    )  # fmt: skip
    host = endpoint_host.EndpointHostSpec(
        deployment=d, account=account, region=region, name=spec.name,
        endpoint_id=login.endpoint_id, image=image, configure_script=ep.configure_script(cloud),
        instance_type=args.instance_type, alarm_email=args.alarm_email or "",
        metric_token=token, session_manager=args.session_manager,
    )  # fmt: skip
    report = Report()
    role_arn = f"arn:aws:iam::{account}:role{naming.IAM_PATH}{naming.endpoint_role(d, region)}"
    before = len(report.actions)
    secrets.put_login(
        c.secretsmanager, d, login, report, dry_run=args.dry_run,
        endpoint_role_arn=role_arn,
    )  # fmt: skip
    login_changed = report.actions[before].outcome in ("updated", "would-update")
    report.actions.extend(
        endpoint_host.deploy(c, host, dry_run=args.dry_run, roll=login_changed).actions
    )
    print(report)
    if not args.dry_run:
        print(
            f"cloud endpoint {spec.name} ({login.endpoint_id}) is starting on "
            f"{args.instance_type}; it's online in about 3 minutes (`aegc endpoint status "
            f"--deployment {d}` shows the instance, and the alarm turns OK)"
        )
    return 0


def _endpoint_status(c: Any, d: str, region: str) -> str:
    from aegc import naming
    from aegc.awsutil import error_code
    from aegc.infra import secrets
    from aegc.infra.deployment import ACCESS_DENIED

    lines = [f"cloud endpoint of {d} ({region}):"]
    g = c.autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[naming.endpoint_group(d)]
    )["AutoScalingGroups"]
    if not g:
        return lines[0] + " none (aegc endpoint deploy creates one)"
    inst = g[0].get("Instances", [])
    lines.append(
        "  instance: "
        + (", ".join(f"{i['InstanceId']} {i.get('InstanceType', '')} {i['LifecycleState']}"
                     for i in inst) or "none yet")
    )  # fmt: skip
    try:
        meta = secrets.metadata(c.secretsmanager, d)
        lines.append(f"  endpoint: {meta['name'] or '?'} ({meta['endpoint_id'] or 'no secret'})")
        a = c.cloudwatch.describe_alarms(AlarmNames=[naming.endpoint_alarm(d)]).get(
            "MetricAlarms", []
        )
        if a:
            state = a[0]["StateValue"]
            meaning = {"OK": "online", "ALARM": "OFFLINE"}.get(state, "no data yet")
            lines.append(f"  alarm {a[0]['AlarmName']}: {state} ({meaning})")
    except Exception as e:
        if error_code(e) not in ACCESS_DENIED:
            raise
        lines.append("  endpoint and alarm: not shown (needs the endpoint-deployer policy)")
    return "\n".join(lines)


def _evaluate(args: argparse.Namespace) -> int:
    import os

    from aegc import awsutil, evaluator

    sess, region, _ = _ctx(args)
    clients = {
        s: awsutil.client(sess, s)  # type: ignore[arg-type]
        for s in ("ssm", "s3", "autoscaling", "scheduler", "sqs", "ec2", "cloudwatch")
    }
    clients["pricing"] = awsutil.client(  # the Price List API is in us-east-1
        awsutil.session(args.profile, "us-east-1"), "pricing"
    )
    ev = evaluator.evaluate(
        deployment=args.deployment, region=region, clients=clients,
        endpoint_id=args.endpoint_id or "", dry_run=args.dry_run,
        metric_token=os.environ.get("AEGC_METRIC_TOKEN", ""),
    )  # fmt: skip
    print(ev.render(), file=sys.stderr)
    return 1 if ev.errors else 0


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

    services = (
        "iam", "ec2", "autoscaling", "sqs", "logs", "ssm", "ecr", "scheduler", "secretsmanager",
        "cloudwatch", "sns",
    )  # fmt: skip
    clients = {s: awsutil.client(sess, s) for s in (*services, "lambda")}  # type: ignore[arg-type]
    clients["lambda_"] = clients.pop("lambda")
    return Clients(**clients)


#: Pool options a Lambda pool takes (``env:lambda,...``); the rest are EC2-only.
LAMBDA_POOL_OPTIONS = {"lambda", "memory", "timeout", "concurrency", "tmp", "walltime",
                       "max_walltime", "python"}  # fmt: skip


def parse_pool(text: str) -> dict[str, Any]:
    """``env[:key=value,...]``, e.g. ``geospatial:max_vcpus=32,vcpu=4-16,mem=8192,on-demand``,
    or a Lambda pool (#21), ``geospatial:lambda,memory=4096,timeout=900,concurrency=20``.

    Only the options given are returned, so a redeploy keeps the rest from the last deploy.
    ``max=``/``min=`` (workers) are the v0.1 spelling, converted to vCPUs by the caller.
    """
    env, _, opts = text.partition(":")
    out: dict[str, Any] = {"env": env}
    keys = [o.partition("=")[0] for o in filter(None, opts.split(","))]
    if "lambda" in keys:
        if ec2 := [k for k in keys if k not in LAMBDA_POOL_OPTIONS]:
            raise SystemExit(
                f"aegc: {', '.join(ec2)} {'is' if len(ec2) == 1 else 'are'} for EC2 pools, not "
                f"Lambda pools ({text!r}); a Lambda pool takes "
                f"{', '.join(sorted(LAMBDA_POOL_OPTIONS - {'lambda'}))}"
            )
    elif lam := [k for k in keys if k in LAMBDA_POOL_OPTIONS - {"walltime", "max_walltime",
                                                                  "python"}]:  # fmt: skip
        raise SystemExit(f"aegc: {', '.join(lam)} need a Lambda pool: {env}:lambda,... ({text!r})")
    for opt in filter(None, opts.split(",")):
        key, _, value = opt.partition("=")
        if key == "lambda":
            if value:
                raise SystemExit(f"aegc: lambda takes no value ({text!r})")
            out["kind"] = "lambda"
        elif key == "memory":
            out["memory_mib"] = int(value)
        elif key == "timeout":
            out["timeout_s"] = int(value)
        elif key == "concurrency":
            out["max_concurrency"] = int(value)
        elif key == "tmp":
            out["ephemeral_mib"] = int(value)
        elif key == "max_vcpus":
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
        elif key == "python":
            out["python"] = value
        elif key == "size":
            out["size_class"] = value
        else:
            raise SystemExit(f"aegc: unknown pool option {key!r} in {text!r}")
    return out


def _pool_spec(
    args: argparse.Namespace,
    env: str,
    rec: Any,
    opts: dict[str, Any],
    prev_pools: dict[str, dict[str, Any]],
) -> Any:
    """The pool for one (env, Python minor) image: the options given over the last deploy's."""
    from dataclasses import asdict

    from aegc import naming, sizes
    from aegc.infra import deployment

    if _version(rec.aegc_version) < (0, 2) and not args.allow_old_image:
        raise SystemExit(
            f"aegc: the {env!r} image was built with aegc {rec.aegc_version}; its workers "
            "ignore spend limits, stop and the lease, and write no cost ledger. Rebuild it with "
            "`aegc image build` (or `image register`) again, or pass --allow-old-image"
        )
    size = str(opts.pop("size_class", ""))
    try:
        cls = sizes.get(size) if size else None
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    if cls is not None and _version(rec.aegc_version) < (0, 3):
        raise SystemExit(
            f"aegc: size={size} needs a worker image built with aegc 0.3 or later (the {env!r} "
            f"Python {rec.python_minor} image has {rec.aegc_version}, whose workers ignore sizes); "
            "rebuild it with `aegc image build`"
        )
    if opts.get("kind") == "lambda":
        return _lambda_pool_spec(args, env, rec, opts, prev_pools)
    name = f"{env}-py{rec.python_minor.replace('.', '')}" + (f"-{size}" if size else "")
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
    if cls is not None:
        kw.update(
            size_class=cls.name,
            task_cpus=cls.task_cpus,
            task_memory_gib=cls.task_memory_gib,
            memory_gib_per_vcpu_min=cls.memory_gib_per_vcpu,
        )
        if "vcpu_min" not in opts and int(kw.get("vcpu_min", 2)) < cls.task_cpus:
            kw["vcpu_min"] = cls.task_cpus  # workers big enough for one task
            kw["vcpu_max"] = max(int(kw.get("vcpu_max", 8)), cls.task_cpus)
        if cls.name == "whole" and "vcpu_min" not in opts:
            kw["vcpu_min"] = kw["vcpu_max"] = int(kw.get("vcpu_max", 8))  # one worker size
    # A small max_vcpus with the default size range: keep workers within the pool's cap.
    if "vcpu_max" not in opts and "max_vcpus" in kw:
        default_max = deployment.PoolSpec.__dataclass_fields__["vcpu_max"].default
        vmax = int(kw.get("vcpu_max", default_max))
        if kw["max_vcpus"] and vmax > kw["max_vcpus"]:
            kw["vcpu_max"] = max(int(kw["max_vcpus"]), int(kw.get("vcpu_min", 2)))
    kw["image"] = rec.image
    # 0.4 workers isolate task processes from the worker role (#32); older images can't.
    kw["isolate_tasks"] = _version(rec.aegc_version) >= (0, 4)
    try:
        return deployment.PoolSpec(**kw)
    except ValueError as e:
        hint = ""
        if size and len(name) > naming.MAX_POOL:
            room = naming.MAX_POOL - len(name) + len(env)
            hint = f" (with size={size}, env names can be at most {room} characters)"
        raise SystemExit(f"aegc: {e}{hint}") from None


#: Lambda pools run ``aegc-worker lambda``, which images built before aegc 0.7 lack.
LAMBDA_IMAGE_VERSION = (0, 7)


def _lambda_pool_spec(
    args: argparse.Namespace,
    env: str,
    rec: Any,
    opts: dict[str, Any],
    prev_pools: dict[str, dict[str, Any]],
) -> Any:
    """A Lambda pool (#21), ``<env>-py<NNN>-lambda``: the options given over the last deploy's."""
    from dataclasses import asdict

    from aegc import lambda_pools
    from aegc.infra import deployment

    if _version(rec.aegc_version) < LAMBDA_IMAGE_VERSION and not args.allow_old_image:
        raise SystemExit(
            f"aegc: a Lambda pool needs a worker image built with aegc 0.7 or later (the {env!r} "
            f"Python {rec.python_minor} image has {rec.aegc_version}, which has no "
            "`aegc-worker lambda`); rebuild it with `aegc image build`"
        )
    name = f"{env}-py{rec.python_minor.replace('.', '')}-{lambda_pools.KIND}"
    base: dict[str, Any] = {}
    if name in prev_pools:  # a redeploy keeps the options it isn't given (#30)
        base = asdict(deployment.PoolSpec.from_manifest(name, prev_pools[name]))
        # Walltimes derived from the old timeout follow a new one.
        if "timeout_s" in opts and "max_walltime_s" not in opts:
            ceiling = int(base["timeout_s"]) - lambda_pools.MARGIN_S
            if base.get("max_walltime_s") == ceiling:
                base.pop("max_walltime_s")
                if base.get("walltime_s") == ceiling and "walltime_s" not in opts:
                    base.pop("walltime_s")
    kw = {**base, **opts, "name": name, "env": env, "python_minor": rec.python_minor}
    kw.update(image=rec.image, kind=lambda_pools.KIND, isolate_tasks=False)
    try:
        return deployment.PoolSpec(**kw)
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None


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
        parsed = parse_pool(text)
        env = str(parsed.pop("env"))
        want = parsed.pop("python", None)
        try:
            records = images.read_images(ssm, env)
        except ssm.exceptions.ParameterNotFound:
            raise SystemExit(
                f"aegc: no image recorded for env {env!r}; run `aegc image build --env {env} ...`"
            ) from None
        size = str(parsed.get("size_class", ""))
        kind = str(parsed.get("kind", "ec2"))
        deployed = {
            str(p.get("python_minor"))
            for p in prev_pools.values()
            if p.get("env") == env
            and str(p.get("size_class", "")) == size
            and str(p.get("kind") or "ec2") == kind
        }
        if want:
            if want not in records:
                raise SystemExit(
                    f"aegc: no {env!r} image for Python {want}; have {', '.join(sorted(records))}"
                )
            records = {want: records[want]}
        elif deployed:
            # A redeploy updates the minors already deployed; a new one is added with python=.
            new = sorted(set(records) - deployed)
            if new:
                print(
                    f"note: {env} also has images for Python {', '.join(new)}; add a pool with "
                    f"--pool {env}:python={new[0]}",
                    file=sys.stderr,
                )
            records = {m: r for m, r in records.items() if m in deployed}
        else:
            # A first deploy makes one pool per recorded minor, skipping pre-0.2 images.
            old = sorted(m for m, r in records.items() if _version(r.aegc_version) < (0, 2))
            if old and not args.allow_old_image and len(old) < len(records):
                print(
                    f"warning: skipping {env} Python {', '.join(old)}: built with aegc < 0.2 "
                    f"(rebuild, or `aegc image forget --env {env} --python {old[0]}`)",
                    file=sys.stderr,
                )
                records = {m: r for m, r in records.items() if m not in old}
        if kind == "lambda" and not want and not deployed and not args.allow_old_image:
            old = sorted(
                m for m, r in records.items() if _version(r.aegc_version) < LAMBDA_IMAGE_VERSION
            )
            if old and len(old) < len(records):
                print(
                    f"warning: no Lambda pool for {env} Python {', '.join(old)}: built with "
                    "aegc < 0.7, which has no `aegc-worker lambda` (rebuild it)",
                    file=sys.stderr,
                )
                records = {m: r for m, r in records.items() if m not in old}
        if len(records) > 1:
            print(
                f"note: {env}: one pool per Python minor ({', '.join(sorted(records))}); the "
                "options given apply to each (min_vcpus and max_vcpus too)",
                file=sys.stderr,
            )
        for _minor, rec in sorted(records.items(), reverse=True):
            pools.append(_pool_spec(args, env, rec, dict(parsed), prev_pools))
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
    grants = deploy_grants(
        args.grant, prev, deployment=args.deployment, account=account, region=region
    )
    try:
        spec = deployment.DeploymentSpec(
            deployment=args.deployment,
            account=account,
            region=region,
            bucket=naming.bucket_name(account, region),
            pools=tuple(pools),
            limits=limits.to_dict(),
            lease_s=lease_s,
            ttl_at=ttl_at,
            grants=grants,
        )
    except ValueError as e:  # e.g. a Lambda pool's names too long for Lambda and IAM
        raise SystemExit(f"aegc: {e}") from None
    if old := [p.name for p in pools if not p.isolate_tasks and not p.is_lambda]:
        print(
            f"warning: {', '.join(old)}: images built before aegc 0.4 run tasks with the worker "
            "role's credentials (they can read other tasks' payloads and post results); rebuild "
            "them with `aegc image build` to isolate tasks",
            file=sys.stderr,
        )
    # vCPU quotas are for EC2 pools; Lambda pools are bounded by their mappings' concurrency.
    errors = _quota_preflight(sess, [asdict(p) for p in pools if not p.is_lambda])
    if errors and not args.dry_run:
        raise SystemExit("aegc: " + "\naegc: ".join(errors))
    if not limits.any:
        print(
            "note: no spend limits; set one with --max-usd-day / --max-usd-month",
            file=sys.stderr,
        )
    c = _clients(sess)
    try:  # a Lambda pool's image or environment too large: say so before anything changes
        deployment.check_lambda_pools(c, spec)  # type: ignore[arg-type]
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
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
            iam.grants_policy(grants, deployment=deployment, budget=room, account=account)
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
    from aegc.awsutil import error_code
    from aegc.infra import deployment

    try:  # Lambda pools (#21): functions and mappings, with look-alikes
        deployment.lambda_footprint(awsutil.client(sess, "lambda"), fp, args.deployment)  # type: ignore[arg-type]
    except Exception as e:
        if error_code(e) not in deployment.ACCESS_DENIED:
            raise
        print(f"note: Lambda pools {deployment.LAMBDA_DENIED}", file=sys.stderr)
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


def _metrics(args: argparse.Namespace) -> int:
    import time

    from aegc import awsutil, control, observability
    from aegc.infra.deployment import read_manifest

    sess, region, _ = _ctx(args)
    end = time.time()
    try:
        start = control.window_start(args.since, end)
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    summary = observability.metrics(
        cw=awsutil.client(sess, "cloudwatch"),  # type: ignore[arg-type]
        manifest=read_manifest(awsutil.client(sess, "ssm"), args.deployment),  # type: ignore[arg-type]
        deployment=args.deployment,
        region=region,
        start=start,
        end=end,
    )
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(summary.render())
    return 0


def _provenance(args: argparse.Namespace) -> int:
    import time
    import uuid

    from aegc import awsutil, control, observability

    try:
        tid = str(uuid.UUID(args.task_uuid))
    except ValueError:
        raise SystemExit(f"aegc: {args.task_uuid!r} isn't a task id (a UUID)") from None
    try:
        start = control.window_start(args.since, time.time())
    except ValueError as e:
        raise SystemExit(f"aegc: {e}") from None
    sess, _, _ = _ctx(args)
    records = observability.provenance(
        logs=awsutil.client(sess, "logs"),  # type: ignore[arg-type]
        deployment=args.deployment,
        task_uuid=tid,
        start=start,
    )
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    elif records:
        print(observability.render_provenance(tid, args.deployment, records))
    if not records:
        print(
            f"aegc: no task_finished line for {tid} in /aegc/{args.deployment} since "
            f"{args.since} (logs are kept 14 days; workers from images built before aegc 0.4 "
            "don't write them)",
            file=sys.stderr,
        )
        return 1
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
        "bootstrap: aegc bootstrap/unbootstrap. builder: aegc image build. task: task code on "
        "isolated pools, besides the grants (aegc-task on the task role). endpoint: the cloud "
        "endpoint host. endpoint-deployer: aegc endpoint deploy/undeploy, with deployer. "
        "lambda-deployer: aegc deploy/destroy of Lambda pools, with deployer.",
    )
    p.add_argument(
        "kind",
        choices=[
            "worker",
            "operator",
            "deployer",
            "bootstrap",
            "builder",
            "task",
            "endpoint",
            "endpoint-deployer",
            "lambda-deployer",
        ],
    )
    p.add_argument("--deployment", default="default", help="deployment name (worker, operator)")
    p.add_argument("--account", help="AWS account id (default: the caller's)")
    p.set_defaults(func=_iam_policy)

    img = sub.add_parser("image", help="worker images").add_subparsers(
        dest="image_command", required=True
    )
    b = img.add_parser("build", help="build, push and record a worker image for an environment")
    b.add_argument(
        "--env", required=True,
        help="the environment's name: an aarchsci env or aarchbio tool with --base-tag, or any "
        "name of yours with --base",
    )  # fmt: skip
    base = b.add_mutually_exclusive_group(required=True)
    base.add_argument("--base-tag", help="immutable aarchsci/aarchbio tag, e.g. 2026.10.17.115051")
    base.add_argument(
        "--base", metavar="REF",
        help="any linux/arm64 image with Python >= 3.11, e.g. python:3.13-slim@sha256:...; "
        "not latest or untagged. aegc builds from the image the tag points to now",
    )  # fmt: skip
    b.add_argument("--source", default="aarchsci", choices=["aarchsci", "aarchbio"])
    b.add_argument(
        "--allow-change", action="append", default=[], metavar="PACKAGE",
        help="let installing aegc change this package of the base (repeatable), e.g. "
        "python-dateutil when the base's is newer than botocore accepts; click always may",
    )  # fmt: skip
    b.add_argument(
        "--python", metavar="PATH", help="with --base: the base's Python (default python3)"
    )
    b.add_argument("--gce-version", default="4.21.0", help="globus-compute-endpoint version")
    b.add_argument("--no-push", action="store_true", help="build and test locally only")
    b.add_argument(
        "--direct", action="store_true", help="pull the base from quay.io, not the ECR cache"
    )
    b.add_argument("--project-dir", default=".", help="aegc source tree to build the wheel from")
    b.set_defaults(func=_image_build)
    r = img.add_parser(
        "register",
        help="check an image you built (with aegc and globus-compute-endpoint) and record it",
    )
    r.add_argument("--env", required=True, help="the environment's name")
    r.add_argument(
        "--image",
        required=True,
        help="linux/arm64 image: repo@sha256:..., an immutable tag, or a local image",
    )
    r.set_defaults(func=_image_register)
    fg = img.add_parser("forget", help="drop an environment's image for one Python minor")
    fg.add_argument("--env", required=True)
    fg.add_argument("--python", required=True, metavar="3.NN")
    fg.set_defaults(func=_image_forget)
    ls = img.add_parser("list", help="an environment's recorded images, one per Python minor")
    ls.add_argument("--env", required=True)
    ls.set_defaults(func=_image_list)

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
        "throughput=MiBps,walltime=S,max_walltime=S,lifetime=S,python=3.NN,"
        "size=small|medium|mem|large|whole,on-demand|spot]; "
        "repeatable. One pool per Python minor the env has images for (python= picks one). "
        "max_vcpus caps the pool (default 8); vcpu is each worker's size range (default 2-8, "
        "at most max_vcpus); disk: root gp3 GiB (30-16384, default 30); throughput: its MiB/s "
        "(125-1000, default 125); lifetime: 3600-259200 s (default 86400). A Lambda pool, "
        "<env>-py<NNN>-lambda: env:lambda[,memory=MiB,timeout=S,concurrency=N,tmp=MiB,"
        "walltime=S,max_walltime=S,python=3.NN] (memory 128-10240, default 2048; timeout "
        "60-1800, default 900; concurrency 2-1000, default 10; tmp 512-10240). On a redeploy, "
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
        "status",
        help="state, spend, limits, grants, lease, pools and their size classes, queues, workers "
        "and waiting results",
    )
    st.add_argument("deployment")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.set_defaults(func=_status)

    mt = sub.add_parser(
        "metrics",
        help="per-pool task counts, times, interruptions and capacity from CloudWatch",
        description=(
            "Reads the metrics workers emit (namespace aegc; dimensions Deployment, Pool and, "
            "on task counts, Outcome) and the Auto Scaling group metrics, with GetMetricData."
        ),
    )
    mt.add_argument("deployment")
    mt.add_argument(
        "--since", default="1h", help="a duration like 90m, 1h (default), 1d, 7d; or today, month"
    )
    mt.add_argument("--json", action="store_true", help="machine-readable output")
    mt.set_defaults(func=_metrics)

    pv = sub.add_parser(
        "provenance",
        help="where and how a task ran: image, instance, timings, estimated cost",
        description=(
            "Finds the task's task_finished log lines in /aegc/<deployment> (FilterLogEvents), "
            "one per attempt. The same record is in the Result's details['aegc']."
        ),
    )
    pv.add_argument("deployment")
    pv.add_argument("task_uuid")
    pv.add_argument(
        "--since", default="14d", help="how far back to look (default 14d, the retention)"
    )
    pv.add_argument("--json", action="store_true", help="the records as JSON")
    pv.set_defaults(func=_provenance)

    rp = sub.add_parser("reap", help="delete idle result queues of finished endpoint processes")
    rp.add_argument("deployment")
    rp.add_argument("--yes", action="store_true", help="actually delete")
    rp.set_defaults(func=_reap)

    e = sub.add_parser("endpoint", help="run a Globus Compute endpoint with AWSEngine in Docker")
    e.add_argument(
        "endpoint_command",
        choices=["build", "login", "configure", "start", "stop", "logs", "test", "id", "deploy",
                 "undeploy", "status"],
        help="deploy/undeploy/status: the cloud-hosted endpoint (EC2), running as your login",
    )  # fmt: skip
    e.add_argument("--name", default="aegc", help="Globus Compute endpoint name")
    e.add_argument("--deployment", default="default", help="aegc deployment the engine uses")
    e.add_argument("--env", help="default science environment (configure); env to test (test)")
    e.add_argument("-n", type=int, default=10, help="tasks to submit (test)")
    e.add_argument(
        "--allow-function", action="append", metavar="ID",
        help="configure, deploy: only these registered function ids may run (repeatable; "
        "'none' allows any; deploy keeps the local endpoint's list by default). Globus refuses "
        "others with 403 once the endpoint (re)starts",
    )  # fmt: skip
    e.add_argument("--project-dir", default=".", help="aegc source tree (build)")
    e.add_argument(
        "--python",
        default="3.14",
        help="Python in the endpoint image; `test` submits with it (build)",
    )
    e.add_argument("--instance-type", default="t4g.micro", help="deploy: the host (Graviton)")
    e.add_argument("--alarm-email", help="deploy: email when the cloud endpoint is offline")
    e.add_argument("--image", help="deploy: an endpoint image already in ECR (default: build one)")
    e.add_argument("--stop-local", action="store_true", help="deploy: stop the local endpoint")
    e.add_argument(
        "--session-manager", action="store_true",
        help="deploy: allow Session Manager shells on the host (whoever may ssm:StartSession "
        "there gets root, and with it your stored Globus login)",
    )  # fmt: skip
    e.add_argument("--dry-run", action="store_true", help="deploy: show what would change")
    e.add_argument("--yes", action="store_true", help="undeploy: actually remove it")
    e.set_defaults(func=_endpoint)

    ev = sub.add_parser(
        "evaluate",
        help="spend caps, lease, liveness and idle result queues, without an engine (the cloud "
        "endpoint runs this every 5 minutes)",
    )
    ev.add_argument("deployment")
    ev.add_argument("--endpoint-id", help="the endpoint to check with Globus (liveness metric)")
    ev.add_argument("--dry-run", action="store_true", help="change nothing; say what would")
    ev.set_defaults(func=_evaluate)
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
