"""Run one task in a fresh child process, with a hard walltime.

Each task gets its own process: execute_task changes the working directory and environment
variables, and a crash or leak must not take down the agent. The parent enforces walltime by
killing the child, because GC_TASK_TIMEOUT doesn't interrupt blocking calls (spike #2).

A forkserver preloads globus_compute_endpoint, so each child starts without re-importing it.

Isolated pools (#32): the agent runs as root and each child drops to the task uid before it
deserializes anything, with the task-role credentials in its environment and no instance
metadata. The packed result comes back over a pipe, not a file a neighbouring task could
overwrite.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Literal

from aegc.gcresult import failed_result

PRELOAD = ["globus_compute_endpoint.engines.helper"]
KILL_GRACE_S = 10.0
#: Globus Compute refuses results over 10 MB; a child that sends more is broken or hostile.
MAX_RESULT_BYTES = 32 * 2**20
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


def drop_privileges(uid: int, run_dir: str) -> None:
    """Become ``uid`` for good (and its primary group), with a HOME it can write."""
    import pwd

    try:
        entry: pwd.struct_passwd | None = pwd.getpwuid(uid)
    except KeyError:
        entry = None
    gid = entry.pw_gid if entry else uid
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    home = entry.pw_dir if entry else ""
    os.environ["HOME"] = home if home and os.access(home, os.W_OK) else run_dir
    os.environ["USER"] = entry.pw_name if entry else str(uid)


def _child(
    task_body: bytes,
    task_uuid: str,
    endpoint_id: str,
    run_dir: str,
    conn: Connection,
    run_in_sandbox: bool,
    task_deserializers: list[str] | None,
    result_serializers: list[str] | None,
    walltime_s: int,
    task_uid: int | None = None,
    task_env: dict[str, str] | None = None,
) -> None:
    os.setsid()  # its own process group, so walltime kills reach anything it starts
    # The agent's stdout carries EMF metric lines; task output goes to stderr, so a partial line
    # from a task can't corrupt one (docker logs each stream line by line).
    os.dup2(2, 1)
    if task_uid is not None:
        drop_privileges(task_uid, run_dir)  # before the task body is deserialized
        os.environ["AWS_EC2_METADATA_DISABLED"] = "true"  # it's unreachable; don't wait for it
    os.environ.update(task_env or {})
    os.environ["GC_TASK_TIMEOUT"] = str(walltime_s)
    os.environ["AEGC_TASK_UUID"] = task_uuid  # aegc.outputs keys outputs by task
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
    conn.send_bytes(packed)
    conn.close()


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
    task_uid: int | None = None,
    task_env: dict[str, str] | None = None,
) -> RunOutcome:
    run_dir.mkdir(parents=True, exist_ok=True)
    if task_uid is not None:  # the task's own directory; the agent (root) can still clean it
        os.chown(run_dir, task_uid, -1)
        os.chmod(run_dir, 0o700)
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(  # type: ignore[attr-defined]
        target=_child,
        args=(
            task_body,
            task_uuid,
            endpoint_id,
            str(run_dir),
            send,
            run_in_sandbox,
            task_deserializers,
            result_serializers,
            walltime_s,
            task_uid,
            task_env,
        ),
        daemon=False,
    )
    proc.start()
    send.close()  # only the child writes; EOF when it exits
    packed, timed_out = _receive(recv, proc, walltime_s + SOFT_GRACE_S)
    recv.close()
    if timed_out:
        _kill_group(proc.pid)
        proc.join(KILL_GRACE_S)
        if proc.is_alive():
            proc.kill()
            proc.join()
        msg = f"task exceeded its walltime of {walltime_s} s and was killed"
        return RunOutcome("walltime", failed_result(task_uuid, "AEGC_WALLTIME_EXCEEDED", msg), msg)
    proc.join(KILL_GRACE_S)
    if packed is not None and proc.exitcode is None:
        # It sent its result but won't exit (e.g. a non-daemon thread): keep the result.
        _kill_group(proc.pid)
        proc.join(KILL_GRACE_S)
        if proc.is_alive():
            proc.kill()
            proc.join()
        return RunOutcome("result", packed)
    if packed is not None and proc.exitcode == 0:
        return RunOutcome("result", packed)
    if proc.exitcode is not None and proc.exitcode < 0:
        detail = f"killed by signal {signal.Signals(-proc.exitcode).name}"
    else:
        detail = f"exited with status {proc.exitcode}"
    return RunOutcome("crashed", None, detail)


def _receive(recv: Connection, proc: BaseProcess, timeout_s: float) -> tuple[bytes | None, bool]:
    """``(packed result or None, timed out)``: None if the child exits without a result.

    Read while the child runs: a large result fills the pipe, so waiting for the child to exit
    first would deadlock.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None, True
        if recv.poll(min(left, 1.0)):
            try:
                return recv.recv_bytes(MAX_RESULT_BYTES), False
            except (EOFError, OSError):  # the child exited (or crashed), or sent too much
                return None, False
        if not proc.is_alive() and not recv.poll(0):
            return None, False


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
