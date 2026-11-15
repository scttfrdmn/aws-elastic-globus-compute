"""The worker agent: claim → fetch → run → post → ack, with heartbeats, notices and idle drain.

Semantics follow spore-host spawn's task pool:

- **Claim** with a long poll. The message is invisible to other workers for the visibility timeout;
  a heartbeat thread keeps extending it while the task runs.
- **Ack** (DeleteMessage) only after the result is durably posted. A worker that dies before acking
  causes a redelivery: at-least-once, and the engine keeps the first result per task.
- **Poison tasks:** after ``max_deliveries`` receives, post a ``system_failure`` instead of
  running it again.
- **Notices:** a Spot interruption or ASG termination releases in-flight tasks immediately
  (visibility 0) and exits. A rebalance recommendation is only logged: with capacity rebalance on,
  Auto Scaling launches a replacement and then terminates this instance (SIGTERM releases
  in-flight tasks). Draining here would leave an instance with no worker in the group.
- **Idle drain:** with nothing to do for ``idle_timeout_s``, leave the Auto Scaling group with
  TerminateInstanceInAutoScalingGroup(ShouldDecrementDesiredCapacity=True).
- **Deployment state:** every ``state_poll_s`` the worker reads ``/aegc/<d>/state``. When it isn't
  running (capped, stopped, expired) the worker stops claiming, fails any task it still receives
  with a clear, non-retried result (AEGC_BUDGET_EXCEEDED / AEGC_STOPPED / AEGC_EXPIRED), finishes
  in-flight tasks (or fails them at once if the state says ``kill``), then leaves the group.
- **Lifetime:** after ``max_lifetime_s`` the worker stops claiming and leaves the group once idle.
- **Ledger:** the worker writes its cost-ledger record at start, every 5 minutes, and at exit.
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

from aegc import __version__, ledger, payloads, protocol
from aegc.awsutil import error_code
from aegc.gcresult import failed_result
from aegc.protocol import ErrorInfo, ProtocolError, ResultMessage, TaskMessage
from aegc.state import State, read_state
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
    ssm: Any | None = None  # reads the deployment state; None: don't watch it
    metadata: Metadata = field(default_factory=NoMetadata)
    ctx: BaseContext | None = None
    clock: Callable[[], float] = time.monotonic
    wait_s: int = 20
    notice_poll_s: float = 5.0
    heartbeat_file_s: float = 30.0
    #: The host watchdog's heartbeat file is touched only while the claim loop is making progress:
    #: a loop pass takes at most one long poll plus botocore's retries (5 x (10 s + 30 s)).
    loop_stale_s: float = 300.0
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, InFlight] = {}
        self._stop = threading.Event()  # stop everything now
        self._draining = threading.Event()  # stop claiming; finish in-flight
        self._drain_reason = ""
        self._rebalance_seen = False
        self._loop_tick = time.monotonic()
        self._loop_stale_logged = False
        self._pool = ThreadPoolExecutor(
            max_workers=self.config.slot_count, thread_name_prefix="aegc-slot"
        )
        self._last_activity = self.clock()
        self._ctx = self.ctx or runner.context()
        self._scratch = Path(self.config.scratch)
        self._halt: State | None = None  # set when the deployment state isn't running
        self._state_checked = -1e18
        self._started_at = _boot_time()
        self._started_mono = time.monotonic()
        self._busy_slot_s = 0.0
        self._ledger_written = 0.0
        self._exit_reason = ""

    # --- public -----------------------------------------------------------------------------

    def run(self, *, max_cycles: int | None = None) -> str:
        """Run until drained or stopped; returns the reason. ``max_cycles`` is for tests."""
        threads = [
            threading.Thread(target=self._heartbeat_loop, name="aegc-heartbeat", daemon=True),
            threading.Thread(target=self._notice_loop, name="aegc-notices", daemon=True),
        ]
        if self.ssm is not None and self.config.state_parameter:
            threads.append(
                threading.Thread(target=self._state_loop, name="aegc-state", daemon=True)
            )
        if self.ssm is not None and self.config.state_parameter:
            self.check_state()  # before the first claim: a new worker mustn't run into a halt
        for t in threads:
            t.start()
        self.write_ledger()
        cycles = 0
        try:
            while not self._stop.is_set():
                self._loop_tick = time.monotonic()
                if self._draining.is_set():
                    if not self._busy():
                        break
                    self._stop.wait(0.2)
                    continue
                if self._halt is not None:
                    self.fail_queued_once(self._halt)
                    if not self._busy() and self._leave_group():
                        self._drain(self._halt.mode)
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    continue
                if self._lifetime_over():
                    if not self._busy() and self._leave_group(replace_at_minimum=True):
                        self._drain("lifetime")
                    self._stop.wait(0.2)
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
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
            self._exit_reason = self._drain_reason or "stopped"
            self.write_ledger(final=True)
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
        log.info("claimed %s (delivery %d)", msg.executor_task_id, receive_count)
        if self._halt is not None:
            if self._post_refusal(msg, self._halt):
                self._ack(receipt)
            return
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
        self._busy_slot_s += finished - started
        log.info("%s: %s in %.1f s", msg.executor_task_id, outcome.outcome, finished - started)
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
                self._touch_heartbeat_file(hb, now)
                next_file = now + self.heartbeat_file_s
            if now - self._ledger_written >= ledger.WRITE_EVERY_S:
                self.write_ledger()

    def _touch_heartbeat_file(self, hb: Path, now: float) -> bool:
        """Tell the host watchdog we're alive, but only if the claim loop is.

        This thread keeps running if the claim loop wedges; touching the file regardless would hide
        the wedge from the watchdog and keep the instance (and its cost) forever.
        """
        if now - self._loop_tick > self.loop_stale_s:
            if not self._loop_stale_logged:
                log.error(
                    "claim loop hasn't run for %d s; letting the watchdog replace this instance",
                    now - self._loop_tick,
                )
                self._loop_stale_logged = True
            return False
        self._loop_stale_logged = False
        try:
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.touch()
            return True
        except OSError as e:
            log.warning("couldn't touch heartbeat file: %s", e)
            return False

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
            if not self._rebalance_seen:
                self._rebalance_seen = True
                log.info("rebalance recommendation: Auto Scaling will replace this instance")
        else:
            log.warning("%s: releasing in-flight tasks and exiting", notice)
            self.stop(notice)

    # --- deployment state ---------------------------------------------------------------

    def _state_loop(self) -> None:
        while not self._stop.wait(self.config.state_poll_s):
            self.check_state()

    def check_state(self) -> State | None:
        """Read the deployment state; on a non-running state, halt. Returns the halt state."""
        assert self.ssm is not None
        self._state_checked = time.monotonic()
        try:
            st = read_state(self.ssm, self.config.deployment)
        except Exception as e:
            log.warning("couldn't read the deployment state: %s", e)
            return self._halt
        if st.running:
            if self._halt is not None:
                log.info("deployment is running again")
            self._halt = None
            return None
        if self._halt is None or self._halt.mode != st.mode or (st.kill and not self._halt.kill):
            log.warning("deployment %s (%s): stop claiming", st.mode, st.reason)
            if st.kill:
                self._fail_inflight(st)
        self._halt = st
        return st

    def _refusal(self, st: State) -> tuple[str, str]:
        return st.failure_code or "AEGC_STOPPED", st.explain(self.config.deployment)

    def _post_refusal(self, msg: TaskMessage, st: State) -> bool:
        """Post a final failed Result (never retried) for ``msg``."""
        code, text = self._refusal(st)
        packed = failed_result(msg.task_uuid, code, text)
        result = payloads.make_payload(
            packed,
            s3=self.s3,
            bucket=self.config.bucket,
            key=protocol.result_payload_key(self.config.deployment, msg.task_uuid, msg.attempt),
        )
        return self._post(
            ResultMessage(
                kind="result",
                task_uuid=msg.task_uuid,
                attempt=msg.attempt,
                endpoint_id=msg.endpoint_id,
                result=result,
                worker=self._worker_info(),
            ),
            msg.reply_to,
        )

    def fail_queued_once(self, st: State) -> int:
        """Receive queued tasks and fail each with a clear result. Returns how many."""
        # Re-read before refusing anything: a resume shouldn't cost freshly queued tasks.
        stale = self.ssm is not None and time.monotonic() - self._state_checked > 15
        if stale and self.check_state() is None:
            return 0
        resp = self.sqs.receive_message(
            QueueUrl=self.config.task_queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=min(self.wait_s, 1),
            VisibilityTimeout=self.config.visibility_s,
        )
        n = 0
        for m in resp.get("Messages", []):
            try:
                msg = TaskMessage.decode(m["Body"])
            except ProtocolError:
                continue  # no reply_to: redelivery and the DLQ take it
            if self._post_refusal(msg, st):
                self._ack(m["ReceiptHandle"])
                n += 1
        if n:
            log.info("failed %d queued task(s): deployment %s", n, st.mode)
        return n

    def _fail_inflight(self, st: State) -> None:
        """Fail running tasks now (stop --hard, on_cap=kill); their results are discarded."""
        with self._lock:
            jobs = [j for j in self._inflight.values() if not j.released]
            for j in jobs:
                j.released = True
        for j in jobs:
            if self._post_refusal(j.msg, st):
                self._ack(j.receipt)
        if jobs:
            log.warning("failed %d running task(s): deployment %s", len(jobs), st.mode)
        # The children keep running until the instance goes; leaving the group ends them.
        with self._lock:
            for j in jobs:
                self._inflight.pop(j.receipt, None)

    def _lifetime_over(self) -> bool:
        return time.monotonic() - self._started_mono >= self.config.max_lifetime_s

    # --- cost ledger -------------------------------------------------------------------------

    def ledger_record(self) -> ledger.Record:
        return ledger.Record(
            instance_id=self.config.instance_id or "local",
            deployment=self.config.deployment,
            pool=self.config.pool,
            instance_type=self.config.instance_type,
            availability_zone=self.config.availability_zone,
            lifecycle=self.config.lifecycle or "spot",
            vcpus=self.config.slot_count if self.config.slots == "auto" else 0,
            launched_at=self._started_at,
            last_seen=time.time(),
            root_gib=self.config.root_volume_gib,
            throughput_mibps=self.config.throughput_mibps,
            tasks=self.stats.claimed,
            busy_slot_s=round(self._busy_slot_s, 1),
            exit_reason=self._exit_reason,
        )

    def write_ledger(self, *, final: bool = False) -> None:
        if not self.config.ledger or not self.config.instance_id:
            return
        try:
            ledger.write(self.s3, self.config.bucket, self.ledger_record())
            self._ledger_written = time.monotonic()
        except Exception as e:
            log.warning("couldn't write the cost ledger%s: %s", " (final)" if final else "", e)

    # --- drain ------------------------------------------------------------------------------

    def _drain(self, reason: str) -> None:
        self._drain_reason = self._drain_reason or reason
        self._draining.set()

    def _leave_group(self, *, replace_at_minimum: bool = False) -> bool:
        """Terminate this instance through Auto Scaling, decrementing desired capacity.

        At the group's minimum the decrement is refused. An idle worker then stays up; one past
        its lifetime (``replace_at_minimum``) is terminated without the decrement, so the group
        replaces it with a fresh instance.
        """
        if self.autoscaling is None or not self.config.instance_id:
            return True  # not on an Auto Scaling instance (local runs, tests)
        try:
            self.autoscaling.terminate_instance_in_auto_scaling_group(
                InstanceId=self.config.instance_id, ShouldDecrementDesiredCapacity=True
            )
            log.info("leaving the group")
            return True
        except Exception as e:
            if error_code(e) == "ValidationError":
                if replace_at_minimum:
                    try:
                        self.autoscaling.terminate_instance_in_auto_scaling_group(
                            InstanceId=self.config.instance_id,
                            ShouldDecrementDesiredCapacity=False,
                        )
                        log.info("at the group's minimum: terminating for a replacement")
                        return True
                    except Exception as e2:
                        log.warning("couldn't terminate for a replacement: %s", e2)
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
            "instance_type": self.config.instance_type,
            "availability_zone": self.config.availability_zone,
            "lifecycle": self.config.lifecycle,
        }


def _boot_time() -> float:
    """When this machine booted (so the cost ledger includes boot and image pull); now, if
    unknown."""
    try:
        with open("/proc/uptime") as f:
            return time.time() - float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return time.time()
