"""The worker agent: claim → fetch → run → post → ack, with heartbeats, notices and idle drain.

Semantics follow spore-host spawn's task pool:

- **Claim** with a long poll. The message is invisible to other workers for the visibility timeout;
  a heartbeat thread keeps extending it while the task runs.
- **Ack** (DeleteMessage) only after the result is durably posted. A worker that dies before acking
  causes a redelivery: at-least-once, and the engine keeps the first result per task.
- **Poison tasks:** after ``max_deliveries`` receives, post a ``system_failure`` instead of
  running it again.
- **Notices:** a Spot interruption or ASG termination releases in-flight tasks immediately
  (visibility 0) and exits; a rebalance recommendation stops claiming and drains.
- **Idle drain:** with nothing to do for ``idle_timeout_s``, leave the Auto Scaling group with
  TerminateInstanceInAutoScalingGroup(ShouldDecrementDesiredCapacity=True).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Any

from aegc import __version__, payloads, protocol
from aegc.awsutil import error_code
from aegc.protocol import ErrorInfo, ProtocolError, ResultMessage, TaskMessage
from aegc.worker import runner
from aegc.worker.config import WorkerConfig
from aegc.worker.imds import Metadata, NoMetadata, Notice

log = logging.getLogger("aegc.worker")


@dataclass
class InFlight:
    msg: TaskMessage
    receipt: str
    received_at: float
    receive_count: int
    future: Future[Any] | None = None
    released: bool = False


@dataclass
class Stats:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    released: int = 0
    poison: int = 0


@dataclass
class Agent:
    config: WorkerConfig
    sqs: Any
    s3: Any
    autoscaling: Any | None = None
    metadata: Metadata = field(default_factory=NoMetadata)
    ctx: BaseContext | None = None
    clock: Callable[[], float] = time.monotonic
    wait_s: int = 20
    notice_poll_s: float = 5.0
    heartbeat_file_s: float = 30.0
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, InFlight] = {}
        self._stop = threading.Event()  # stop everything now
        self._draining = threading.Event()  # stop claiming; finish in-flight
        self._drain_reason = ""
        self._pool = ThreadPoolExecutor(
            max_workers=self.config.slot_count, thread_name_prefix="aegc-slot"
        )
        self._last_activity = self.clock()
        self._ctx = self.ctx or runner.context()
        self._scratch = Path(self.config.scratch)

    # --- public -----------------------------------------------------------------------------

    def run(self, *, max_cycles: int | None = None) -> str:
        """Run until drained or stopped; returns the reason. ``max_cycles`` is for tests."""
        threads = [
            threading.Thread(target=self._heartbeat_loop, name="aegc-heartbeat", daemon=True),
            threading.Thread(target=self._notice_loop, name="aegc-notices", daemon=True),
        ]
        for t in threads:
            t.start()
        cycles = 0
        try:
            while not self._stop.is_set():
                if self._draining.is_set():
                    if not self._busy():
                        break
                    self._stop.wait(0.2)
                    continue
                self.claim_once()
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                idle = self._idle_for() >= self.config.idle_timeout_s and not self._busy()
                if idle and self._leave_group():
                    self._drain("idle")
        finally:
            self._pool.shutdown(wait=not self._stop.is_set())
            self._stop.set()
        return self._drain_reason or "stopped"

    def stop(self, reason: str = "stopped") -> None:
        """Release in-flight tasks for other workers and stop now (SIGTERM, interruption)."""
        self._drain_reason = self._drain_reason or reason
        self._release_all()
        self._stop.set()

    def claim_once(self) -> int:
        free = self.config.slot_count - self._inflight_count()
        if free <= 0:
            self._stop.wait(0.2)
            return 0
        resp = self.sqs.receive_message(
            QueueUrl=self.config.task_queue_url,
            MaxNumberOfMessages=min(free, 10),
            WaitTimeSeconds=self.wait_s,
            VisibilityTimeout=self.config.visibility_s,
            AttributeNames=["ApproximateReceiveCount"],
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        messages = resp.get("Messages", [])
        for m in messages:
            self._accept(m)
        return len(messages)

    def wait_idle(self, timeout_s: float = 30) -> None:
        """Block until no task is in flight (tests)."""
        deadline = time.monotonic() + timeout_s
        while self._busy():
            if time.monotonic() > deadline:
                raise TimeoutError("tasks still in flight")
            time.sleep(0.05)

    # --- claim → run → post -----------------------------------------------------------------

    def _accept(self, m: dict[str, Any]) -> None:
        receipt = m["ReceiptHandle"]
        receive_count = int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1))
        try:
            msg = TaskMessage.decode(m["Body"])
        except ProtocolError as e:
            # No usable reply_to: leave it for redelivery and, eventually, the DLQ.
            log.error("undecodable task message %s: %s", m.get("MessageId"), e)
            return
        self.stats.claimed += 1
        self._touch()
        if receive_count > self.config.max_deliveries:
            self.stats.poison += 1
            self._post_failure(
                msg,
                "WORKER_LOST_REPEATEDLY",
                f"delivered {receive_count} times without completing; not retrying",
                receive_count,
            )
            self._ack(receipt)
            return
        try:
            body = payloads.read_payload(msg.task, s3=self.s3)
        except payloads.PayloadMissing as e:
            self._post_failure(msg, "PAYLOAD_MISSING", str(e), receive_count)
            self._ack(receipt)
            return
        except Exception:
            log.exception("couldn't fetch payload for %s; releasing", msg.executor_task_id)
            self._change_visibility(receipt, 0)
            return
        job = InFlight(
            msg=msg, receipt=receipt, received_at=time.time(), receive_count=receive_count
        )
        with self._lock:
            self._inflight[receipt] = job
        self._post(
            ResultMessage(
                kind="started",
                task_uuid=msg.task_uuid,
                attempt=msg.attempt,
                endpoint_id=msg.endpoint_id,
                worker=self._worker_info(),
                receive_count=receive_count,
            ),
            msg.reply_to,
            best_effort=True,
        )
        job.future = self._pool.submit(self._run, job, body)

    def _run(self, job: InFlight, body: bytes) -> None:
        msg = job.msg
        run_dir = self._scratch / "tasks" / msg.executor_task_id.replace(":", "_")
        started = time.time()
        try:
            outcome = runner.run_task(
                ctx=self._ctx,
                task_body=body,
                task_uuid=msg.task_uuid,
                endpoint_id=msg.endpoint_id,
                run_dir=run_dir,
                walltime_s=msg.walltime_s,
                run_in_sandbox=msg.call.run_in_sandbox,
                task_deserializers=msg.call.task_deserializers,
                result_serializers=msg.call.result_serializers,
            )
        except Exception as e:
            log.exception("runner failed for %s", msg.executor_task_id)
            outcome = runner.RunOutcome("crashed", None, f"runner error: {e}")
        finished = time.time()
        try:
            if job.released or self._stop.is_set():
                return  # another worker will run it
            timings = {
                "received_at": job.received_at,
                "started_at": started,
                "finished_at": finished,
                "exec_s": finished - started,
            }
            if outcome.outcome == "crashed":
                self.stats.failed += 1
                self._post_failure(msg, "CHILD_CRASHED", outcome.detail, job.receive_count, timings)
            else:
                assert outcome.result is not None
                if outcome.outcome == "walltime":
                    self.stats.failed += 1
                else:
                    self.stats.completed += 1
                result = payloads.make_payload(
                    outcome.result,
                    s3=self.s3,
                    bucket=self.config.bucket,
                    key=protocol.result_payload_key(
                        self.config.deployment, msg.task_uuid, msg.attempt
                    ),
                )
                if not self._post(
                    ResultMessage(
                        kind="result",
                        task_uuid=msg.task_uuid,
                        attempt=msg.attempt,
                        endpoint_id=msg.endpoint_id,
                        result=result,
                        worker=self._worker_info(),
                        timings=timings,
                        receive_count=job.receive_count,
                    ),
                    msg.reply_to,
                ):
                    return  # couldn't post: don't ack; redelivery (and the DLQ) keep it
            self._ack(job.receipt)
        finally:
            with self._lock:
                self._inflight.pop(job.receipt, None)
            runner.clean(run_dir, self._scratch)
            self._touch()

    # --- SQS --------------------------------------------------------------------------------

    def _post(self, m: ResultMessage, reply_to: str, *, best_effort: bool = False) -> bool:
        try:
            self.sqs.send_message(QueueUrl=reply_to, MessageBody=m.encode())
            return True
        except Exception as e:
            level = logging.WARNING if best_effort else logging.ERROR
            log.log(level, "couldn't post %s for %s: %s", m.kind, m.executor_task_id, e)
            return False

    def _post_failure(
        self,
        msg: TaskMessage,
        code: str,
        message: str,
        receive_count: int,
        timings: dict[str, float] | None = None,
    ) -> None:
        self._post(
            ResultMessage(
                kind="system_failure",
                task_uuid=msg.task_uuid,
                attempt=msg.attempt,
                endpoint_id=msg.endpoint_id,
                error=ErrorInfo(code=code, message=message),
                worker=self._worker_info(),
                timings=timings or {},
                receive_count=receive_count,
            ),
            msg.reply_to,
        )

    def _ack(self, receipt: str) -> None:
        try:
            self.sqs.delete_message(QueueUrl=self.config.task_queue_url, ReceiptHandle=receipt)
        except Exception as e:
            log.warning("couldn't delete a task message: %s", e)

    def _change_visibility(self, receipt: str, seconds: int) -> None:
        try:
            self.sqs.change_message_visibility(
                QueueUrl=self.config.task_queue_url,
                ReceiptHandle=receipt,
                VisibilityTimeout=seconds,
            )
        except Exception as e:
            log.warning("couldn't change visibility: %s", e)

    def extend_visibility(self) -> int:
        """Extend every in-flight claim by the visibility timeout; returns how many."""
        with self._lock:
            receipts = [r for r, j in self._inflight.items() if not j.released]
        for i in range(0, len(receipts), 10):
            batch = receipts[i : i + 10]
            try:
                resp = self.sqs.change_message_visibility_batch(
                    QueueUrl=self.config.task_queue_url,
                    Entries=[
                        {
                            "Id": str(n),
                            "ReceiptHandle": r,
                            "VisibilityTimeout": self.config.visibility_s,
                        }
                        for n, r in enumerate(batch)
                    ],
                )
                for f in resp.get("Failed", []):
                    log.warning("visibility extension failed: %s", f)
            except Exception as e:
                log.warning("visibility heartbeat failed: %s", e)
        return len(receipts)

    def _release_all(self) -> None:
        with self._lock:
            jobs = [j for j in self._inflight.values() if not j.released]
            for j in jobs:
                j.released = True
        for j in jobs:
            self._change_visibility(j.receipt, 0)
            self.stats.released += 1

    # --- background loops -------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        hb = self._scratch / ".aegc-heartbeat"
        next_file = 0.0
        while not self._stop.wait(min(self.config.heartbeat_s, self.heartbeat_file_s)):
            self.extend_visibility()
            now = time.monotonic()
            if now >= next_file:
                try:
                    hb.parent.mkdir(parents=True, exist_ok=True)
                    hb.touch()
                except OSError as e:
                    log.warning("couldn't touch heartbeat file: %s", e)
                next_file = now + self.heartbeat_file_s

    def _notice_loop(self) -> None:
        while not self._stop.wait(self.notice_poll_s):
            try:
                notice = self.metadata.notice()
            except Exception as e:
                log.debug("metadata poll failed: %s", e)
                continue
            if notice:
                self.handle_notice(notice)

    def handle_notice(self, notice: Notice) -> None:
        if notice == "rebalance":
            log.warning("rebalance recommendation: finishing in-flight tasks, then draining")
            self._drain("rebalance")
        else:
            log.warning("%s: releasing in-flight tasks and exiting", notice)
            self.stop(notice)

    # --- drain ------------------------------------------------------------------------------

    def _drain(self, reason: str) -> None:
        self._drain_reason = self._drain_reason or reason
        self._draining.set()

    def _leave_group(self) -> bool:
        """Terminate this instance through Auto Scaling, decrementing desired capacity."""
        if self.autoscaling is None or not self.config.instance_id:
            return True  # not on an Auto Scaling instance (local runs, tests)
        try:
            self.autoscaling.terminate_instance_in_auto_scaling_group(
                InstanceId=self.config.instance_id, ShouldDecrementDesiredCapacity=True
            )
            log.info("idle for %d s; leaving the group", self.config.idle_timeout_s)
            return True
        except Exception as e:
            if error_code(e) == "ValidationError":
                # At the group's minimum size: stay up, and wait another idle period.
                self._touch()
                return False
            log.warning("couldn't leave the group: %s", e)
            self._touch()
            return False

    # --- helpers ----------------------------------------------------------------------------

    def _busy(self) -> bool:
        return self._inflight_count() > 0

    def _inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def _touch(self) -> None:
        self._last_activity = self.clock()

    def _idle_for(self) -> float:
        return self.clock() - self._last_activity

    def _worker_info(self) -> dict[str, Any]:
        return {
            "instance_id": self.config.instance_id,
            "pool": self.config.pool,
            "agent_version": __version__,
        }
