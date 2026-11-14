"""AWSEngine: a Globus Compute endpoint engine that runs tasks on ephemeral AWS capacity.

The endpoint's interchange calls :meth:`submit` (from the base class), which calls :meth:`_submit`
here. ``_submit`` sends the task to its pool's SQS queue *before returning*: the interchange acks
the task to Globus as soon as ``submit`` returns. Workers post results to this user endpoint
process's own result queue; a poller thread resolves the matching futures.

Properties the interchange relies on:

- ``start`` is fast and never raises. Missing configuration fails tasks, not the process.
- ``get_status_report`` reads a cached snapshot and never raises.
- ``shutdown`` only stops threads. Queues, capacity and in-flight tasks outlive this process; a
  result that arrives for a task this process doesn't know is written to
  ``<run_dir>/unacked_results/<task_uuid>``, which the interchange republishes.
- The scaler only scales out. Several processes may share a pool; workers scale in themselves.
"""

from __future__ import annotations

import collections
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from globus_compute_common.messagepack.message_types import EPStatusReport
from globus_compute_endpoint.engines.base import GlobusComputeEngineBase

from aegc import __version__, awsutil, naming, payloads, protocol
from aegc.gcresult import failed_result
from aegc.protocol import Call, ProtocolError, ResultMessage, TaskMessage

log = logging.getLogger("aegc.engine")

#: resource_specification keys AWSEngine understands in v0.1.
RESOURCE_KEYS = frozenset({"env", "pool", "walltime_s"})
COMPLETED_LRU = 10_000
STALL_AFTER_S = 15 * 60
STALL_PAUSE_S = 10 * 60


