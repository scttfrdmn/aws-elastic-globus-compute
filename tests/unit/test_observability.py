"""Worker observability (#39, #40): JSON log lines, EMF documents, provenance in results."""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator

import pytest

from aegc.infra import userdata
from aegc.worker import emf, jsonlog, provenance


@pytest.fixture
def clean_context() -> Iterator[None]:
    saved = jsonlog.context()
    yield
    jsonlog._context.clear()
    jsonlog._context.update(saved)


# --- JSON logs ---------------------------------------------------------------------------------


def test_json_lines_carry_context_and_extra(clean_context: None) -> None:
    out = io.StringIO()
    logger = logging.getLogger("aegc.test.jsonlog")
    handler = logging.StreamHandler(out)
    handler.setFormatter(jsonlog.JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        jsonlog.set_context(deployment="dev", pool="p", instance_id="i-1", type="c8g.large")
        jsonlog.set_context(az="us-west-2a", lifecycle="spot", type="c8g.xlarge")
        jsonlog.set_context(lifecycle="")  # empty: dropped
        logger.info("claimed %s (delivery %d)", "t:1", 2, extra={"task_uuid": "t", "attempt": 1})
        try:
            raise RuntimeError("bad")
        except RuntimeError:
            logger.exception("runner failed for %s", "t:1")
    finally:
        logger.removeHandler(handler)
    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert first["msg"] == "claimed t:1 (delivery 2)"  # the same wording as before
    assert (first["level"], first["logger"]) == ("INFO", "aegc.test.jsonlog")
    assert first["ts"].endswith("Z") and "T" in first["ts"]
    assert {k: first[k] for k in ("deployment", "pool", "instance_id", "type", "az")} == {
        "deployment": "dev",
        "pool": "p",
        "instance_id": "i-1",
        "type": "c8g.xlarge",
        "az": "us-west-2a",
    }
    assert "lifecycle" not in first
    assert (first["task_uuid"], first["attempt"]) == ("t", 1)
    assert second["level"] == "ERROR" and "RuntimeError: bad" in second["exc"]
    assert "task_uuid" not in second


def test_unknown_context_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="task_uuid"):
        jsonlog.set_context(task_uuid="t")  # per-task fields go in extra=


def test_configure_replaces_the_root_handlers() -> None:
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    out = io.StringIO()
    try:
        jsonlog.configure(out)
        logging.getLogger("aegc").info("hello %s", "world")
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)
    (line,) = out.getvalue().splitlines()
    assert json.loads(line)["msg"] == "hello world"


# --- EMF ---------------------------------------------------------------------------------------


def test_emf_document_shape() -> None:
    doc = emf.document(
        "dev",
        "p",
        {"TasksCompleted": 1, "ExecSeconds": 2.5},
        outcome="ok",
        properties={"instance_id": "i-1"},
        timestamp_ms=1234,
    )
    assert doc["_aws"]["Timestamp"] == 1234
    plain, counts = doc["_aws"]["CloudWatchMetrics"]
    assert plain == {
        "Namespace": "aegc",
        "Dimensions": [["Deployment", "Pool"], ["Deployment"]],
        "Metrics": [{"Name": "ExecSeconds", "Unit": "Seconds"}],
    }
    assert counts["Dimensions"] == [
        ["Deployment", "Pool"],
        ["Deployment"],
        ["Deployment", "Pool", "Outcome"],
    ]
    assert counts["Metrics"] == [{"Name": "TasksCompleted", "Unit": "Count"}]
    # Every dimension and metric named in a directive is a top-level member (the EMF rule).
    for d in doc["_aws"]["CloudWatchMetrics"]:
        for name in {n for s in d["Dimensions"] for n in s} | {m["Name"] for m in d["Metrics"]}:
            assert name in doc
    assert (doc["Deployment"], doc["Pool"], doc["Outcome"]) == ("dev", "p", "ok")
    assert doc["TasksCompleted"] == 1.0 and doc["ExecSeconds"] == 2.5
    assert doc["instance_id"] == "i-1"  # a property: logged, not a dimension


