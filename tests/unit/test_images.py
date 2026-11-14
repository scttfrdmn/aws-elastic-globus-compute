import json
import subprocess
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from aegc import images, naming
from aegc.infra import Report
from tests.conftest import ACCOUNT, REGION

SPEC = images.ImageSpec(env="geospatial", base_tag="2026.11.13.154112")
GUARD = resources.files("aegc") / "templates" / "worker" / "aegc_env_guard.py"


def guard(
    tmp_path: Path, before: list[dict[str, str]], after: list[dict[str, str]], allow: str
) -> subprocess.CompletedProcess[str]:
    b, a = tmp_path / "b.json", tmp_path / "a.json"
    b.write_text(json.dumps(before))
    a.write_text(json.dumps({"packages": after}))  # micromamba 2.x shape
    return subprocess.run(
        [sys.executable, str(GUARD), str(b), str(a), allow], capture_output=True, text=True
    )


def test_env_guard(tmp_path: Path) -> None:
    before = [{"name": "click", "version": "8.5.1"}, {"name": "gdal", "version": "3.13.3"}]
    ok = guard(
        tmp_path,
        before,
        [*before[1:], {"name": "click", "version": "8.3.3"}, {"name": "pyzmq", "version": "27"}],
        "click",
    )
    assert ok.returncode == 0 and "1 added" in ok.stdout and "click" in ok.stdout
    bad = guard(
        tmp_path,
        before,
        [{"name": "click", "version": "8.5.1"}, {"name": "gdal", "version": "3.12.0"}],
        "click",
    )
    assert bad.returncode == 1 and "gdal 3.13.3 -> 3.12.0" in bad.stderr
    gone = guard(tmp_path, before, [{"name": "click", "version": "8.3.3"}], "click")
    assert gone.returncode == 1 and "gdal 3.13.3 -> removed" in gone.stderr


def test_spec_validation() -> None:
    with pytest.raises(ValueError, match="immutable"):
        images.ImageSpec(env="geospatial", base_tag="latest")
    with pytest.raises(ValueError, match="source"):
        images.ImageSpec(env="x", base_tag="1", source="dockerhub")


def test_refs_and_tags() -> None:
    cached = images.base_ref(SPEC, account=ACCOUNT, region=REGION)
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    assert cached == f"{registry}/aegc-quay/aarchsci/geospatial:2026.11.13.154112"
    assert images.base_ref(SPEC, account=ACCOUNT, region=REGION, through_cache=False).startswith(
        "quay.io/aarchsci/"
    )
    assert images.image_tag("3.14", SPEC) == "py3.14-gce4.21.0-aegc0.1.0-b2026.11.13.154112"
    args = images.docker_build_args(SPEC, base="B", local_tag="L", context=Path("/ctx"))
    assert args[:6] == ["docker", "buildx", "build", "--platform", "linux/arm64", "--load"]
    assert "BASE=B" in args and "AEGC_ALLOW_CHANGE=click" in args


def test_build_context(tmp_path: Path) -> None:
    wheels = tmp_path / "w"
    wheels.mkdir()
    (wheels / "aws_elastic_globus_compute-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    images.build_context(ctx, wheel_dir=wheels)
    assert (ctx / "Dockerfile").read_text().startswith("# syntax=docker/dockerfile:1")
    assert (ctx / "aegc_env_guard.py").exists()
    assert [p.name for p in (ctx / "dist").iterdir()] == [
        "aws_elastic_globus_compute-0.1.0-py3-none-any.whl"
    ]


def test_repository_and_record(aws: Callable[[str], Any]) -> None:
    ecr, ssm = aws("ecr"), aws("ssm")
    r = Report()
    uri = images.ensure_repository(ecr, "geospatial", r)
    assert uri.endswith("/aegc/geospatial")
    assert images.ensure_repository(ecr, "geospatial", r) == uri
    assert [a.outcome for a in r.actions] == ["created", "exists"]
    repo = ecr.describe_repositories(repositoryNames=["aegc/geospatial"])["repositories"][0]
    assert repo["imageTagMutability"] == "IMMUTABLE"

    rec = images.ImageRecord(
        env="geospatial", python_minor="3.14", image=f"{uri}@sha256:" + "e" * 64, tag="t",
        base="b", gce_version="4.21.0", aegc_version="0.1.0", built_at=1,
    )  # fmt: skip
    images.record_image(ssm, rec)
    images.record_image(ssm, rec)  # overwrite is fine
    assert images.read_image(ssm, "geospatial") == rec
    tags = naming.tag_dict(
        ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId="/aegc/images/geospatial")[
            "TagList"
        ]
    )
    assert tags[naming.TAG_MANAGED] == "true"


def test_refuses_foreign_repository(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    ecr.create_repository(repositoryName="aegc/geospatial")  # untagged: not ours
    with pytest.raises(images.OwnershipError):
        images.ensure_repository(ecr, "geospatial", Report())


def test_build_orchestration_without_docker(aws: Callable[[str], Any], tmp_path: Path) -> None:
    ecr, ssm = aws("ecr"), aws("ssm")
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        out = ""
        if cmd[:2] == ["uv", "build"]:
            out_dir = Path(cmd[cmd.index("--out-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "aws_elastic_globus_compute-0.1.0-py3-none-any.whl").write_bytes(b"w")
        elif cmd[:2] == ["docker", "run"]:
            out = "3.14\n"
        elif cmd[:2] == ["docker", "inspect"]:
            remote = cmd[-1]
            out = json.dumps([remote.rsplit(":", 1)[0] + "@sha256:" + "f" * 64])
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    rec = images.build(
        SPEC,
        account=ACCOUNT,
        region=REGION,
        ecr=ecr,
        ssm=ssm,
        project_dir=tmp_path,
        run=run,
        log=lambda _: None,
    )
    assert isinstance(rec, images.ImageRecord)
    assert rec.python_minor == "3.14" and rec.image.endswith("@sha256:" + "f" * 64)
    assert images.read_image(ssm, "geospatial") == rec
    kinds = [c[1] for c in calls if c[0] == "docker"]
    assert kinds == ["login", "buildx", "run", "login", "tag", "push", "inspect"]
