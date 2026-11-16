"""Task isolation (#32): task processes get the task role from the agent, not the worker role."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aegc import payloads, protocol
from aegc.infra import userdata
from aegc.protocol import ResultMessage
from aegc.worker import credentials, runner
from aegc.worker.testing import pack_task, unpack_result
from tests.unit.test_engine import BUCKET, EID, Env, _post, submit, triple
from tests.unit.test_engine import env as env  # the fixture


class FakeSts:
    def __init__(self) -> None:
        self.calls = 0

    def assume_role(self, **kw: Any) -> dict[str, Any]:
        self.calls += 1
        assert kw["DurationSeconds"] == credentials.SESSION_S
        return {
            "Credentials": {
                "AccessKeyId": f"ASIATASK{self.calls}",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }


def test_credentials_server_speaks_the_container_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.credentials import ContainerProvider

    sts = FakeSts()
    creds = credentials.TaskCredentials(
        sts=sts, role_arn="arn:aws:iam::1:role/aegc/t", session_name="s",
        token_file=tmp_path / "state" / "task-token",
    )  # fmt: skip
    env = creds.start()
    try:
        assert oct(os.stat(env["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"]).st_mode)[-3:] == "444"
        got = ContainerProvider(environ=env).load()  # what boto3 in a task does
        assert got is not None
        frozen = got.get_frozen_credentials()
        assert frozen.access_key == "ASIATASK1" and frozen.token == "token"
        # Without the token, or on another path: refused.
        import urllib.error
        import urllib.request

        url = env["AWS_CONTAINER_CREDENTIALS_FULL_URI"]
        for headers in ({}, {"Authorization": "nope"}):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)
            assert e.value.code == 403
    finally:
        creds.stop()


def test_credentials_renew_before_expiry() -> None:
    now = [1_000_000.0]
    sts = FakeSts()
    creds = credentials.TaskCredentials(
        sts=sts, role_arn="r", session_name="s", token_file=Path("/nonexistent"),
        clock=lambda: now[0],
    )  # fmt: skip
    creds._expires = 0
    creds.current()
    first_expiry = creds._expires
    now[0] = first_expiry - credentials.RENEW_BEFORE_S - 60  # plenty left: cached
    assert creds.current()["AccessKeyId"] == "ASIATASK1"
    now[0] = first_expiry - credentials.RENEW_BEFORE_S + 1  # close to expiry: renewed
    assert creds.current()["AccessKeyId"] == "ASIATASK2"


def test_renewal_failure_keeps_valid_credentials() -> None:
    now = [0.0]
    sts = FakeSts()
    creds = credentials.TaskCredentials(
        sts=sts, role_arn="r", session_name="s", token_file=Path("/x"), clock=lambda: now[0]
    )
    creds.current()
    now[0] = creds._expires - 60

    def broken(**_: Any) -> Any:
        raise RuntimeError("throttled")

    sts.assume_role = broken  # type: ignore[method-assign]
    assert creds.current()["AccessKeyId"] == "ASIATASK1"  # still valid for a minute
    now[0] = creds._expires + 1
    with pytest.raises(RuntimeError):
        creds.current()


def test_drop_privileges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(os, "setgroups", lambda g: calls.append(("setgroups", g)))
    monkeypatch.setattr(os, "setgid", lambda g: calls.append(("setgid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: calls.append(("setuid", u)))
    monkeypatch.setenv("HOME", "/root")
    runner.drop_privileges(57439, str(tmp_path))
    # Groups and gid before uid (after setuid it couldn't change them).
    assert [c[0] for c in calls] == ["setgroups", "setgid", "setuid"]
    assert calls[0][1] == [] and calls[2][1] == 57439
    assert os.environ["HOME"] != "/root"


def where_env() -> dict[str, str | None]:
    import os

    keys = ("AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_EC2_METADATA_DISABLED", "AEGC_TASK_UUID")
    return {k: os.environ.get(k) for k in keys}


@pytest.mark.timeout(120)
def test_task_env_reaches_the_task_and_results_come_over_a_pipe(tmp_path: Path) -> None:
    tid = str(uuid.uuid4())
    out = runner.run_task(
        ctx=runner.context(), task_body=pack_task(where_env, task_uuid=tid), task_uuid=tid,
        endpoint_id=str(uuid.uuid4()), run_dir=tmp_path / "t", walltime_s=60,
        task_env={"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:1/x"},
    )  # fmt: skip
    assert out.outcome == "result" and out.result is not None
    is_err, value = unpack_result(out.result)
    assert not is_err and value["AWS_CONTAINER_CREDENTIALS_FULL_URI"] == "http://127.0.0.1:1/x"
    assert value["AEGC_TASK_UUID"] == tid
    assert not (tmp_path / "t" / ".aegc-result").exists()  # nothing a neighbour could overwrite


def big() -> bytes:
    return b"x" * (5 * 2**20)


@pytest.mark.timeout(120)
def test_large_results_dont_deadlock_the_pipe(tmp_path: Path) -> None:
    tid = str(uuid.uuid4())
    t0 = time.monotonic()
    out = runner.run_task(
        ctx=runner.context(), task_body=pack_task(big, task_uuid=tid), task_uuid=tid,
        endpoint_id=str(uuid.uuid4()), run_dir=tmp_path / "t", walltime_s=60,
    )  # fmt: skip
    assert out.outcome == "result" and time.monotonic() - t0 < 30
    assert unpack_result(out.result or b"")[1] == big()


def test_user_data_isolates_only_isolated_pools() -> None:
    def render(isolate: bool) -> str:
        return userdata.render(userdata.WorkerBoot(
            deployment="d", pool="p", image="r/x@sha256:" + "a" * 64, log_group="/aegc/d",
            worker_config={}, isolate=isolate))  # fmt: skip

    iso, plain = render(True), render(False)
    assert "-m owner --uid-owner 57439 -j REJECT" in iso and "169.254.169.254" in iso
    assert "--user 0 --security-opt no-new-privileges --cap-drop ALL" in iso
    assert "HB=/run/aegc/heartbeat" in iso and "-v /run/aegc:/run/aegc" in iso
    for absent in ("--uid-owner", "--user 0", "/run/aegc"):
        assert absent not in plain
    assert "HB=/scratch/.aegc-heartbeat" in plain


@pytest.mark.timeout(60)
def test_engine_ignores_results_pointing_at_other_keys(env: Env) -> None:
    # #32: a forged result pointing at another task's input (whose size and hash a task can
    # compute) must not be returned, or deleted by the engine.
    e = env.engine()
    try:
        f = submit(e, triple, 1)
        tid = str(f.gc_task_id)
        victim = protocol.task_payload_key("dev", tid, 1)
        env.s3.put_object(Bucket=BUCKET, Key=victim, Body=b"someone's input")
        forged = payloads.make_payload(b"someone's input", s3=env.s3, bucket=BUCKET, key=victim,
                                       inline_limit=0)  # fmt: skip
        _post(env, e, ResultMessage(kind="result", task_uuid=tid, attempt=1,
                                    endpoint_id=str(EID), result=forged))  # fmt: skip
        time.sleep(3)
        assert not f.done()
        assert env.s3.get_object(Bucket=BUCKET, Key=victim)["Body"].read() == b"someone's input"
        real = payloads.make_payload(
            b"packed-ok",
            s3=env.s3,
            bucket=BUCKET,
            key=protocol.result_payload_key("dev", tid, 1),
            inline_limit=0,
        )
        _post(env, e, ResultMessage(kind="result", task_uuid=tid, attempt=1,
                                    endpoint_id=str(EID), result=real))  # fmt: skip
        assert f.result(timeout=15) == b"packed-ok"
    finally:
        e.shutdown()


def stdout_is_stderr() -> bool:
    import os

    a, b = os.fstat(1), os.fstat(2)
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


@pytest.mark.timeout(120)
def test_task_stdout_goes_to_stderr(tmp_path: Path) -> None:
    # Review: the agent's stdout carries EMF lines; a task's partial line there would corrupt one.
    tid = str(uuid.uuid4())
    out = runner.run_task(
        ctx=runner.context(), task_body=pack_task(stdout_is_stderr, task_uuid=tid), task_uuid=tid,
        endpoint_id=str(uuid.uuid4()), run_dir=tmp_path / "t", walltime_s=60,
    )  # fmt: skip
    assert unpack_result(out.result or b"") == (False, True)


def medium() -> bytes:
    return b"y" * 20_000


@pytest.mark.timeout(120)
def test_oversized_results_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Review: a hostile child could send gigabytes over the pipe and exhaust the agent's memory.
    monkeypatch.setattr(runner, "MAX_RESULT_BYTES", 1000)
    tid = str(uuid.uuid4())
    out = runner.run_task(
        ctx=runner.context(), task_body=pack_task(medium, task_uuid=tid), task_uuid=tid,
        endpoint_id=str(uuid.uuid4()), run_dir=tmp_path / "t", walltime_s=60,
    )  # fmt: skip
    assert out.outcome == "crashed" and out.result is None


def test_worker_cli_gives_tasks_a_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Found live: with instance metadata blocked, boto3 in a task had no region (NoRegionError).
    import base64
    import json as _json

    from aegc.worker import cli as wcli

    captured: dict[str, Any] = {}

    class FakeCreds:
        def __init__(self, **kw: Any) -> None: ...

        def start(self) -> dict[str, str]:
            return {"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:1/x"}

        def stop(self) -> None: ...

    class FakeAgent:
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)
            self.stats = None

        def run(self) -> str:
            return "test"

        def stop(self, reason: str) -> None: ...

        def handle_notice(self, kind: str) -> None: ...

    cfg = {"deployment": "d", "pool": "p", "region": "us-west-2", "task_queue_url": "q",
           "bucket": "b", "task_role_arn": "arn:aws:iam::1:role/aegc/t", "task_uid": 57439,
           "state_dir": str(tmp_path)}  # fmt: skip
    monkeypatch.setenv(
        "AEGC_WORKER_CONFIG_B64", base64.b64encode(_json.dumps(cfg).encode()).decode()
    )
    monkeypatch.setattr("aegc.worker.credentials.TaskCredentials", FakeCreds)
    monkeypatch.setattr("aegc.worker.agent.Agent", FakeAgent)
    monkeypatch.setattr("aegc.worker.imds.Imds.identity", lambda self: {})
    assert wcli.main(["run"]) == 0
    env = captured["task_env"]
    assert env["AWS_DEFAULT_REGION"] == env["AWS_REGION"] == "us-west-2"
