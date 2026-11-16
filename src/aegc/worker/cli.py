"""``aegc-worker run`` (on instances) and ``aegc-worker selftest`` (at image build time)."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import tempfile
import uuid
from pathlib import Path
from types import FrameType


def _run(_: argparse.Namespace) -> int:
    from dataclasses import replace

    from aegc import awsutil
    from aegc.worker.agent import Agent
    from aegc.worker.config import WorkerConfig
    from aegc.worker.imds import Imds

    cfg = WorkerConfig.from_env()
    imds = Imds()
    try:
        ident = imds.identity()
        cfg = replace(
            cfg,
            instance_type=ident.get("instance_type", ""),
            availability_zone=ident.get("availability_zone", ""),
            lifecycle=ident.get("lifecycle", ""),
        )
    except Exception as e:  # off EC2, or IMDS unreachable: the ledger records less
        logging.getLogger("aegc").warning("couldn't read instance identity: %s", e)
    sess = awsutil.session(region=cfg.region)
    task_env: dict[str, str] = {}
    creds = None
    if cfg.task_role_arn:  # isolated pool (#32): tasks get the task role, not the worker role
        from aegc.worker.credentials import TaskCredentials

        creds = TaskCredentials(
            sts=awsutil.client(sess, "sts"),
            role_arn=cfg.task_role_arn,
            session_name=f"aegc-{cfg.instance_id or 'worker'}",
            token_file=Path(cfg.state_dir or "/run/aegc") / "task-token",
        )
        task_env = creds.start()
    agent = Agent(
        config=cfg,
        sqs=awsutil.client(sess, "sqs"),
        s3=awsutil.client(sess, "s3"),
        autoscaling=awsutil.client(sess, "autoscaling"),
        ssm=awsutil.client(sess, "ssm") if cfg.state_parameter else None,
        metadata=imds,
        task_env=task_env,
    )

    def on_term(signum: int, _frame: FrameType | None) -> None:
        agent.stop(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, on_term)
    # SIGUSR1 simulates a Spot interruption, for testing.
    signal.signal(signal.SIGUSR1, lambda *_: agent.handle_notice("interruption"))
    logging.getLogger("aegc").info(
        "aegc-worker: pool %s, %d slots, queue %s", cfg.pool, cfg.slot_count, cfg.task_queue_url
    )
    try:
        reason = agent.run()
    finally:
        if creds is not None:
            creds.stop()
    logging.getLogger("aegc").info("aegc-worker: exiting (%s): %s", reason, agent.stats)
    return 0


def _selftest(_: argparse.Namespace) -> int:
    """Run a real packed task through execute_task in this image's Python."""
    from globus_compute_common import messagepack

    from aegc.worker import runner
    from aegc.worker.testing import pack_task

    def probe() -> str:
        import platform

        return f"ok {platform.python_version()}"

    task_uuid = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as d:
        out = runner.run_task(
            ctx=runner.context(),
            task_body=pack_task(probe, task_uuid=task_uuid),
            task_uuid=task_uuid,
            endpoint_id=str(uuid.uuid4()),
            run_dir=Path(d) / "tasks" / "selftest",
            walltime_s=60,
        )
    if out.outcome != "result" or out.result is None:
        print(f"selftest failed: {out}", file=sys.stderr)
        return 1
    from globus_compute_common.messagepack.message_types import Result

    result = messagepack.unpack(out.result)
    if not isinstance(result, Result):
        print("selftest failed: not a Result", file=sys.stderr)
        return 1
    if result.is_error:
        print(f"selftest task errored: {result.data}", file=sys.stderr)
        return 1
    print("aegc-worker selftest: ok")
    return 0


def _version(_: argparse.Namespace) -> int:
    """What ``aegc image register`` checks, as JSON on one line."""
    import os
    import platform
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    import aegc

    try:
        gce = dist_version("globus-compute-endpoint")
    except PackageNotFoundError:
        gce = ""
    info = {
        "aegc": aegc.__version__,
        "gce": gce,
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "python_full": platform.python_version(),
        "executable": sys.executable,
        "uid": os.getuid(),
    }
    print(json.dumps(info, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="aegc-worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="claim and run tasks until drained").set_defaults(func=_run)
    sub.add_parser("selftest", help="run a probe task through execute_task").set_defaults(
        func=_selftest
    )
    sub.add_parser("version", help="aegc, Python and uid, as JSON").set_defaults(func=_version)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
