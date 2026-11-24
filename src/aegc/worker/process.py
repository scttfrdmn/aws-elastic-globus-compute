"""One task message, start to finish: the path EC2 workers and Lambda functions share (#21).

:class:`Processor` does what happens to every message, wherever it runs:

1. :meth:`Processor.accept`: decode, refuse it when the deployment is halted, fail it after too
   many deliveries, fetch the payload and post ``started``. It returns a :class:`Job` to run, or
   a :class:`Handled` saying whether the message should come back (``retry``).
2. :meth:`Processor.run`: run the task in a child process, merge provenance into the result,
   post the result and ack.

It has no threads, and it keeps no state across messages beyond what's passed in. The EC2
agent (:mod:`aegc.worker.agent`) adds claiming, visibility heartbeats, notices and drain; the
Lambda handler (:mod:`aegc.worker.lambda_handler`) adds the SQS event and its remaining time.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Any

from aegc import payloads, protocol
from aegc.gcresult import failed_result
from aegc.protocol import ErrorInfo, ProtocolError, ResultMessage, TaskMessage
from aegc.state import State
from aegc.worker import emf, provenance, runner

log = logging.getLogger("aegc.worker")


@dataclass
class Job:
    """A claimed message whose payload is fetched and whose ``started`` is posted."""

    msg: TaskMessage
    receipt: str
    received_at: float
    receive_count: int
    body: bytes = b""
    queue_wait_s: float = 0.0  # submitted (the engine's clock) to claimed
    released: bool = False  # its message went back to the queue: don't post or ack
    future: Any = None  # the EC2 agent's slot


@dataclass(frozen=True)
class Handled:
    """``accept`` dealt with the message without running it."""

    retry: bool  # True: the message should come back (left for redelivery or released)
    reason: str = ""


@dataclass(frozen=True)
class Finished:
    status: str  # ok, task_error, walltime, crashed, released (what metrics and logs report)
    outcome: str  # the runner's outcome: result, walltime, crashed
    posted: bool  # the result is durably posted (and the message acked)
    timings: dict[str, float]
    provenance: dict[str, Any]


@dataclass
class Processor:
    sqs: Any
    s3: Any
    deployment: str
    bucket: str
    #: Deletes a message once its result is posted.
    ack: Callable[[str], None]
    #: Makes a message visible again now (a payload fetch failed).
    release: Callable[[str], None]
    #: The ``worker`` field of every ResultMessage.
    worker_info: Callable[[], dict[str, Any]]
    #: ``details["aegc"]`` for a finished task: ``(job, timings) -> dict``.
    provenance: Callable[[Job, dict[str, float]], dict[str, Any]]
    emitter: emf.Emitter
    max_deliveries: int = 5
    _first_claim: bool = field(default=False, init=False)

    # --- accept -----------------------------------------------------------------------------

    def accept(
        self,
        m: dict[str, Any],
        *,
        receive_count: int,
        halt: State | None,
        started_at: float | None = None,
        now: float | None = None,
    ) -> Job | Handled:
        """Take one SQS message (``Body`` and ``ReceiptHandle``). ``started_at`` (when this worker
        booted) gives the first claim's ``BootToFirstClaimSeconds``."""
        receipt = str(m.get("ReceiptHandle") or m.get("receiptHandle") or "")
        body = m.get("Body", m.get("body", ""))
        try:
            msg = TaskMessage.decode(body)
        except ProtocolError as e:
            # No usable reply_to: leave it for redelivery and, eventually, the DLQ.
            log.error("undecodable task message %s: %s", m.get("MessageId", m.get("messageId")), e)
            return Handled(retry=True, reason="undecodable")
        now = time.time() if now is None else now
        if not self._first_claim:
            self._first_claim = True
            if started_at is not None:
                self.emitter.emit({"BootToFirstClaimSeconds": max(0.0, now - started_at)})
        log.info(
            "claimed %s (delivery %d)",
            msg.executor_task_id,
            receive_count,
            extra=task_fields(msg, receive_count),
        )
        if halt is not None:
            if self.post_refusal(msg, halt):
                self.ack(receipt)
                return Handled(retry=False, reason="refused")
            return Handled(retry=True, reason="refusal not posted")
        if receive_count > self.max_deliveries:
            self.emitter.emit({"PoisonTasks": 1, "TasksFailed": 1}, outcome="poison")
            self.post_failure(
                msg,
                "WORKER_LOST_REPEATEDLY",
                f"delivered {receive_count} times without completing; not retrying",
                receive_count,
            )
            self.ack(receipt)
            return Handled(retry=False, reason="poison")
        try:
            payload = payloads.read_payload(msg.task, s3=self.s3)
        except payloads.PayloadMissing as e:
            self.post_failure(msg, "PAYLOAD_MISSING", str(e), receive_count)
            self.emitter.emit({"TasksFailed": 1}, outcome="payload_missing")
            self.ack(receipt)
            return Handled(retry=False, reason="payload_missing")
        except Exception:
            log.exception("couldn't fetch payload for %s; releasing", msg.executor_task_id)
            self.release(receipt)
            return Handled(retry=True, reason="payload fetch failed")
        job = Job(
            msg=msg,
            receipt=receipt,
            received_at=now,
            receive_count=receive_count,
            body=payload,
            queue_wait_s=max(0.0, now - msg.submitted_at),  # clocks differ: never negative
        )
        self.emitter.emit({"QueueWaitSeconds": job.queue_wait_s})
        self.post(
            ResultMessage(
                kind="started",
                task_uuid=msg.task_uuid,
                attempt=msg.attempt,
                endpoint_id=msg.endpoint_id,
                worker=self.worker_info(),
                receive_count=receive_count,
            ),
            msg.reply_to,
            best_effort=True,
        )
        return job

    # --- run --------------------------------------------------------------------------------

    def run(
        self,
        job: Job,
        *,
        ctx: BaseContext,
        scratch: Path,
        task_uid: int | None = None,
        task_env: dict[str, str] | None = None,
        walltime_s: int | None = None,
        on_ran: Callable[[runner.RunOutcome, float], None] | None = None,
    ) -> Finished:
        """Run ``job``, post its result and ack it. ``walltime_s`` overrides (never raises) the
        message's walltime: a Lambda function has only so much time left. ``on_ran(outcome,
        exec_s)`` runs as soon as the task has run, before its result is posted."""
        msg = job.msg
        run_dir = scratch / "tasks" / msg.executor_task_id.replace(":", "_")
        limit = msg.walltime_s if walltime_s is None else max(1, min(msg.walltime_s, walltime_s))
        started = time.time()
        try:
            outcome = runner.run_task(
                ctx=ctx,
                task_body=job.body,
                task_uuid=msg.task_uuid,
                endpoint_id=msg.endpoint_id,
                run_dir=run_dir,
                walltime_s=limit,
                run_in_sandbox=msg.call.run_in_sandbox,
                task_deserializers=msg.call.task_deserializers,
                result_serializers=msg.call.result_serializers,
                task_uid=task_uid,
                task_env=task_env or None,
            )
        except Exception as e:
            log.exception("runner failed for %s", msg.executor_task_id)
            outcome = runner.RunOutcome("crashed", None, f"runner error: {e}")
        job.body = b""  # don't hold the payload past the run
        finished = time.time()
        if on_ran is not None and not job.released:
            on_ran(outcome, finished - started)
        timings = {
            "received_at": job.received_at,
            "started_at": started,
            "finished_at": finished,
            "exec_s": finished - started,
            "queue_wait_s": job.queue_wait_s,
        }
        done = Finished(outcome.outcome, outcome.outcome, False, timings, {})
        try:
            done = self._post_outcome(job, outcome, timings)
        except Exception:
            log.exception("couldn't finish %s", msg.executor_task_id)
        finally:
            runner.clean(run_dir, scratch)
            log.info(
                "task finished: %s %s in %.1f s",
                msg.executor_task_id,
                done.status,
                timings["exec_s"],
                extra={
                    "event": "task_finished",
                    **task_fields(msg, job.receive_count),
                    "outcome": done.status,
                    "posted": done.posted,
                    **{k: round(v, 3) for k, v in timings.items()},
                    "provenance": done.provenance,
                },
            )
        return done

    def _post_outcome(
        self, job: Job, outcome: runner.RunOutcome, timings: dict[str, float]
    ) -> Finished:
        msg = job.msg
        prov = self.provenance(job, timings)
        if job.released:  # its message is visible again: another worker will run it
            return Finished("released", outcome.outcome, False, timings, prov)
        # Stopping, but this one finished (a claim can race the stop's release): post it.
        status: str = outcome.outcome
        if outcome.outcome == "crashed":
            self.post_failure(msg, "CHILD_CRASHED", outcome.detail, job.receive_count, timings)
        else:
            assert outcome.result is not None
            packed, is_error = provenance.merge(outcome.result, prov)
            if outcome.outcome == "result":
                status = "task_error" if is_error else "ok"
            result = payloads.make_payload(
                packed,
                s3=self.s3,
                bucket=self.bucket,
                key=result_key(self.deployment, msg),
            )
            if not self.post(
                ResultMessage(
                    kind="result",
                    task_uuid=msg.task_uuid,
                    attempt=msg.attempt,
                    endpoint_id=msg.endpoint_id,
                    result=result,
                    worker=self.worker_info(),
                    timings=timings,
                    receive_count=job.receive_count,
                ),
                msg.reply_to,
            ):
                # Couldn't post: don't ack; redelivery (and the DLQ) keep it.
                return Finished(status, outcome.outcome, False, timings, prov)
        self.ack(job.receipt)
        count = "TasksCompleted" if status in emf.COMPLETED_OUTCOMES else "TasksFailed"
        self.emitter.emit({count: 1, "ExecSeconds": timings["exec_s"]}, outcome=status)
        return Finished(status, outcome.outcome, True, timings, prov)

    # --- posting ----------------------------------------------------------------------------

    def post(self, m: ResultMessage, reply_to: str, *, best_effort: bool = False) -> bool:
        try:
            self.sqs.send_message(QueueUrl=reply_to, MessageBody=m.encode())
            return True
        except Exception as e:
            level = logging.WARNING if best_effort else logging.ERROR
            log.log(level, "couldn't post %s for %s: %s", m.kind, m.executor_task_id, e)
            return False

    def post_failure(
        self,
        msg: TaskMessage,
        code: str,
        message: str,
        receive_count: int,
        timings: dict[str, float] | None = None,
    ) -> bool:
        return self.post(
            ResultMessage(
                kind="system_failure",
                task_uuid=msg.task_uuid,
                attempt=msg.attempt,
                endpoint_id=msg.endpoint_id,
                error=ErrorInfo(code=code, message=message),
                worker=self.worker_info(),
                timings=timings or {},
                receive_count=receive_count,
            ),
            msg.reply_to,
        )

    def post_refusal(self, msg: TaskMessage, st: State) -> bool:
        """Post a final failed Result (never retried) for ``msg``: the deployment is halted."""
        code, text = st.failure_code or "AEGC_STOPPED", st.explain(self.deployment)
        result = payloads.make_payload(
            failed_result(msg.task_uuid, code, text),
            s3=self.s3,
            bucket=self.bucket,
            key=result_key(self.deployment, msg),
        )
        posted = self.post(
            ResultMessage(
                kind="result",
                task_uuid=msg.task_uuid,
                attempt=msg.attempt,
                endpoint_id=msg.endpoint_id,
                result=result,
                worker=self.worker_info(),
            ),
            msg.reply_to,
        )
        if posted:
            self.emitter.emit({"TasksFailed": 1}, outcome="refused")
        return posted


def result_key(deployment: str, msg: TaskMessage) -> str:
    """Where a result too large to send inline goes: the key the engine chose (0.8 and later),
    else the layout before 0.8."""
    return msg.result_key or protocol.result_payload_key(deployment, msg.task_uuid, msg.attempt)


def task_fields(msg: TaskMessage, receive_count: int) -> dict[str, Any]:
    """The per-task fields of a JSON log line."""
    return {
        "task_uuid": msg.task_uuid,
        "attempt": msg.attempt,
        "etid": msg.executor_task_id,
        "receive_count": receive_count,
    }
