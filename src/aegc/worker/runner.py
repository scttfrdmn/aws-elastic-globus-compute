"""Run one task in a fresh child process, with a hard walltime.

Each task gets its own process: execute_task changes the working directory and environment
variables, and a crash or leak must not take down the agent. The parent enforces walltime by
killing the child, because GC_TASK_TIMEOUT doesn't interrupt blocking calls (spike #2).

A forkserver preloads globus_compute_endpoint, so each child starts without re-importing it.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import signal
import uuid
from dataclasses import dataclass
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Literal

from aegc.gcresult import failed_result

PRELOAD = ["globus_compute_endpoint.engines.helper"]
KILL_GRACE_S = 10.0
#: Let GC_TASK_TIMEOUT report a clean AppTimeout for pure-Python work before the hard kill.
SOFT_GRACE_S = 5.0

Outcome = Literal["result", "crashed", "walltime"]


@dataclass(frozen=True)
class RunOutcome:
    outcome: Outcome
    result: bytes | None = None  # packed Result bytes (for "result" and "walltime")
    detail: str = ""


def context() -> BaseContext:
    try:
        ctx = mp.get_context("forkserver")
        ctx.set_forkserver_preload(PRELOAD)
        return ctx
    except ValueError:  # pragma: no cover - platforms without forkserver
        return mp.get_context("spawn")


def _child(
    task_body: bytes,
    task_uuid: str,
    endpoint_id: str,
    run_dir: str,
    out_path: str,
    run_in_sandbox: bool,
    task_deserializers: list[str] | None,
    result_serializers: list[str] | None,
    walltime_s: int,
) -> None:
    os.setsid()  # its own process group, so walltime kills reach anything it starts
    os.environ["GC_TASK_TIMEOUT"] = str(walltime_s)
    from globus_compute_endpoint.engines.helper import execute_task

    packed = execute_task(
        task_body,
        uuid.UUID(task_uuid),
        uuid.UUID(endpoint_id),
        run_dir=run_dir,
        run_in_sandbox=run_in_sandbox,
        task_deserializers=task_deserializers,
        result_serializers=result_serializers,
    )
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(packed)
    os.replace(tmp, out_path)


def run_task(
    *,
    ctx: BaseContext,
    task_body: bytes,
    task_uuid: str,
    endpoint_id: str,
    run_dir: Path,
    walltime_s: int,
    run_in_sandbox: bool = False,
    task_deserializers: list[str] | None = None,
    result_serializers: list[str] | None = None,
) -> RunOutcome:
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / ".aegc-result"
    proc = ctx.Process(  # type: ignore[attr-defined]
        target=_child,
        args=(
            task_body,
            task_uuid,
            endpoint_id,
            str(run_dir),
            str(out_path),
            run_in_sandbox,
            task_deserializers,
            result_serializers,
            walltime_s,
        ),
        daemon=False,
    )
    proc.start()
    proc.join(walltime_s + SOFT_GRACE_S)
    if proc.is_alive():
        _kill_group(proc.pid)
        proc.join(KILL_GRACE_S)
        if proc.is_alive():
            proc.kill()
            proc.join()
        msg = f"task exceeded its walltime of {walltime_s} s and was killed"
        return RunOutcome("walltime", failed_result(task_uuid, "AEGC_WALLTIME_EXCEEDED", msg), msg)
    if proc.exitcode == 0 and out_path.exists():
        return RunOutcome("result", out_path.read_bytes())
    if proc.exitcode is not None and proc.exitcode < 0:
        detail = f"killed by signal {signal.Signals(-proc.exitcode).name}"
    else:
        detail = f"exited with status {proc.exitcode}"
    return RunOutcome("crashed", None, detail)


def _kill_group(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:  # pragma: no cover
        os.kill(pid, signal.SIGTERM)


def clean(run_dir: Path, scratch: Path) -> None:
    """Remove a task's run directory, but only if it's inside the scratch tasks directory."""
    root = (scratch / "tasks").resolve()
    target = run_dir.resolve()
    if target != root and root in target.parents:
        shutil.rmtree(target, ignore_errors=True)
