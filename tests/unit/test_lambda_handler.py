"""The Lambda handler (#75) against substrate SQS, S3 and SSM, called as Lambda would call it."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aegc import ledger, state
from aegc.worker import emf
from aegc.worker.lambda_handler import VISIBILITY_SLACK_S, Runtime, environment_id
from tests.unit.test_worker import BUCKET, Rig, double, sleeper, value_of
from tests.unit.test_worker import rig as rig  # the fixture

STREAM = "2026/11/23/[$LATEST]0123456789abcdef0123456789abcdef"


class Context:
    def __init__(self, remaining_s: float = 900, request_id: str = "req-1") -> None:
        self.remaining_s = remaining_s
        self.aws_request_id = request_id

    def get_remaining_time_in_millis(self) -> int:
        return int(self.remaining_s * 1000)


def runtime(rig: Rig, **kw: Any) -> Runtime:
    kw.setdefault("emitter", emf.Emitter("dev", "p", stream=rig.emf_out))
    return Runtime(
        config=rig.cfg, sqs=kw.pop("sqs", rig.sqs), s3=rig.s3, ssm=kw.pop("ssm", None),
        function="aegc-dev-p", function_version="$LATEST", memory_mib=2048, log_stream=STREAM,
        scratch=Path(rig.cfg.scratch), **kw,
    )  # fmt: skip


def event(rig: Rig, n: int = 1) -> dict[str, Any]:
    """What the event source mapping hands the function: received messages as records."""
    got: list[dict[str, Any]] = []
    while len(got) < n:
        r = rig.sqs.receive_message(
            QueueUrl=rig.tasks, MaxNumberOfMessages=n - len(got), WaitTimeSeconds=1,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )  # fmt: skip
        got += r.get("Messages", [])
    return {
        "Records": [
            {
                "messageId": m["MessageId"],
                "receiptHandle": m["ReceiptHandle"],
                "body": m["Body"],
                "attributes": {
                    "ApproximateReceiveCount": m["Attributes"].get("ApproximateReceiveCount", "1")
                },
                "eventSource": "aws:sqs",
            }
            for m in got
        ]
    }


@pytest.mark.timeout(120)
def test_runs_a_task_with_lambda_provenance(rig: Rig) -> None:
    rt = runtime(rig)
    tid = rig.submit(double, 21)
    assert rt.invoke(event(rig), Context()) == {"batchItemFailures": []}
    res = rig.final(1)[tid]
    assert value_of(res, rig.s3) == (False, 42)
    assert res.worker["backend"] == "lambda" and res.worker["instance_id"] == environment_id(STREAM)
    assert rig.task_queue_empty()  # deleted, not left for the mapping
    assert not _ledger(rig)  # Lambda spend comes from Lambda's metrics, not the function role
    assert rig.metric_values("TasksCompleted", "ok") == [1.0]
    assert len(rig.metric_values("InitSeconds")) == 1  # once per environment, before any task


def _ledger(rig: Rig) -> list[dict[str, Any]]:
    keys = rig.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{ledger.PREFIX}/dev/").get("Contents", [])
    return [rig.s3.get_object(Bucket=BUCKET, Key=k["Key"]) for k in keys]


@pytest.mark.timeout(120)
def test_walltime_is_clamped_to_the_remaining_time(rig: Rig) -> None:
    rt = runtime(rig)
    tid = rig.submit(sleeper, 30, walltime_s=600)
    out = rt.invoke(event(rig), Context(remaining_s=23))  # 23 s left, 20 s kept: 3 s to run
    assert out == {"batchItemFailures": []}  # the walltime failure is a result, posted
    is_err, text = value_of(rig.final(1)[tid], rig.s3)
    assert is_err and "walltime" in str(text).lower()


@pytest.mark.timeout(60)
def test_no_time_left_releases_the_record(rig: Rig) -> None:
    rt = runtime(rig)
    rig.submit(double, 1)
    ev = event(rig)
    out = rt.invoke(ev, Context(remaining_s=10))
    assert out == {"batchItemFailures": [{"itemIdentifier": ev["Records"][0]["messageId"]}]}
    assert not rig.task_queue_empty()  # visible again at once


@pytest.mark.timeout(60)
def test_a_halted_deployment_refuses_records(rig: Rig) -> None:
    ssm = rig.aws("ssm")
    rig.cfg = replace(rig.cfg, state_parameter="/aegc/dev/state")
    state.write_state(ssm, "dev", state.State(mode="capped", reason="max_usd_day", by="test"))
    rt = runtime(rig, ssm=ssm)
    tid = rig.submit(double, 2)
    assert rt.invoke(event(rig), Context()) == {"batchItemFailures": []}
    is_err, text = value_of(rig.final(1)[tid], rig.s3)
    assert is_err and "spend limit" in str(text)
    assert rig.task_queue_empty()


@pytest.mark.timeout(120)
def test_unposted_results_and_bad_records_are_batch_failures(rig: Rig) -> None:
    class NoResults:
        """SQS that can't send to the result queue (but still deletes and changes visibility)."""

        def __init__(self, sqs: Any) -> None:
            self._sqs = sqs

        def send_message(self, **kw: Any) -> Any:
            raise RuntimeError("result queue gone")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._sqs, name)

    rt = runtime(rig, sqs=NoResults(rig.sqs))
    rig.submit(double, 3)
    rig.sqs.send_message(QueueUrl=rig.tasks, MessageBody="not a task")
    ev = event(rig, 2)
    out = rt.invoke(ev, Context())
    assert sorted(f["itemIdentifier"] for f in out["batchItemFailures"]) == sorted(
        r["messageId"] for r in ev["Records"]
    )


