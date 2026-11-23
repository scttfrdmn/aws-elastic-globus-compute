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
- Lambda pools (#21, :mod:`aegc.lambda_pools`) aren't scaled: Lambda scales them. Tasks go to one
  only when they name it (``pool=``) or their env has no EC2 pool for their Python. The scaler
  keeps each one's event source mapping enabled while the deployment runs, disabled otherwise.
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

from aegc import (
    __version__,
    awsutil,
    caps,
    lambda_pools,
    lease,
    naming,
    payloads,
    pricing,
    protocol,
    sizes,
)
from aegc.gcresult import failed_result
from aegc.pricing import IPV4_HOUR, Prices, volume_hour
from aegc.protocol import Call, ProtocolError, ResultMessage, TaskMessage
from aegc.spend import SpendGuard, Trip
from aegc.state import Limits, State, read_state

log = logging.getLogger("aegc.engine")

#: resource_specification keys AWSEngine understands in v0.1.
RESOURCE_KEYS = frozenset({"env", "pool", "walltime_s", "size", "cpus", "memory_gib"})
#: The Globus Compute web service validates resource_specification as an MPI request: it
#: requires ``num_nodes`` (HTTP 422 without it) and allows these. Tasks here run on one instance.
MPI_KEYS = frozenset({"num_nodes", "ranks_per_node", "num_ranks", "launcher_options"})
#: Keys people reach for that aegc spells differently.
_DID_YOU_MEAN = {"vcpus": "cpus", "num_cores": "cpus", "cores": "cpus", "memory": "memory_gib",
                 "mem": "memory_gib", "mem_per_worker": "memory_gib"}  # fmt: skip
COMPLETED_LRU = 10_000
STALL_AFTER_S = 15 * 60
STALL_PAUSE_S = 10 * 60
#: Re-read the deployment state and manifest this often (scaler thread).
STATE_TTL_S = 30.0
MANIFEST_TTL_S = 60.0
#: Before refusing a task, re-read a state older than this (a resume may have just happened).
REFUSE_RECHECK_S = 5.0
#: Tasks waiting this long with no workers because the spend limits admit none are failed.
BLOCKED_FAIL_S = 600.0


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
    started: bool = False  # a worker posted "started"


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
    admitted: int | None = None  # capped by spend admission this pass (None: not limited)
    kind: str = "ec2"
    mapping: str = ""  # Lambda pools: the event source mapping's State


@dataclass
class _Snapshot:
    at: float = 0.0
    pools: dict[str, _PoolMetrics] = field(default_factory=dict)
    last_aws_error: str = ""
    state: str = "running"
    usd_per_hour: float = 0.0
    spend_today: float | None = None
    spend_month: float | None = None


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
        stall_fail_s: float = 3600.0,
        pool: str = "",
        size: str = "",
        cpus: float = 0,
        memory_gib: float = 0,
        walltime_s: int = 0,
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
        self.stall_fail_s = stall_fail_s
        # Task defaults from user_endpoint_config: the web service drops these keys from a
        # task's resource_specification (it keeps only num_nodes and the MPI fields).
        self.pool = pool
        self.size = size
        self.cpus = cpus
        self.memory_gib = memory_gib
        self.walltime_s = walltime_s
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
        self._starved_since: dict[str, float] = {}
        self._blocked_since: dict[str, float] = {}
        self._local_cap: State | None = None  # a cap this engine couldn't write to SSM
        self._state = State()
        self._state_read = -math.inf
        self._manifest_read = -math.inf
        self._spend: SpendGuard | None = None
        self._lease_renewed = -math.inf
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
            for svc in ("sqs", "s3", "ssm", "autoscaling", "ec2", "scheduler", "lambda"):
                if svc not in self._clients:  # tests inject clients before start()
                    self._clients[svc] = awsutil.client(sess, svc)
            if "pricing" not in self._clients:  # the Price List API lives in us-east-1
                self._clients["pricing"] = awsutil.client(
                    awsutil.session(self.aws_profile, "us-east-1"), "pricing"
                )
            self._load_manifest()
            if self._manifest is not None:
                self._ensure_result_queue()
                self._spend = SpendGuard(
                    s3=self._clients["s3"],
                    bucket=str(self._manifest["bucket"]),
                    deployment=self.deployment,
                    prices=Prices(
                        region=self.region,
                        ec2=self._clients["ec2"],
                        pricing=self._clients["pricing"],
                    ),
                )
                self.refresh_state()
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
                name: (
                    {
                        "kind": m.kind,
                        "queue_visible": m.visible,
                        "queue_inflight": m.inflight,
                        "dlq": m.dlq,
                        "max_concurrency": m.max,
                        "mapping": m.mapping,
                        "last_scaling_error": m.last_scaling_error,
                    }
                    if m.kind == lambda_pools.KIND
                    else {
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
                )
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
                "deployment_state": self._state.mode,
                "usd_per_hour": round(snap.usd_per_hour, 4),
                "spend_today_usd": snap.spend_today,
                "spend_month_usd": snap.spend_month,
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

        st = self._state
        if not st.running and time.time() - self._state_read > REFUSE_RECHECK_S:
            # Don't refuse on a stale copy: a resume may have happened since the last read.
            st = self.refresh_state(force=True)
        if not st.running:
            return _resolved(
                etid, failed_result(task_uuid, st.failure_code, st.explain(self.deployment))
            )
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
        spec, problem = _strip_mpi_keys(spec)
        if problem:
            return None, 0, problem
        defaults = {
            k: v
            for k, v in (
                ("pool", self.pool),
                ("size", self.size),
                ("cpus", self.cpus),
                ("memory_gib", self.memory_gib),
                ("walltime_s", self.walltime_s),
            )
            if v
        }
        spec = {**defaults, **spec}
        unknown = set(spec) - RESOURCE_KEYS
        if unknown:
            hints = [f"{k} -> {_DID_YOU_MEAN[k]}" for k in sorted(unknown) if k in _DID_YOU_MEAN]
            return (
                None,
                0,
                (
                    f"unsupported task options {sorted(unknown)}; "
                    f"AWSEngine accepts {sorted(RESOURCE_KEYS)}"
                    + (f" (did you mean {', '.join(hints)}?)" if hints else "")
                ),
            )
        for key in ("cpus", "memory_gib"):
            if key in spec:
                try:
                    ok = float(spec[key]) > 0
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    return None, 0, f"task option {key} must be a positive number"
        pools: dict[str, dict[str, Any]] = self._manifest.get("pools", {})
        if "pool" in spec:
            name = str(spec["pool"])
            if name not in pools:
                return None, 0, f"no pool {name!r} in deployment {self.deployment!r}"
            walltime, problem = _walltime(spec, pools[name], name)
            problem = problem or self._python_check(name, pools[name]) or _fit(spec, name, pools)
            return name, walltime, problem
        picked, problem = self._pick_pool(spec, pools)
        if picked is None:
            return None, 0, problem
        walltime, problem = _walltime(spec, pools[picked], picked)
        return (None, 0, problem) if problem else (picked, walltime, "")

    def _pick_pool(
        self, spec: dict[str, Any], pools: dict[str, dict[str, Any]]
    ) -> tuple[str | None, str]:
        env = str(spec.get("env") or self.env)
        candidates = {n: p for n, p in pools.items() if not env or p.get("env") == env}
        if not candidates:
            have = sorted({str(p.get("env")) for p in pools.values()})
            return None, f"no pool for env {env!r} in {self.deployment!r}; envs: {have}"
        if self.python_minor:
            matching = [
                n for n, p in candidates.items() if p.get("python_minor") == self.python_minor
            ]
            if not matching:
                have = sorted(f"{p.get('python_minor')} ({n})" for n, p in candidates.items())
                return None, (
                    f"no worker image for Python {self.python_minor} in env {env!r}; "
                    f"available: {', '.join(have)}. Submit from a matching Python version."
                )
            candidates = _prefer_ec2({n: candidates[n] for n in matching})
        else:
            # Lambda pools only for an env without EC2 pools (any Python): see _prefer_ec2.
            candidates = _prefer_ec2(candidates)
            minors = sorted(
                {str(p.get("python_minor")) for p in candidates.values()},
                key=lambda m: tuple(int(x) if x.isdigit() else 0 for x in m.split(".")),
            )
            if len(minors) > 1:
                return None, (
                    f"this submission doesn't say its Python version (Globus Flows doesn't send "
                    f"one), and env {env!r} has worker images for {', '.join(minors)}; add "
                    f'"python": "{minors[-1]}" (your function\'s Python) to user_endpoint_config'
                )
        return _pick_size(spec, candidates, env)

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
            with self._lock:
                started = self._inflight.get(etid)
                if started is not None:
                    started.started = True
            self.set_tasks_placement(
                [etid], block_id=msg.worker.get("pool"), job_id=msg.worker.get("instance_id")
            )
            return
        # Before anything else: a forged message pointing at another key (e.g. another task's
        # input, #32) must neither be returned nor make the engine delete that object.
        ref = msg.result.s3 if msg.result is not None else None
        if ref is not None and (
            ref.key != protocol.result_payload_key(self.deployment, msg.task_uuid, msg.attempt)
            or ref.bucket != self._manifest_value("bucket")
        ):
            log.warning(
                "ignoring a result for %s that points at s3://%s/%s, not its own result key",
                msg.task_uuid, ref.bucket, ref.key,
            )  # fmt: skip
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
                self.refresh_manifest()
                self.refresh_state()
                self.renew_lease()
                self.scale_once()
            except Exception as e:
                self._snapshot.last_aws_error = str(e)
                log.warning("scaling pass failed: %s", e)
            self.first_scale_pass.set()
            self._scale_now.wait(self.scale_interval_s)
            self._scale_now.clear()

    # --- deployment state and manifest ------------------------------------------------------

    def refresh_state(self, now: float | None = None, *, force: bool = False) -> State:
        """Re-read ``/aegc/<d>/state`` (at most every STATE_TTL_S); act on a change."""
        now = time.time() if now is None else now
        if not force and now - self._state_read < STATE_TTL_S:
            return self._state
        try:
            st = read_state(self._clients["ssm"], self.deployment, now=now)
        except Exception as e:
            log.warning("couldn't read the deployment state: %s", e)
            return self._state
        self._state_read = now
        lc = self._local_cap
        if st.running and lc is not None and (not lc.until or now < lc.until):
            return self._state  # keep the cap we couldn't share rather than flip-flop
        self._local_cap = None
        self._set_state(st)
        return st

    def _set_state(self, st: State) -> None:
        before, self._state = self._state, st
        if st.running:
            if not before.running:
                log.info("deployment %s is running again", self.deployment)
                if before.mode == "capped" and st.by == "reset":
                    self._restore_minimums()
            return
        if before.running or before.mode != st.mode or (st.kill and not before.kill):
            log.warning("deployment %s is %s: %s", self.deployment, st.mode, st.reason)
            self.fail_owned(st.failure_code, st.explain(self.deployment), include_started=st.kill)

    def _restore_minimums(self) -> None:
        """After a cap's period ends, put back the minimums the trip lowered to 0."""
        if self._manifest is None:
            return
        for name, pool in self._manifest.get("pools", {}).items():
            if lambda_pools.is_lambda(pool):
                continue  # the scaler enables its mapping (_reconcile_mappings)
            minimum = int(pool.get("min_vcpus" if _vcpu_mode(pool) else "min_workers", 0))
            if not minimum:
                continue
            try:
                self._clients["autoscaling"].update_auto_scaling_group(
                    AutoScalingGroupName=pool["auto_scaling_group"], MinSize=minimum
                )
            except Exception as e:
                log.warning("pool %s: couldn't restore the minimum: %s", name, e)

    def refresh_manifest(self, now: float | None = None) -> bool:
        """Reload the manifest if a redeploy changed it (at most every MANIFEST_TTL_S)."""
        from aegc.infra.deployment import read_manifest

        now = time.time() if now is None else now
        if now - self._manifest_read < MANIFEST_TTL_S or self._manifest is None:
            return False
        self._manifest_read = now
        try:
            fresh = read_manifest(self._clients["ssm"], self.deployment)
        except Exception as e:
            log.warning("couldn't re-read the manifest: %s", e)
            return False
        if fresh.get("generation") == self._manifest.get("generation"):
            return False
        log.info(
            "deployment %s redeployed (generation %s -> %s): pools %s",
            self.deployment,
            self._manifest.get("generation"),
            fresh.get("generation"),
            sorted(fresh.get("pools", {})),
        )
        self._manifest = fresh
        return True

    def renew_lease(self, now: float | None = None) -> bool:
        """Push the deployment's lease forward while this engine runs (running or capped: a cap
        ends by itself at the end of its period, so the deployment must not expire meanwhile)."""
        now = time.time() if now is None else now
        if self._manifest is None or self._state.mode not in ("running", "capped"):
            return False
        lease_s = int((self._manifest.get("lease") or {}).get("lease_s") or 0)
        if not lease_s or now - self._lease_renewed < lease.renew_interval(lease_s):
            return False
        try:
            n = lease.renew(self._clients["scheduler"], self.deployment, lease_s, now=now)
        except Exception as e:
            log.warning("couldn't renew the deployment lease: %s", e)
            return False
        self._lease_renewed = now
        log.info(
            "renewed the lease of %s (%d schedule(s)) for %d h", self.deployment, n, lease_s // 3600
        )
        return True

    # --- failing what this process owns -------------------------------------------------

    def fail_owned(
        self, code: str, message: str, *, pool: str | None = None, include_started: bool = False
    ) -> int:
        """Fail this process's queued tasks (and running ones if ``include_started``) with a
        clear, final result, and remove their messages from the task queues."""
        with self._lock:
            doomed = [
                (etid, e)
                for etid, e in self._inflight.items()
                if (pool is None or e.pool == pool) and (include_started or not e.started)
            ]
            for etid, e in doomed:
                self._inflight.pop(etid, None)
                self._remember(e.task_uuid)
        for _, e in doomed:
            if not e.future.done():
                e.future.set_result(failed_result(e.task_uuid, code, message))
        if doomed:
            log.warning("failed %d task(s): %s", len(doomed), code)
            uuids = {e.task_uuid for _, e in doomed}
            for name in sorted({e.pool for _, e in doomed}):
                self._purge(name, uuids)
        return len(doomed)

    def _purge(self, pool_name: str, uuids: set[str]) -> None:
        """Delete this process's messages for ``uuids`` from a pool's task queue; leave others.

        One sweep: other messages stay invisible until the sweep ends and are then released
        together, so each is received at most once (every receive counts toward the DLQ).
        """
        if self._manifest is None or pool_name not in self._manifest.get("pools", {}):
            return
        sqs, url = self._clients["sqs"], self._pool(pool_name)["task_queue_url"]
        remaining = set(uuids)
        foreign: list[str] = []
        try:
            for _ in range(20):
                if not remaining:
                    break
                msgs = sqs.receive_message(
                    QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=0, VisibilityTimeout=120
                ).get("Messages", [])
                if not msgs:
                    break
                for m in msgs:
                    try:
                        tm = TaskMessage.decode(m["Body"])
                        mine = tm.reply_to == self._result_queue_url and tm.task_uuid in remaining
                    except ProtocolError:
                        mine = False
                    if mine:
                        sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
                        remaining.discard(tm.task_uuid)
                    else:
                        foreign.append(m["ReceiptHandle"])
        except Exception as e:
            log.warning("couldn't purge %s: %s", pool_name, e)
        finally:
            for receipt in foreign:
                try:
                    sqs.change_message_visibility(
                        QueueUrl=url, ReceiptHandle=receipt, VisibilityTimeout=0
                    )
                except Exception as e:
                    log.warning("purge %s: couldn't release a message: %s", pool_name, e)

    # --- scaling ---------------------------------------------------------------------------

    def scale_once(self, now: float | None = None) -> _Snapshot:
        """Refresh metrics and scale out each pool to its demand, within the spend limits."""
        now = time.time() if now is None else now
        if self._manifest is None:
            return self._snapshot
        with self._lock:
            outstanding = collections.Counter(e.pool for e in self._inflight.values())
        sqs, asg = self._clients["sqs"], self._clients["autoscaling"]
        snap = _Snapshot(at=now, state=self._state.mode)
        pools: dict[str, dict[str, Any]] = self._manifest.get("pools", {})
        groups: dict[str, dict[str, Any]] = {}
        for name, pool in pools.items():
            if lambda_pools.is_lambda(pool):
                m = _PoolMetrics(kind=lambda_pools.KIND, max=lambda_pools.concurrency(pool))
            elif _vcpu_mode(pool):
                m = _PoolMetrics(
                    min=int(pool.get("min_vcpus", 0)), max=int(pool.get("max_vcpus", 0))
                )
            else:  # a v0.1 deployment: capacity in workers
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
            if m.kind == lambda_pools.KIND:  # Lambda scales it: nothing to describe
                snap.pools[name] = m
                continue
            found = asg.describe_auto_scaling_groups(
                AutoScalingGroupNames=[pool["auto_scaling_group"]]
            )["AutoScalingGroups"]
            if found:
                groups[name] = found[0]
                m.desired = int(found[0].get("DesiredCapacity", 0))
                m.in_service = sum(
                    1
                    for i in found[0].get("Instances", [])
                    if i.get("LifecycleState") == "InService"
                )
            snap.pools[name] = m

        self._reconcile_mappings(pools, snap, now)
        limits = Limits.from_dict(self._manifest.get("limits"))
        rate_now, instances_now = self._current_rate(pools, groups)
        snap.usd_per_hour = rate_now + self._lambda_rate(pools, snap)
        # Admission counts Lambda pools at their worst case: every environment busy.
        worst_rate, worst_instances = _lambda_worst_case(pools)
        rate_now += worst_rate
        instances_now += worst_instances
        if limits.any and self._spend is not None and self._state.running:
            try:
                snap.spend_today = round(self._spend.accrued("day", now).usd, 4)
                snap.spend_month = round(self._spend.accrued("month", now).usd, 4)
                trip = self._spend.check(limits, now)
            except Exception as e:
                trip = None
                snap.last_aws_error = f"spend check failed: {e}"
                log.warning("spend check failed: %s", e)
            if trip is not None:
                self._trip(trip, pools, groups, limits)
                snap.state = self._state.mode

        for name, pool in pools.items():
            m = snap.pools[name]
            if m.kind == lambda_pools.KIND:
                continue
            demand = max(outstanding.get(name, 0), m.visible + m.inflight)
            target = min(max(_capacity_for(pool, demand), m.min), m.max)
            m.stalled = self._stalled(name, m, demand, now)
            self._starvation(name, pool, m, demand, now, groups.get(name))
            if not self._state.running or target <= m.desired or m.stalled or name not in groups:
                continue
            if limits.any and self._spend is not None:
                worker_cost = self._unit_cost(pool)
                per_worker = int(pool.get("vcpu_min", 2)) if _vcpu_mode(pool) else 1
                unit_cost = worker_cost / per_worker
                # Whole workers: Auto Scaling launches a whole instance for any vCPU target, so a
                # 1-vCPU demand is a 2-vCPU worker (and rounding it down to 0 would hang it).
                want = math.ceil((target - m.desired) / per_worker) * per_worker
                # ...but never past the group's maximum (the call would be refused).
                want = min(want, (m.max - m.desired) // per_worker * per_worker)
                allowed = self._spend.admit(
                    limits,
                    now,
                    rate_now=rate_now,
                    instances_now=instances_now,
                    want_units=want,
                    unit_usd_per_hour=unit_cost,
                    unit_instances=1.0 / per_worker,
                )
                # Whole workers only: Auto Scaling rounds a vCPU target up to an instance.
                allowed -= allowed % per_worker
                m.admitted = allowed
                self._admission_blocked(name, m, limits, allowed, now)
                if allowed < want:
                    log.warning(
                        "pool %s: spend limits admit %d of %d more %s",
                        name,
                        allowed,
                        want,
                        "vCPUs" if _vcpu_mode(pool) else "worker(s)",
                    )
                target = m.desired + allowed
                rate_now += allowed * unit_cost
                instances_now += allowed / per_worker
                if target <= m.desired:
                    continue
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
        self._snapshot = snap
        return snap

    def _current_rate(
        self, pools: dict[str, dict[str, Any]], groups: dict[str, dict[str, Any]]
    ) -> tuple[float, float]:
        """($/h, instances) of the deployment's running workers."""
        if self._spend is None:
            return 0.0, 0.0
        rate = n = 0.0
        for name, g in groups.items():
            pool = pools[name]
            for i in g.get("Instances", []):
                if i.get("LifecycleState") not in ("InService", "Pending", "Pending:Wait"):
                    continue
                r = self._spend.prices.rate(
                    str(i.get("InstanceType") or _cheapest_guess(pool)),
                    str(i.get("AvailabilityZone", "")),
                    "spot" if pool.get("spot", True) else "on-demand",
                    root_gib=int(pool.get("root_volume_gib", 30)),
                    throughput_mibps=int(pool.get("throughput_mibps", 125)),
                )
                rate += r.usd_per_hour
                n += 1
        return rate, n

    def _lambda_rate(self, pools: dict[str, dict[str, Any]], snap: _Snapshot) -> float:
        """About what the Lambda pools cost now: one busy environment per message in flight."""
        return sum(
            min(snap.pools[n].inflight, lambda_pools.concurrency(p))
            * pricing.lambda_hourly(lambda_pools.memory_mib(p))
            for n, p in pools.items()
            if lambda_pools.is_lambda(p) and n in snap.pools
        )

    def _reconcile_mappings(
        self, pools: dict[str, dict[str, Any]], snap: _Snapshot, now: float
    ) -> None:
        """Lambda pools' mappings follow the state: enabled while running, else disabled (a cap
        whose period ends reads as running with nobody to enable them)."""
        lam = self._clients.get("lambda")
        if lam is None or not lambda_pools.mappings(pools):
            return

        def still_running() -> bool:  # re-read: don't undo a stop that just happened
            return self.refresh_state(now, force=True).running

        try:
            states, changes = lambda_pools.reconcile(
                lam, pools, self._state.running, confirm=still_running
            )
        except Exception as e:
            snap.last_aws_error = f"Lambda mappings: {e}"
            log.warning("couldn't check the Lambda pools' mappings: %s", e)
            return
        for name, state in states.items():
            if name in snap.pools:
                snap.pools[name].mapping = state
        for name, change in changes.items():
            if change in ("enabled", "disabled"):
                log.info("pool %s: %s its event source mapping", name, change)
            else:
                if name in snap.pools:
                    snap.pools[name].last_scaling_error = change
                log.warning("pool %s: couldn't update its event source mapping: %s", name, change)

    def _unit_cost(self, pool: dict[str, Any]) -> float:
        """$/h of one more worker, erring high: the priciest allowed family at the pool's
        smallest size, plus volume and IPv4."""
        assert self._spend is not None
        families = _families(pool)
        per_vcpu = self._spend.prices.max_spot_per_vcpu(
            [f"{f}.large" for f in families] if pool.get("spot", True) else [],
        )
        if not pool.get("spot", True):
            per_vcpu = max(
                (self._spend.prices.on_demand(f"{f}.large")[0] / 2 for f in families),
                default=per_vcpu,
            )
        vcpus = int(pool.get("vcpu_min", 2))
        return (
            per_vcpu * vcpus
            + volume_hour(
                int(pool.get("root_volume_gib", 30)), int(pool.get("throughput_mibps", 125))
            )
            + IPV4_HOUR
        )

    def _admission_blocked(
        self, name: str, m: _PoolMetrics, limits: Limits, allowed: int, now: float
    ) -> None:
        """Nothing admitted, nothing running: fail this process's waiting tasks after a while
        rather than leave them hanging below a cap that may never trip."""
        if allowed > 0 or m.in_service > 0 or m.desired > 0:
            self._blocked_since.pop(name, None)
            return
        since = self._blocked_since.setdefault(name, now)
        if now - since < BLOCKED_FAIL_S:
            return
        self._blocked_since.pop(name, None)
        from aegc.control import describe_limits

        self.fail_owned(
            "AEGC_BUDGET_EXCEEDED",
            (
                f"pool {name!r}: the spend limits ({describe_limits(limits)}) leave no room for a "
                f"worker, so tasks waited {BLOCKED_FAIL_S / 60:.0f} min without one. Raise a limit "
                f"with `aegc limits {self.deployment} --max-usd-day N` (or --max-usd-month)."
            ),
            pool=name,
        )

    def _trip(
        self,
        trip: Trip,
        pools: dict[str, dict[str, Any]],
        groups: dict[str, dict[str, Any]],
        limits: Limits,
    ) -> None:
        st, error = caps.trip(
            ssm=self._clients["ssm"],
            autoscaling=self._clients["autoscaling"],
            deployment=self.deployment,
            cap=trip,
            limits=limits,
            groups={
                pools[name]["auto_scaling_group"]: int(g.get("MinSize", 0))
                for name, g in groups.items()
            },
            by="engine",
            lam=self._clients.get("lambda"),
            mappings=lambda_pools.mappings(pools),
        )
        if error is not None:  # still stop this process's spending
            self._local_cap = st
        self._state_read = time.time()
        self._set_state(st)

    def _starvation(
        self,
        name: str,
        pool: dict[str, Any],
        m: _PoolMetrics,
        demand: int,
        now: float,
        group: dict[str, Any] | None,
    ) -> None:
        """No capacity for ``stall_fail_s``: fail this process's queued tasks for the pool."""
        if not (m.desired > 0 and m.in_service == 0 and demand > 0):
            self._starved_since.pop(name, None)
            return
        since = self._starved_since.setdefault(name, now)
        if m.desired > m.in_service and group is not None:
            m.last_scaling_error = m.last_scaling_error or self._last_failure(group)
        if now - since < self.stall_fail_s:
            return
        self._starved_since.pop(name, None)
        why = m.last_scaling_error or "no instances launched"
        self.fail_owned(
            "AEGC_NO_CAPACITY",
            (
                f"pool {name!r} got no workers for {self.stall_fail_s / 60:.0f} min ({why}). "
                "If it's the Spot vCPU quota, request more: aws service-quotas "
                "request-service-quota-increase --service-code ec2 --quota-code L-34B43A08 "
                "--desired-value N. Widening `vcpu=` or using `on-demand` can also help."
            ),
            pool=name,
        )

    def _last_failure(self, group: dict[str, Any]) -> str:
        try:
            acts = (
                self._clients["autoscaling"]
                .describe_scaling_activities(
                    AutoScalingGroupName=group["AutoScalingGroupName"], MaxRecords=1
                )
                .get("Activities", [])
            )
        except Exception:
            return ""
        if acts and acts[0].get("StatusCode") in ("Failed", "Cancelled"):
            return str(acts[0].get("StatusMessage") or acts[0].get("Description", ""))
        return ""

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


def _walltime(spec: dict[str, Any], pool: dict[str, Any], name: str = "") -> tuple[int, str]:
    """(walltime_s, problem): the request's, else the pool's default; within the pool's max
    (for a Lambda pool, also within its function's timeout)."""
    ceiling = min(
        int(pool.get("max_walltime_s") or protocol.MAX_WALLTIME_S), protocol.MAX_WALLTIME_S
    )
    if lambda_pools.is_lambda(pool):
        ceiling = min(ceiling, lambda_pools.max_walltime_s(pool))
    default = int(pool.get("walltime_s") or protocol.DEFAULT_WALLTIME_S)
    try:
        walltime = int(spec.get("walltime_s", min(default, ceiling)))
    except (TypeError, ValueError):
        return 0, "walltime_s must be an integer number of seconds"
    if lambda_pools.is_lambda(pool) and walltime > ceiling >= 1:
        timeout = int(pool.get("timeout_s") or lambda_pools.DEFAULT_TIMEOUT_S)
        return 0, (
            f"walltime_s {walltime} is more than Lambda pool {name!r} allows: its function "
            f"{pool.get('function_name') or '?'} times out after {timeout} s, so a task gets at "
            f"most {ceiling} s ({lambda_pools.MARGIN_S} s are kept for posting the result). Ask "
            "for less, or use an EC2 pool for longer tasks."
        )
    if not 1 <= walltime <= ceiling:
        return 0, f"walltime_s must be between 1 and {ceiling}"
    return walltime, ""


def _prefer_ec2(pools: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The EC2 pools, if there are any: tasks go to a Lambda pool only by name, or when only
    Lambda pools serve their env (and Python)."""
    return {n: p for n, p in pools.items() if not lambda_pools.is_lambda(p)} or pools


def _lambda_worst_case(pools: dict[str, dict[str, Any]]) -> tuple[float, float]:
    """($/h, busy environments) of every Lambda pool at its maximum concurrency."""
    lam = [p for p in pools.values() if lambda_pools.is_lambda(p)]
    return (
        sum(lambda_pools.worst_case_usd_per_hour(p) for p in lam),
        float(sum(lambda_pools.concurrency(p) for p in lam)),
    )


def _per_task(pool: dict[str, Any]) -> tuple[float, float]:
    if lambda_pools.is_lambda(pool):
        return lambda_pools.per_task(pool)
    return sizes.per_task(pool)


def _families(pool: dict[str, Any]) -> list[str]:
    """Instance families a pool allows (``c8g.*`` -> ``c8g``)."""
    out = []
    for t in pool.get("allowed_instance_types") or []:
        fam = str(t).split(".")[0].rstrip("*")
        if fam and fam not in out:
            out.append(fam)
    return out or ["m8g"]


def _cheapest_guess(pool: dict[str, Any]) -> str:
    return f"{_families(pool)[0]}.large"


def _vcpu_mode(pool: dict[str, Any]) -> bool:
    return pool.get("capacity_type") == "vcpu"


def _strip_mpi_keys(spec: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """``spec`` without the MPI keys the web service makes every request carry, or a problem if
    they ask for more than one instance or an MPI launch."""
    mpi = {k: spec[k] for k in MPI_KEYS & set(spec)}
    rest = {k: v for k, v in spec.items() if k not in MPI_KEYS}
    try:
        nodes = int(mpi.get("num_nodes", 1))
        ranks = max(int(mpi.get("ranks_per_node") or 1), int(mpi.get("num_ranks") or 1))
    except (TypeError, ValueError):
        return rest, "num_nodes, ranks_per_node and num_ranks must be numbers"
    if nodes != 1:
        return rest, f"AWSEngine runs each task on one instance; num_nodes must be 1, not {nodes}"
    if ranks > 1 or mpi.get("launcher_options"):
        return rest, "AWSEngine doesn't launch MPI ranks; use cpus= for a multi-core task"
    return rest, ""


def _pick_size(
    spec: dict[str, Any], pools: dict[str, dict[str, Any]], env: str
) -> tuple[str | None, str]:
    """The pool for a request's ``size``, else the cheapest giving ``cpus``/``memory_gib``."""

    def cls(p: dict[str, Any]) -> str:
        return str(p.get("size_class") or sizes.DEFAULT)

    def have() -> str:
        return ", ".join(
            f"{n} ({lambda_pools.describe(p) if lambda_pools.is_lambda(p) else cls(p)}: "
            f"{c:g} vCPU, {m:g} GiB per task)"
            for n, p in sorted(pools.items())
            for c, m in [_per_task(p)]
        )

    if "size" in spec:
        want = str(spec["size"])
        named = sorted(n for n, p in pools.items() if cls(p) == want)
        if not named:
            return None, f"no size={want!r} pool for env {env!r}; available: {have()}"
        problem = _fit(spec, named[0], pools)
        return (None, problem) if problem else (named[0], "")
    cpus = float(spec.get("cpus", 0))
    mem = float(spec.get("memory_gib", 0))
    # Cheapest first: the vCPUs a task holds, then a sized pool over a whole one, then memory.
    fits = [
        (
            math.ceil(c)
            if lambda_pools.is_lambda(p)
            else _capacity_for({**p, "slots": "auto", "capacity_type": "vcpu"}, 1),
            cls(p) == "whole",
            m,
            n,
        )
        for n, p in pools.items()
        for c, m in [_per_task(p)]
        if c >= cpus and m >= mem
    ]
    if not fits:
        return None, (
            f"no pool for env {env!r} gives a task {cpus:g} vCPUs and {mem:g} GiB; "
            f"available: {have()}. Deploy one with --pool {env}:size=..."
        )
    return min(fits)[-1], ""


def _fit(spec: dict[str, Any], name: str, pools: dict[str, dict[str, Any]]) -> str:
    """A problem if the request's ``cpus``/``memory_gib`` don't fit pool ``name``'s tasks."""
    cpus, mem = float(spec.get("cpus", 0)), float(spec.get("memory_gib", 0))
    c, m = _per_task(pools[name])
    if cpus > c or mem > m:
        return (
            f"pool {name!r} gives a task {c:g} vCPUs and {m:g} GiB, less than the "
            f"{cpus:g} vCPUs and {mem:g} GiB asked for"
        )
    return ""


def _capacity_for(pool: dict[str, Any], demand: int) -> int:
    """Capacity units (vCPUs, or workers for a v0.1 pool) that run ``demand`` tasks at once.

    ``slots=auto`` runs one task per ``task_cpus`` vCPUs (one per vCPU without a size class),
    and ``size=whole`` one per instance (whole pools have one worker size). Fixed ``slots=N`` runs N
    tasks per worker of at least ``vcpu_min`` vCPUs.
    """
    if not _vcpu_mode(pool):
        return math.ceil(demand / _slots(pool))
    slots = str(pool.get("slots", "auto"))
    if slots == "auto":
        if pool.get("size_class") == "whole":  # one worker size (vcpu_min == vcpu_max)
            return demand * int(pool.get("vcpu_min", 2))
        return demand * max(1, int(pool.get("task_cpus") or 1))
    return math.ceil(demand / max(1, int(slots))) * int(pool.get("vcpu_min", 2))


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
