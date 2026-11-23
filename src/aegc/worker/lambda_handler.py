"""The Lambda entry point for pools of kind ``lambda`` (#21, #75).

The worker image runs here unchanged: the function's image config sets the command to
``aegc-worker lambda`` (:mod:`aegc.worker.lambda_runtime`), which calls :func:`handler`. An
SQS event source mapping (batch size 1, partial batch responses) delivers task messages; each
record goes through the same path as on EC2 (:mod:`aegc.worker.process`):

- **Deployment state** is read at most every :data:`STATE_TTL_S`: a capped, stopped or expired
  deployment refuses the record with a final result, as an EC2 worker does. (Caps and stops also
  disable the event source mapping, so few records get this far.)
- **Walltime** is the task's, but never past the invocation's remaining time less
  :data:`MARGIN_S`, which is kept for posting the result.
- **Scratch** is ``/tmp/aegc`` (the only writable path).
- **Failures:** a record whose result couldn't be posted (or that couldn't be read) is returned
  in ``batchItemFailures``, so SQS delivers it again; the dead-letter queue catches the rest.
- **Ledger:** one record per execution environment, with its billed seconds and invocations,
  written on its first invocation and then at most every :data:`LEDGER_EVERY_S`.

Tasks run with the function role's credentials: Lambda pools aren't isolated (``isolate`` is
refused for them), and their role holds only the pool's grants.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Any

from aegc import __version__, ledger, pricing
from aegc.state import State, read_state
from aegc.worker import emf
from aegc.worker.config import WorkerConfig
from aegc.worker.process import Handled, Job, Processor

log = logging.getLogger("aegc.worker")

#: Kept from the invocation's remaining time for posting the result.
MARGIN_S = 20
STATE_TTL_S = 60.0
LEDGER_EVERY_S = 30.0
SCRATCH = "/tmp/aegc"

#: When this execution environment's init began (container images are billed for init).
_INIT_AT = time.time()


def environment_id(log_stream: str) -> str:
    """A ledger-safe id for one execution environment, from its log stream name
    (``2026/11/23/[$LATEST]0123abcd...``)."""
    tail = log_stream.rsplit("]", 1)[-1] or log_stream
    return "lambda-" + (re.sub(r"[^A-Za-z0-9-]", "-", tail).strip("-") or "unknown")


@dataclass
class Runtime:
    """What one execution environment keeps between invocations."""

    config: WorkerConfig
    sqs: Any
    s3: Any
    ssm: Any | None
    function: str = ""
    function_version: str = ""
    memory_mib: int = 0
    log_stream: str = ""
    ctx: BaseContext | None = None
    scratch: Path = Path(SCRATCH)
    margin_s: int = MARGIN_S
    clock: Any = time.time
    emitter: emf.Emitter | None = None
    init_at: float = _INIT_AT
    _halt: State | None = field(default=None, init=False)
    _state_at: float = field(default=-1e18, init=False)
    _billed_s: float = field(default=0.0, init=False)
    _invocations: int = field(default=0, init=False)
    _tasks: int = field(default=0, init=False)
    _first_at: float = field(default=0.0, init=False)
    _ledger_at: float = field(default=-1e18, init=False)
    _request_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        from aegc.worker import runner

        self.env_id = environment_id(self.log_stream)
        self._ctx = self.ctx or runner.context()
        self._emf = self.emitter or emf.Emitter(
            self.config.deployment,
            self.config.pool,
            properties={"function": self.function, "environment": self.env_id},
        )
        self.proc = Processor(
            sqs=self.sqs,
            s3=self.s3,
            deployment=self.config.deployment,
            bucket=self.config.bucket,
            ack=self._ack,
            release=self._release,
            worker_info=self.worker_info,
            provenance=self.provenance,
            emitter=self._emf,
            max_deliveries=self.config.max_deliveries,
        )

    # --- one invocation -----------------------------------------------------------------------

    def invoke(self, event: dict[str, Any], context: Any) -> dict[str, Any]:
        entered = self.clock()
        first = self._invocations == 0
        self._invocations += 1
        self._first_at = self._first_at or entered
        self._request_id = str(getattr(context, "aws_request_id", "") or "")
        failures: list[dict[str, str]] = []
        try:
            for rec in event.get("Records", []):
                if not self.handle(rec, context):
                    failures.append({"itemIdentifier": str(rec.get("messageId", ""))})
        finally:
            done = self.clock()
            billed = done - entered
            if first:
                billed += max(0.0, entered - self.init_at)  # init, billed for images
            self._billed_s += round(billed + 0.0005, 3)  # billed per ms, rounded up
            if first or done - self._ledger_at >= LEDGER_EVERY_S:
                self.write_ledger(done)
        return {"batchItemFailures": failures}

    def handle(self, rec: dict[str, Any], context: Any) -> bool:
        """One SQS record; False: report it as a batch item failure."""
        receive_count = int((rec.get("attributes") or {}).get("ApproximateReceiveCount", 1))
        got = self.proc.accept(
            rec,
            receive_count=receive_count,
            halt=self.check_state(),
            started_at=self.init_at,
        )
        if isinstance(got, Handled):
            return not got.retry
        self._tasks += 1
        left_s = _remaining_s(context) - self.margin_s
        if left_s < 1:
            log.warning("no time left to run %s; releasing it", got.msg.executor_task_id)
            self._release(got.receipt)
            return False
        fin = self.proc.run(got, ctx=self._ctx, scratch=self.scratch, walltime_s=int(left_s))
        return fin.posted

    # --- the deployment state -----------------------------------------------------------------

    def check_state(self) -> State | None:
        if self.ssm is None or not self.config.state_parameter:
            return None
        now = self.clock()
        if now - self._state_at < STATE_TTL_S:
            return self._halt
        try:
            st = read_state(self.ssm, self.config.deployment)
        except Exception as e:
            log.warning("couldn't read the deployment state: %s", e)
            return self._halt
        self._state_at = now
        self._halt = None if st.running else st
        return self._halt

    # --- SQS ------------------------------------------------------------------------------------

    def _ack(self, receipt: str) -> None:
        # The event source mapping deletes successful records too; deleting now means a
        # crash after this point can't run the task again.
        try:
            self.sqs.delete_message(QueueUrl=self.config.task_queue_url, ReceiptHandle=receipt)
        except Exception as e:
            log.warning("couldn't delete a task message: %s", e)

    def _release(self, receipt: str) -> None:
        try:
            self.sqs.change_message_visibility(
                QueueUrl=self.config.task_queue_url, ReceiptHandle=receipt, VisibilityTimeout=0
            )
        except Exception as e:
            log.warning("couldn't release a task message: %s", e)

    # --- provenance and the ledger --------------------------------------------------------

    def worker_info(self) -> dict[str, Any]:
        return {
            "instance_id": self.env_id,
            "pool": self.config.pool,
            "agent_version": __version__,
            "instance_type": self.instance_type,
            "backend": "lambda",
            "function": self.function,
        }

    @property
    def instance_type(self) -> str:
        return f"lambda-{self.memory_mib}MB"

    def provenance(self, job: Job, timings: dict[str, float]) -> dict[str, Any]:
        """``details["aegc"]`` (#40) on Lambda: the function and request, not an instance."""
        cost = pricing.lambda_cost(timings["exec_s"], self.memory_mib, 1)
        return {
            "backend": "lambda",
            "image": self.config.image,
            "function": self.function,
            "function_version": self.function_version,
            "memory_mib": self.memory_mib,
            "request_id": self._request_id,
            "log_stream": self.log_stream,
            "instance_id": self.env_id,
            "instance_type": self.instance_type,
            "slots": 1,
            "deployment": self.config.deployment,
            "pool": self.config.pool,
            "aegc_version": __version__,
            "attempt": job.msg.attempt,
            "receive_count": job.receive_count,
            "timings": {k: round(v, 3) for k, v in timings.items()},
            "est_cost_usd": round(cost, 8),
            "est_cost_source": "static",
        }

    def ledger_record(self, now: float) -> ledger.Record:
        return ledger.Record(
            instance_id=self.env_id,
            deployment=self.config.deployment,
            pool=self.config.pool,
            instance_type=self.instance_type,
            availability_zone="",
            lifecycle="lambda",
            vcpus=os.cpu_count() or 0,
            launched_at=self.init_at,
            last_seen=now,
            tasks=self._tasks,
            busy_slot_s=round(self._billed_s, 3),
            kind="lambda",
            memory_mib=self.memory_mib,
            billed_s=round(self._billed_s, 3),
            invocations=self._invocations,
        )

    def write_ledger(self, now: float) -> None:
        if not self.config.ledger:
            return
        try:
            ledger.write(self.s3, self.config.bucket, self.ledger_record(now))
            self._ledger_at = now
        except Exception as e:
            log.warning("couldn't write the cost ledger: %s", e)


def _remaining_s(context: Any) -> float:
    get = getattr(context, "get_remaining_time_in_millis", None)
    return get() / 1000 if callable(get) else 900.0


# --- the entry point -------------------------------------------------------------------------

_runtime: Runtime | None = None


def runtime(env: dict[str, str] | None = None) -> Runtime:
    """This execution environment's :class:`Runtime`, made on first use."""
    global _runtime
    if _runtime is None:
        from aegc import awsutil
        from aegc.worker import jsonlog

        if env is None:
            env = dict(os.environ)
            home = env.get("HOME", "")
            if not home or not os.access(home, os.W_OK):  # read-only outside /tmp
                os.environ["HOME"] = f"{SCRATCH}/home"
                Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
        cfg = WorkerConfig.from_env(env)
        sess = awsutil.session(region=cfg.region)
        rt = Runtime(
            config=cfg,
            sqs=awsutil.client(sess, "sqs"),
            s3=awsutil.client(sess, "s3"),
            ssm=awsutil.client(sess, "ssm") if cfg.state_parameter else None,
            function=env.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            function_version=env.get("AWS_LAMBDA_FUNCTION_VERSION", ""),
            memory_mib=int(env.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "0") or 0),
            log_stream=env.get("AWS_LAMBDA_LOG_STREAM_NAME", ""),
        )
        jsonlog.configure(sys.stderr)
        jsonlog.set_context(
            deployment=cfg.deployment, pool=cfg.pool, instance_id=rt.env_id, function=rt.function
        )
        Path(SCRATCH).mkdir(parents=True, exist_ok=True)
        _runtime = rt
    return _runtime


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """The function's handler: SQS records in, ``batchItemFailures`` out."""
    return runtime().invoke(event, context)