def test_environment_ids_are_ledger_safe() -> None:
    assert environment_id(STREAM) == "lambda-0123456789abcdef0123456789abcdef"
    assert environment_id("") == "lambda-unknown"
    assert "/" not in environment_id("a/b/[1]c/d")


@pytest.mark.timeout(60)
def test_a_started_record_is_invisible_until_just_past_the_deadline(rig: Rig) -> None:
    # Review: the queue's visibility is 6x the timeout (AWS's advice for throttling); each
    # record's own is set to the invocation's remaining time plus a minute when it starts.
    calls: list[dict[str, Any]] = []
    rig.sqs.meta.events.register(
        "provide-client-params.sqs.ChangeMessageVisibility",
        lambda params, **_: calls.append(dict(params)),
    )
    rt = runtime(rig)
    rig.submit(double, 4)
    rt.invoke(event(rig), Context(remaining_s=300))
    assert calls and calls[0]["VisibilityTimeout"] == 300 + VISIBILITY_SLACK_S


def test_runtime_api_loop() -> None:
    """``aegc-worker lambda`` against a stand-in for the Runtime API."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from aegc.worker import lambda_runtime

    events = [{"Records": [{"messageId": "m1"}]}, {"boom": True}]
    posted: list[tuple[str, dict[str, Any], str | None]] = []

    class Api(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(events.pop(0)).encode()
            self.send_response(200)
            self.send_header("Lambda-Runtime-Aws-Request-Id", f"r{len(events)}")
            self.send_header("Lambda-Runtime-Deadline-Ms", str(int(time.time() * 1000) + 60_000))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            n = int(self.headers["Content-Length"])
            posted.append((self.path, json.loads(self.rfile.read(n)),
                           self.headers.get("Lambda-Runtime-Function-Error-Type")))  # fmt: skip
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a: Any) -> None: ...

    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:

        def handler(event: dict[str, Any], ctx: lambda_runtime.Context) -> dict[str, Any]:
            if event.get("boom"):
                raise ValueError("bad event")
            assert 50_000 < ctx.get_remaining_time_in_millis() <= 60_000
            return {"batchItemFailures": [], "id": ctx.aws_request_id}

        api = lambda_runtime.RuntimeApi(f"127.0.0.1:{server.server_address[1]}")
        assert lambda_runtime.serve(handler, api=api, max_invocations=2) == 2
    finally:
        server.shutdown()
    (ok_path, ok, _), (err_path, err, kind) = posted
    assert ok_path == "/2018-06-01/runtime/invocation/r1/response" and ok["id"] == "r1"
    assert err_path == "/2018-06-01/runtime/invocation/r0/error" and kind == "Unhandled"
    assert err["errorType"] == "ValueError" and err["errorMessage"] == "bad event"