def test_emf_dimension_cardinality() -> None:
    # Only Deployment, Pool and (on task counts) Outcome: never task or instance ids.
    allowed = {("Deployment", "Pool"), ("Deployment",), ("Deployment", "Pool", "Outcome")}
    outcomes = {"TasksCompleted": "ok", "TasksFailed": "walltime"}
    for name in emf.UNITS:
        doc = emf.document("dev", "p", {name: 1}, outcome=outcomes.get(name, ""))
        for d in doc["_aws"]["CloudWatchMetrics"]:
            sets = {tuple(s) for s in d["Dimensions"]}
            assert sets <= allowed, name
            assert (("Deployment", "Pool", "Outcome") in sets) == (name in emf.TASK_COUNTS)
        assert "Outcome" in doc or name not in emf.TASK_COUNTS
    assert len(emf.UNITS) <= 15  # roughly 15 metrics per pool (#39)
    assert len({*emf.COMPLETED_OUTCOMES, *emf.FAILED_OUTCOMES}) <= 8


def test_emf_refuses_unknown_metrics_and_outcomes() -> None:
    with pytest.raises(ValueError, match="unknown metrics"):
        emf.document("dev", "p", {"TaskUuid": 1})
    with pytest.raises(ValueError, match="Outcome"):
        emf.document("dev", "p", {"TasksFailed": 1}, outcome=str(uuid.uuid4()))
    with pytest.raises(ValueError, match="Outcome"):
        emf.document("dev", "p", {"TasksCompleted": 1})


def test_emitter_writes_one_line_and_never_raises() -> None:
    out = io.StringIO()
    e = emf.Emitter("dev", "p", stream=out, properties={"instance_id": "i-1"})
    assert e.emit({"Releases": 3}, reason="interruption") is not None
    assert e.emit({"TasksFailed": 1}, outcome="nonsense") is None  # dropped, not raised
    (line,) = out.getvalue().splitlines()
    doc = json.loads(line)
    assert (
        doc["Releases"] == 3.0 and doc["instance_id"] == "i-1" and doc["reason"] == "interruption"
    )


# --- provenance --------------------------------------------------------------------------------


def _packed(details: dict[str, object] | None, *, error: bool = False) -> bytes:
    from globus_compute_common import messagepack
    from globus_compute_common.messagepack.message_types import Result, ResultErrorDetails

    return bytes(
        messagepack.pack(
            Result(
                task_id=uuid.uuid4(),
                data="payload",
                details=details,
                error_details=ResultErrorDetails(code="X", user_message="x") if error else None,
            )
        )
    )


def test_merge_keeps_existing_details() -> None:
    from globus_compute_common import messagepack

    existing = {"os": "Linux", "python_version": "3.14.0", "globus_compute_sdk_version": "4.21.0"}
    packed, is_error = provenance.merge(
        _packed(dict(existing)), {"image": "x@sha256:1", "slots": 2}
    )
    assert is_error is False
    r = messagepack.unpack(packed)
    assert r.data == "payload"
    assert r.details == {**existing, "aegc": {"image": "x@sha256:1", "slots": 2}}
    assert provenance.details(packed)["aegc"]["slots"] == 2

    packed, is_error = provenance.merge(_packed(None, error=True), {"slots": 1})
    assert is_error is True and provenance.details(packed) == {"aegc": {"slots": 1}}


def test_merge_leaves_what_it_cant_read() -> None:
    assert provenance.merge(b"not a packed message", {"a": 1}) == (b"not a packed message", None)


# --- user data -----------------------------------------------------------------------------------


def test_user_data_turns_on_emf_and_passes_the_image() -> None:
    image = "123456789012.dkr.ecr.us-west-2.amazonaws.com/aegc/geospatial@sha256:" + "a" * 64
    s = userdata.render(
        userdata.WorkerBoot(
            deployment="dev", pool="p", image=image, log_group="/aegc/dev", worker_config={}
        )
    )
    run = s[s.index("ExecStart=/usr/bin/docker run") : s.index("ExecStop=")]
    assert "--log-opt awslogs-format=json/emf" in run
    assert "-e AEGC_IMAGE=$IMAGE" in run and run.rstrip().endswith("$IMAGE aegc-worker run")
