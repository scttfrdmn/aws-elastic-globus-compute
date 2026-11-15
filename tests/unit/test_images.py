import hashlib
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
    tag = images.image_tag("3.14", SPEC, "0123abcd")
    assert tag == "py3.14-gce4.21.0-aegc0.1.0-b2026.11.13.154112-w0123abcd"
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
    assert rec.tag.endswith("-w" + hashlib.sha256(b"w").hexdigest()[:8])


def test_wheel_hash_follows_the_code(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"one")
    first = images.wheel_hash(tmp_path)
    assert len(first) == 8 and images.wheel_hash(tmp_path) == first
    (tmp_path / "a.whl").write_bytes(b"two")
    assert images.wheel_hash(tmp_path) != first


def test_existing_image(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    images.ensure_repository(ecr, "geospatial", Report())
    assert images.existing_image(ecr, "geospatial", "py3.14-x") is None


# --- in-use protection (#27) -------------------------------------------------------------------


def push_fake(ecr: Any, env: str, tag: str, n: int) -> str:
    """Put a distinct (empty) image manifest into aegc/<env>; returns ``<uri>@sha256:...``."""
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "size": n,
                "digest": "sha256:" + f"{n:064x}",
            },
            "layers": [],
        }
    )
    name = naming.image_repository(env)
    digest = ecr.put_image(repositoryName=name, imageManifest=manifest, imageTag=tag)["image"][
        "imageId"
    ]["imageDigest"]
    uri = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]["repositoryUri"]
    return f"{uri}@{digest}"


def tags_of(ecr: Any, ref: str) -> set[str]:
    name = ref.split("/", 1)[1].split("@")[0]
    d = ecr.describe_images(repositoryName=name, imageIds=[{"imageDigest": ref.split("@")[1]}])
    return set(d["imageDetails"][0].get("imageTags", []))


def test_lifecycle_keeps_inuse_images_first() -> None:
    rules = sorted(images.LIFECYCLE["rules"], key=lambda r: r["rulePriority"])
    assert [r["rulePriority"] for r in rules] == [1, 2, 3]
    keep = rules[0]["selection"]
    assert keep["tagStatus"] == "tagged" and keep["tagPrefixList"] == ["inuse-"]
    assert keep["countType"] == "imageCountMoreThan" and keep["countNumber"] >= 10_000
    # AWS requires a tagStatus=any rule to have the highest rulePriority (evaluated last).
    assert rules[-1]["selection"]["tagStatus"] == "any"


def test_ensure_repository_reapplies_the_lifecycle(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    images.ensure_repository(ecr, "geospatial", Report())
    old = {"rules": [images.LIFECYCLE["rules"][2] | {"rulePriority": 1}]}  # a v0.1 repository
    ecr.put_lifecycle_policy(repositoryName="aegc/geospatial", lifecyclePolicyText=json.dumps(old))
    images.ensure_repository(ecr, "geospatial", Report())
    text = ecr.get_lifecycle_policy(repositoryName="aegc/geospatial")["lifecyclePolicyText"]
    assert json.loads(text) == images.LIFECYCLE


def test_inuse_tags_are_exact_per_deployment() -> None:
    ref = "r/aegc/geospatial@sha256:" + "0123456789ab" + "f" * 52
    assert images.inuse_tag("dev", ref) == "inuse-dev-0123456789ab"
    assert images._is_inuse_tag("inuse-dev-0123456789ab", "dev")
    assert not images._is_inuse_tag("inuse-dev-x-0123456789ab", "dev")  # deployment dev-x
    assert not images._is_inuse_tag("inuse-dev-0123456789ab", "de")
    with pytest.raises(ValueError):
        images.inuse_tag("dev", "r/aegc/geospatial:latest")


def test_protect_and_release(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    images.ensure_repository(ecr, "geospatial", Report())
    one = push_fake(ecr, "geospatial", "build-1", 1)
    two = push_fake(ecr, "geospatial", "build-2", 2)

    r = Report()
    tag = images.protect(ecr, "geospatial", one, "dev", report=r)
    assert tag == images.inuse_tag("dev", one)
    assert tags_of(ecr, one) == {"build-1", tag}  # an immutable repository accepts a new tag
    assert images.protect(ecr, "geospatial", one, "dev", report=r) == tag  # idempotent
    assert [a.outcome for a in r.actions] == ["created", "exists"]
    prod = images.protect(ecr, "geospatial", one, "prod")
    newer = images.protect(ecr, "geospatial", two, "dev")

    dry = images.release(ecr, "geospatial", "dev", {newer}, dry_run=True)
    assert dry == [tag] and tag in tags_of(ecr, one)
    assert images.release(ecr, "geospatial", "dev", {newer}) == [tag]
    assert tags_of(ecr, one) == {"build-1", prod}  # the image stays, and so does prod's tag
    assert tags_of(ecr, two) == {"build-2", newer}
    assert images.release(ecr, "geospatial", "dev") == [newer]
    assert images.inuse_tags(ecr, "dev") == {} and images.inuse_tags(ecr, "prod") == {
        "aegc/geospatial": [prod]
    }
    assert images.release(ecr, "no-such-env", "dev") == []


def test_release_never_deletes_an_image(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    images.ensure_repository(ecr, "geospatial", Report())
    ref = push_fake(ecr, "geospatial", "build-1", 1)
    tag = images.protect(ecr, "geospatial", ref, "dev")
    ecr.batch_delete_image(repositoryName="aegc/geospatial", imageIds=[{"imageTag": "build-1"}])
    r = Report()
    assert images.release(ecr, "geospatial", "dev", report=r) == []
    assert tags_of(ecr, ref) == {tag}
    assert [a.outcome for a in r.actions] == ["kept"]


def test_protect_refuses_missing_and_foreign(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    uri = images.ensure_repository(ecr, "geospatial", Report())
    with pytest.raises(RuntimeError, match="gone"):
        images.protect(ecr, "geospatial", f"{uri}@sha256:" + "a" * 64, "dev")
    with pytest.raises(ValueError, match="isn't in"):
        images.protect(ecr, "geospatial", "quay.io/x/y@sha256:" + "a" * 64, "dev")
    ecr.create_repository(repositoryName="aegc/other")  # untagged: not aegc's
    with pytest.raises(images.OwnershipError):
        images.protect(ecr, "other", f"{uri[: -len('geospatial')]}other@sha256:" + "a" * 64, "dev")


@pytest.mark.xfail(
    strict=True,
    reason="substrate gap (not filed): no StartLifecyclePolicyPreview, so the rule order can't be "
    "evaluated here. AWS documents that an image matching a higher-priority rule's tag selection "
    "can't be expired by a lower-priority one.",
)
def test_lifecycle_preview_keeps_old_inuse_image(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    images.ensure_repository(ecr, "geospatial", Report())
    old = push_fake(ecr, "geospatial", "build-0", 100)
    images.protect(ecr, "geospatial", old, "dev")
    for n in range(1, 13):  # 12 newer builds: the old one is past "keep the 10 most recent"
        push_fake(ecr, "geospatial", f"build-{n}", 100 + n)
    ecr.start_lifecycle_policy_preview(repositoryName="aegc/geospatial")
    preview = ecr.get_lifecycle_policy_preview(repositoryName="aegc/geospatial")
    expired = {r["imageDigest"] for r in preview["previewResults"]}
    assert old.split("@")[1] not in expired and len(expired) == 2
