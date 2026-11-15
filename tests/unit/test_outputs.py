"""aegc.outputs (#54) against substrate S3, and the task uuid workers export for it."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aegc import outputs
from tests.conftest import REGION
from tests.unit.test_worker import Rig, value_of
from tests.unit.test_worker import rig as rig  # the fixture

TASK = "0b9b4f6e-8a1c-4a43-9d3f-2c5a1f7e9b10"


def worker_env(monkeypatch: pytest.MonkeyPatch, grants: list[dict[str, str]]) -> None:
    cfg = {
        "deployment": "dev",
        "pool": "p",
        "region": REGION,
        "task_queue_url": "q",
        "bucket": "aegc-123456789012-us-west-2",
        "grants": grants,
    }
    monkeypatch.setenv(
        "AEGC_WORKER_CONFIG_B64", base64.b64encode(json.dumps(cfg).encode()).decode()
    )
    monkeypatch.setenv("AEGC_TASK_UUID", TASK)


@pytest.fixture
def s3(aws: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    c = aws("s3")
    for b in ("lab", "open-data"):
        c.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    worker_env(
        monkeypatch,
        [
            {"access": "read", "bucket": "open-data", "prefix": "in/"},
            {"access": "write", "bucket": "lab", "prefix": "out/"},
            {"access": "write", "bucket": "lab", "prefix": "shared/runs"},
        ],
    )
    return c


def test_put_and_get_bytes_and_text(s3: Any) -> None:
    uri = outputs.put(b"\x00\x01binary", "blob.bin")
    assert uri == f"s3://lab/out/{TASK}/blob.bin"  # the first write grant, then the task
    assert outputs.get(uri) == b"\x00\x01binary"
    assert (
        s3.get_object(Bucket="lab", Key=f"out/{TASK}/blob.bin")["Body"].read() == b"\x00\x01binary"
    )
    text = outputs.put("héllo")
    assert text.startswith(f"s3://lab/out/{TASK}/output-") and outputs.get(text) == "héllo".encode()
    assert outputs.put(b"2") != outputs.put(b"3")  # unnamed outputs don't collide


def test_put_and_download_a_file(s3: Any, tmp_path: Path) -> None:
    src = tmp_path / "result.parquet"
    data = os.urandom(9 * 1024 * 1024)  # above boto3's 8 MiB multipart threshold
    src.write_bytes(data)
    uri = outputs.put(src)
    assert uri == f"s3://lab/out/{TASK}/result.parquet"
    dest = outputs.download(uri, tmp_path / "back.parquet")
    assert dest.read_bytes() == data
    assert outputs.put(src, "renamed.bin").endswith(f"/{TASK}/renamed.bin")


def test_put_under_a_given_prefix(s3: Any) -> None:
    uri = outputs.put(b"x", "a.txt", prefix="s3://lab/shared/runs/42")
    assert uri == f"s3://lab/shared/runs/42/{TASK}/a.txt"
    assert outputs.get(uri) == b"x"
    # A prefix outside every write grant is refused before anything is sent.
    for outside in ("s3://lab/elsewhere/", "s3://open-data/in/", "s3://other/out/"):
        with pytest.raises(PermissionError, match="isn't inside a write grant"):
            outputs.put(b"x", "a.txt", prefix=outside)
    assert s3.list_objects_v2(Bucket="open-data").get("KeyCount", 0) == 0


def test_no_write_grant(aws: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch) -> None:
    aws("s3")  # substrate, in case anything did reach S3
    worker_env(monkeypatch, [{"access": "read", "bucket": "open-data", "prefix": "in/"}])
    with pytest.raises(PermissionError, match=r"aegc deploy <d> --grant write=s3://bucket/prefix"):
        outputs.put(b"x")
    monkeypatch.delenv("AEGC_WORKER_CONFIG_B64")  # not on a worker at all
    with pytest.raises(PermissionError, match="no write grant"):
        outputs.put(b"x")


def test_task_uuid_outside_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEGC_TASK_UUID", raising=False)
    assert outputs.task_uuid() == "local"


def test_uri_to_https(s3: Any) -> None:
    assert outputs.uri_to_https("s3://lab/out/a b.txt", region="us-west-2") == (
        "https://lab.s3.us-west-2.amazonaws.com/out/a%20b.txt"
    )
    signed = outputs.uri_to_https("s3://lab/out/x", expires_s=600)
    assert "X-Amz-Signature=" in signed or "Signature=" in signed
    with pytest.raises(ValueError, match="not an object"):
        outputs.get("s3://lab/")
    with pytest.raises(ValueError, match="s3://"):
        outputs.get("https://lab/x")


def task_uuid_env() -> str:
    import os

    return os.environ["AEGC_TASK_UUID"]


@pytest.mark.timeout(120)
def test_runner_exports_the_task_uuid(rig: Rig) -> None:
    a = rig.agent()
    tid = rig.submit(task_uuid_env)
    a.claim_once()
    a.wait_idle()
    assert value_of(rig.final(1)[tid], rig.s3) == (False, tid)