class WorkerSystemFailure(RuntimeError):
    """A worker reported that it couldn't run a task (crash, poison, lost payload)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class _Inflight:
    future: Future[Any]
    task_uuid: str
    attempt: int
    pool: str
    submitted: float
    task_payload: protocol.Payload | None = None


@dataclass
class _PoolMetrics:
    visible: int = 0
    inflight: int = 0
    dlq: int = 0
    desired: int = 0
    in_service: int = 0
    min: int = 0
    max: int = 0
    slots: int = 1
    stalled: bool = False
    last_scaling_error: str = ""


@dataclass
class _Snapshot:
    at: float = 0.0
    pools: dict[str, _PoolMetrics] = field(default_factory=dict)
    last_aws_error: str = ""


class AWSEngine(GlobusComputeEngineBase):  # type: ignore[misc]
    """Run Globus Compute tasks on an aegc deployment (see ``aegc deploy``)."""

    def __init__(
        self,
        *args: Any,
        deployment: str = "default",
        region: str = "",
        env: str = "",
        python_minor: str = "",
        aws_profile: str = "",
        scale_interval_s: float = 10.0,
        result_wait_s: int = 20,
        inline_limit_bytes: int = protocol.DEFAULT_INLINE_LIMIT,
        label: str = "AWSEngine",
        max_retries_on_system_failure: int = 1,
        **kwargs: Any,
    ) -> None:
        # Every argument is stored under its own name: RepresentationMixin requires it.
        self.deployment = deployment
        self.region = region
        self.env = env
        self.python_minor = python_minor
        self.aws_profile = aws_profile
        self.scale_interval_s = scale_interval_s
        self.result_wait_s = result_wait_s
        self.inline_limit_bytes = inline_limit_bytes
        self.label = label
        super().__init__(
            *args, max_retries_on_system_failure=max_retries_on_system_failure, **kwargs
        )
        self._lock = threading.Lock()
        self._inflight: dict[str, _Inflight] = {}  # executor_task_id -> attempt
        self._attempts: collections.Counter[str] = collections.Counter()
        self._completed: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._stop = threading.Event()
        self._scale_now = threading.Event()
        self.first_scale_pass = threading.Event()  # set after the scaler's first pass
        self._threads: list[threading.Thread] = []
        self._snapshot = _Snapshot()
        self._manifest: dict[str, Any] | None = None
        self._config_error = ""
        self._fatal: BaseException | None = None
        self._result_queue_url = ""
        self._result_queue_touched = 0.0
        self._stalled_since: dict[str, float] = {}
        self._paused_until: dict[str, float] = {}
        self.run_dir = ""
        self._clients: dict[str, Any] = {}

    # --- engine contract --------------------------------------------------------------------

    def assert_ha_compliant(self) -> None:
        raise ValueError("AWSEngine sends task data to AWS; it isn't high-assurance compliant")

    def start(
        self, *args: Any, endpoint_id: Any = None, run_dir: str | None = None, **kwargs: Any
    ) -> None:
        assert endpoint_id, "AWSEngine requires an endpoint_id"
        self.endpoint_id = endpoint_id
        self.set_working_dir(run_dir=run_dir)
        self.run_dir = str(run_dir or os.getcwd())
        _attach_endpoint_logging()
        try:
            sess = awsutil.session(self.aws_profile, self.region)
            self.region = self.region or (sess.region_name or "")
            for svc in ("sqs", "s3", "ssm", "autoscaling"):
                if svc not in self._clients:  # tests inject clients before start()
                    self._clients[svc] = awsutil.client(sess, svc)
            self._load_manifest()
            if self._manifest is not None:
                self._ensure_result_queue()
        except Exception as e:  # start must not raise; tasks report the problem instead
            self._config_error = f"AWS setup failed: {e}"
            log.exception("AWSEngine couldn't initialise")
        for target, name in (
            (self._result_loop, "aegc-results"),
            (self._scale_loop, "aegc-scaler"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._engine_ready = True
        log.info(
            "AWSEngine started: deployment %s, region %s, env %r, python %s, result queue %s",
            self.deployment,
            self.region,
            self.env,
            self.python_minor or "?",
            self._result_queue_url or "-",
        )

    def shutdown(self, /, **kwargs: Any) -> None:
        self._stop.set()
        self._scale_now.set()
        for t in self._threads:
            t.join(timeout=2)

    @property
    def executor_exception(self) -> BaseException | None:
        return self._fatal

    def get_status_report(self) -> EPStatusReport:
        try:
            snap = self._snapshot
            with self._lock:
                outstanding = len(self._inflight)
            pools = {
                name: {
                    "queue_visible": m.visible,
                    "queue_inflight": m.inflight,
                    "dlq": m.dlq,
                    "asg_desired": m.desired,
                    "asg_in_service": m.in_service,
                    "min": m.min,
                    "max": m.max,
                    "slots": m.slots,
                    "stalled": m.stalled,
                    "last_scaling_error": m.last_scaling_error,
                }
                for name, m in snap.pools.items()
            }
            state = {
                "engine_type": "AWSEngine",
                "aegc_version": __version__,
                "deployment": self.deployment,
                "region": self.region,
                "env": self.env,
                "python_minor": self.python_minor,
                "pools": pools,
                "total_workers": sum(m.in_service * m.slots for m in snap.pools.values()),
                "outstanding_tasks": outstanding,
                "pending_tasks": sum(m.visible for m in snap.pools.values()),
                "scaling_enabled": True,
                "metrics_age_s": round(time.time() - snap.at, 1) if snap.at else None,
                "last_aws_error": snap.last_aws_error,
                "config_error": self._config_error,
            }
        except Exception as e:  # never let a status report take the endpoint down
            state = {"engine_type": "AWSEngine", "status_error": str(e)}
        return EPStatusReport(
            endpoint_id=self.endpoint_id, ep_status_report=state, task_statuses={}
        )

    def _submit(
        self,
        resource_specification: dict[str, Any],
        func: Callable[..., Any],
        packed_task: bytes,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Future[Any]:
        task_id, _endpoint_id = args[0], args[1] if len(args) > 1 else self.endpoint_id
        task_uuid = str(task_id)
        kw = kwargs or {}
        with self._lock:
            self._attempts[task_uuid] += 1
            attempt = self._attempts[task_uuid]
        etid = protocol.executor_task_id(task_uuid, attempt)

        pool, walltime, problem = self._resolve(resource_specification or {})
        if problem:
            return _resolved(etid, failed_result(task_uuid, "AEGC_CONFIGURATION", problem))
        assert pool is not None

        fut: Future[Any] = Future()
        fut.executor_task_id = etid  # type: ignore[attr-defined]
        entry = _Inflight(fut, task_uuid, attempt, pool, time.time())
        with self._lock:
            self._inflight[etid] = entry
        try:
            task = payloads.make_payload(
                packed_task,
                s3=self._clients["s3"],
                bucket=self._manifest_value("bucket"),
                key=protocol.task_payload_key(self.deployment, task_uuid, attempt),
                inline_limit=self.inline_limit_bytes,
            )
            entry.task_payload = task
            msg = TaskMessage(
                task_uuid=task_uuid,
                attempt=attempt,
                endpoint_id=str(self.endpoint_id),
                deployment=self.deployment,
                pool=pool,
                reply_to=self._result_queue_url,
                submitted_at=time.time(),
                task=task,
                call=Call(
                    run_in_sandbox=bool(kw.get("run_in_sandbox", False)),
                    task_deserializers=kw.get("task_deserializers"),
                    result_serializers=kw.get("result_serializers"),
                ),
                resource_spec=dict(resource_specification or {}),
                walltime_s=walltime,
                engine_versions={"aegc": __version__},
            )
            self._clients["sqs"].send_message(
                QueueUrl=self._pool(pool)["task_queue_url"], MessageBody=msg.encode()
            )
        except Exception:
            with self._lock:
                self._inflight.pop(etid, None)
            raise  # the base class retries, then reports a failed Result
        self._scale_now.set()
        return fut

    # --- configuration ----------------------------------------------------------------------

    def _load_manifest(self) -> None:
        from aegc.infra.deployment import read_manifest

        try:
            self._manifest = read_manifest(self._clients["ssm"], self.deployment)
            self._config_error = ""
        except Exception as e:
            self._manifest = None
            self._config_error = (
                f"deployment {self.deployment!r} not found in {self.region} ({e}); "
                f"run `aegc deploy {self.deployment}`"
            )
            log.error("%s", self._config_error)

    def _manifest_value(self, key: str) -> Any:
        assert self._manifest is not None
        return self._manifest[key]

    def _pool(self, name: str) -> dict[str, Any]:
        assert self._manifest is not None
        return dict(self._manifest["pools"][name])

    def _resolve(self, spec: dict[str, Any]) -> tuple[str | None, int, str]:
        """(pool, walltime_s, problem). A non-empty problem fails the task without retrying."""
        if self._config_error or self._manifest is None:
            return None, 0, self._config_error or "AWSEngine isn't configured"
        unknown = set(spec) - RESOURCE_KEYS
        if unknown:
            return (
                None,
                0,
                (
                    f"unsupported resource_specification keys {sorted(unknown)}; "
                    f"AWSEngine accepts {sorted(RESOURCE_KEYS)}"
                ),
            )
        try:
            walltime = int(spec.get("walltime_s", protocol.DEFAULT_WALLTIME_S))
        except (TypeError, ValueError):
            return None, 0, "walltime_s must be an integer number of seconds"
        if not 1 <= walltime <= protocol.MAX_WALLTIME_S:
            return None, 0, f"walltime_s must be between 1 and {protocol.MAX_WALLTIME_S}"
        pools: dict[str, dict[str, Any]] = self._manifest.get("pools", {})
        if "pool" in spec:
            name = str(spec["pool"])
            if name not in pools:
                return None, 0, f"no pool {name!r} in deployment {self.deployment!r}"
            return name, walltime, self._python_check(name, pools[name])
        env = str(spec.get("env") or self.env)
        candidates = {n: p for n, p in pools.items() if not env or p.get("env") == env}
        if not candidates:
            have = sorted({str(p.get("env")) for p in pools.values()})
            return None, 0, f"no pool for env {env!r} in {self.deployment!r}; envs: {have}"
        if self.python_minor:
            matching = [
                n for n, p in candidates.items() if p.get("python_minor") == self.python_minor
            ]
            if not matching:
                have = sorted(f"{p.get('python_minor')} ({n})" for n, p in candidates.items())
                return (
                    None,
                    0,
                    (
                        f"no worker image for Python {self.python_minor} in env {env!r}; "
                        f"available: {', '.join(have)}. Submit from a matching Python version."
                    ),
                )
            return sorted(matching)[0], walltime, ""
        return sorted(candidates)[0], walltime, ""

    def _python_check(self, name: str, pool: dict[str, Any]) -> str:
        if self.python_minor and pool.get("python_minor") != self.python_minor:
            return (
                f"pool {name!r} runs Python {pool.get('python_minor')}, but this client is "
                f"Python {self.python_minor}"
            )
        return ""

    def _ensure_result_queue(self) -> None:
        name = naming.result_queue(self.deployment, str(self.endpoint_id))
        tags = naming.tags(self.deployment, "result-queue")
        tags[naming.TAG_ENDPOINT] = str(self.endpoint_id)
        attributes = {
            "MessageRetentionPeriod": str(14 * 24 * 3600),
            "VisibilityTimeout": "120",
            "SqsManagedSseEnabled": "true",
        }
        sqs = self._clients["sqs"]
        self._result_queue_url = sqs.create_queue(QueueName=name, Attributes=attributes, tags=tags)[
            "QueueUrl"
        ]
        # Refreshes LastModifiedTimestamp: `aegc reap` only removes result queues idle for 14 days.
        sqs.set_queue_attributes(QueueUrl=self._result_queue_url, Attributes=attributes)
        self._result_queue_touched = time.time()

    # --- results ----------------------------------------------------------------------------

    def _result_loop(self) -> None:
        while not self._stop.is_set():
            if not self._result_queue_url:
                self._stop.wait(5)
                continue
            try:
                resp = self._clients["sqs"].receive_message(
                    QueueUrl=self._result_queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=self.result_wait_s,
                )
            except Exception as e:
                if awsutil.is_not_found(e):
                    self._recreate_result_queue(e)
                else:
                    self._snapshot.last_aws_error = str(e)
                    log.warning("result poll failed: %s", e)
                self._stop.wait(2)
                continue
            for m in resp.get("Messages", []):
                try:
                    self.handle_result(m["Body"])
                except Exception:
                    log.exception("couldn't handle a result message; leaving it for redelivery")
                    continue
                try:
                    self._clients["sqs"].delete_message(
                        QueueUrl=self._result_queue_url, ReceiptHandle=m["ReceiptHandle"]
                    )
                except Exception as e:
                    log.warning("couldn't delete a result message: %s", e)

    def _recreate_result_queue(self, e: BaseException) -> None:
        try:
            self._ensure_result_queue()
            log.warning("result queue was missing; recreated it")
        except Exception as e2:
            self._fatal = e2
            log.error("result queue is gone (%s) and couldn't be recreated: %s", e, e2)

    def handle_result(self, body: str) -> None:
        """Route one result-queue message. Never call future callbacks while holding the lock."""
        try:
            msg = ResultMessage.decode(body)
        except ProtocolError as e:
            log.error("dropping an undecodable result message: %s", e)
            return
        etid = msg.executor_task_id
        if msg.kind == "started":
            self.set_tasks_placement(
                [etid], block_id=msg.worker.get("pool"), job_id=msg.worker.get("instance_id")
            )
            return

        with self._lock:
            entry = self._inflight.get(etid)
            if entry is None or entry.future.done():
                # First result wins across attempts of the same task.
                entry = next(
                    (
                        e
                        for e in self._inflight.values()
                        if e.task_uuid == msg.task_uuid and not e.future.done()
                    ),
                    None,
                )
            if entry is not None:
                self._inflight.pop(protocol.executor_task_id(entry.task_uuid, entry.attempt), None)
                self._remember(msg.task_uuid)
            already_done = entry is None and msg.task_uuid in self._completed

        if already_done:
            self._discard_payload(msg)
            return
        if msg.kind == "system_failure":
            assert msg.error is not None
            if entry is None:
                self._recover(
                    msg.task_uuid, failed_result(msg.task_uuid, msg.error.code, msg.error.message)
                )
            else:
                entry.future.set_exception(WorkerSystemFailure(msg.error.code, msg.error.message))
            self._discard_task_payload(entry)
            return
        assert msg.result is not None
        data = payloads.read_payload(msg.result, s3=self._clients.get("s3"))
        if entry is None:
            self._recover(msg.task_uuid, data)
        else:
            entry.future.set_result(data)
        self._discard_payload(msg)
        self._discard_task_payload(entry)

    def _remember(self, task_uuid: str) -> None:
        self._completed[task_uuid] = None
        self._completed.move_to_end(task_uuid)
        while len(self._completed) > COMPLETED_LRU:
            self._completed.popitem(last=False)

    def _recover(self, task_uuid: str, packed: bytes) -> None:
        """Hand a result for an unknown task to the interchange via unacked_results/."""
        d = Path(self.run_dir) / "unacked_results"
        d.mkdir(parents=True, exist_ok=True)
        final = d / task_uuid
        if final.exists():
            return
        tmp = d / f".aegc-{task_uuid}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as f:
            f.write(packed)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
        with self._lock:
            self._remember(task_uuid)
        log.info("recovered a result for task %s from an earlier endpoint process", task_uuid)

    def _discard_payload(self, msg: ResultMessage) -> None:
        if msg.result is not None and msg.result.s3 is not None and "s3" in self._clients:
            try:
                payloads.delete_payload(msg.result, s3=self._clients["s3"])
            except Exception as e:
                log.debug("couldn't delete a result payload: %s", e)

    def _discard_task_payload(self, entry: _Inflight | None) -> None:
        if entry and entry.task_payload and entry.task_payload.s3 is not None:
            try:
                payloads.delete_payload(entry.task_payload, s3=self._clients["s3"])
            except Exception as e:
                log.debug("couldn't delete a task payload: %s", e)

    # --- scaling ----------------------------------------------------------------------------

    def _scale_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._result_queue_url and time.time() - self._result_queue_touched > 86400:
                    self._ensure_result_queue()  # daily keep-alive for `aegc reap`
                self.scale_once()
            except Exception as e:
                self._snapshot.last_aws_error = str(e)
                log.warning("scaling pass failed: %s", e)
            self.first_scale_pass.set()
            self._scale_now.wait(self.scale_interval_s)
            self._scale_now.clear()

    def scale_once(self, now: float | None = None) -> _Snapshot:
        """Refresh metrics and scale out each pool to its demand. Returns the new snapshot."""
        now = time.time() if now is None else now
        if self._manifest is None:
            return self._snapshot
        with self._lock:
            outstanding = collections.Counter(e.pool for e in self._inflight.values())
        sqs, asg = self._clients["sqs"], self._clients["autoscaling"]
        snap = _Snapshot(at=now)
        for name, pool in self._manifest.get("pools", {}).items():
            m = _PoolMetrics(
                min=int(pool.get("min_workers", 0)), max=int(pool.get("max_workers", 0))
            )
            m.slots = _slots(pool)
            a = sqs.get_queue_attributes(
                QueueUrl=pool["task_queue_url"],
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            m.visible = int(a.get("ApproximateNumberOfMessages", 0))
            m.inflight = int(a.get("ApproximateNumberOfMessagesNotVisible", 0))
            groups = asg.describe_auto_scaling_groups(
                AutoScalingGroupNames=[pool["auto_scaling_group"]]
            )["AutoScalingGroups"]
            if groups:
                g = groups[0]
                m.desired = int(g.get("DesiredCapacity", 0))
                m.in_service = sum(
                    1 for i in g.get("Instances", []) if i.get("LifecycleState") == "InService"
                )
            demand = max(outstanding.get(name, 0), m.visible + m.inflight)
            target = min(max(math.ceil(demand / m.slots), m.min), m.max)
            m.stalled = self._stalled(name, m, demand, now)
            if target > m.desired and not m.stalled and groups:
                try:
                    asg.set_desired_capacity(
                        AutoScalingGroupName=pool["auto_scaling_group"],
                        DesiredCapacity=target,
                        HonorCooldown=False,
                    )
                    log.info(
                        "pool %s: scaling out %d -> %d (demand %d)", name, m.desired, target, demand
                    )
                    m.desired = target
                except Exception as e:
                    m.last_scaling_error = str(e)
                    log.warning("pool %s: scale-out failed: %s", name, e)
            snap.pools[name] = m
        self._snapshot = snap
        return snap

    def _stalled(self, name: str, m: _PoolMetrics, demand: int, now: float) -> bool:
        """Circuit breaker: capacity asked for but none in service for a long time."""
        if now < self._paused_until.get(name, 0):
            return True
        if m.desired > 0 and m.in_service == 0 and demand > 0:
            since = self._stalled_since.setdefault(name, now)
            if now - since >= STALL_AFTER_S:
                self._paused_until[name] = now + STALL_PAUSE_S
                self._stalled_since.pop(name, None)
                log.error(
                    "pool %s: %d instance(s) requested but none in service for %d min; pausing "
                    "scale-out for %d min (check the Auto Scaling group's activity)",
                    name,
                    m.desired,
                    STALL_AFTER_S // 60,
                    STALL_PAUSE_S // 60,
                )
                return True
        else:
            self._stalled_since.pop(name, None)
        return False


def _slots(pool: dict[str, Any]) -> int:
    slots = str(pool.get("slots", "auto"))
    if slots == "auto":
        return max(1, int(pool.get("vcpu_min", 1)))
    return max(1, int(slots))


def _resolved(etid: str, packed: bytes) -> Future[Any]:
    fut: Future[Any] = Future()
    fut.executor_task_id = etid  # type: ignore[attr-defined]
    fut.set_result(packed)
    return fut


def _attach_endpoint_logging() -> None:
    """The endpoint only attaches handlers to its own loggers; share them with aegc (spike #3)."""
    src = logging.getLogger("globus_compute_endpoint")
    dst = logging.getLogger("aegc")
    for h in src.handlers:
        if h not in dst.handlers:
            dst.addHandler(h)
    if src.level and (dst.level == logging.NOTSET or dst.level > src.level):
        dst.setLevel(src.level)
