"""Worker images: an aarchsci/aarchbio environment plus globus-compute-endpoint and aegc-worker.

``aegc image build`` renders ``templates/worker/Dockerfile``, builds it natively for arm64 with
buildx, reads the Python version from the built image, pushes it to ECR as ``aegc/<env>``, and
records ``{env, python_minor, image@digest}`` at ``/aegc/images/<env>`` for ``aegc deploy``.

Bases come through the ``aegc-quay`` pull-through cache (``aegc bootstrap``), so pulls stay
in-region.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from aegc import __version__, naming
from aegc.awsutil import error_code, is_not_found, paginate
from aegc.infra import Outcome, OwnershipError, Report

DEFAULT_GCE_VERSION = "4.21.0"
SOURCES = {"aarchsci": "aarchsci", "aarchbio": "aarchbio"}
#: Packages globus-compute-endpoint may downgrade in a science image (spike #2).
DEFAULT_ALLOW_CHANGE = ("click",)
#: Tag prefix that pins an image a deployment uses: ``inuse-<deployment>-<digest12>`` (#27).
INUSE_PREFIX = "inuse-"
LIFECYCLE = {
    "rules": [
        {
            # ECR applies each image's highest-priority matching rule only: an image that matches a
            # rule's tag selection can't be expired by a lower-priority rule. So any image with an
            # inuse- tag is kept, however old. The count is never reached in practice.
            "rulePriority": 1,
            "description": "aegc: keep images a deployment uses (inuse- tags)",
            "selection": {
                "tagStatus": "tagged",
                "tagPrefixList": [INUSE_PREFIX],
                "countType": "imageCountMoreThan",
                "countNumber": 10000,
            },
            "action": {"type": "expire"},
        },
        {
            "rulePriority": 2,
            "description": "aegc: drop untagged images after a day",
            "selection": {
                "tagStatus": "untagged",
                "countType": "sinceImagePushed",
                "countUnit": "days",
                "countNumber": 1,
            },
            "action": {"type": "expire"},
        },
        {
            "rulePriority": 3,
            "description": "aegc: keep the 10 most recent builds",
            "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 10},
            "action": {"type": "expire"},
        },
    ]
}

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ImageSpec:
    env: str
    base_tag: str  # an immutable aarchsci tag (YYYY.MM.DD.HHMMSS) or aarchbio <version>--<build>
    source: str = "aarchsci"
    gce_version: str = DEFAULT_GCE_VERSION
    allow_change: tuple[str, ...] = DEFAULT_ALLOW_CHANGE

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {sorted(SOURCES)}")
        if self.base_tag in {"latest", ""}:
            raise ValueError("pin an immutable base tag, not 'latest'")


@dataclass(frozen=True)
class ImageRecord:
    env: str
    python_minor: str
    image: str  # <registry>/aegc/<env>@sha256:...
    tag: str
    base: str
    gce_version: str
    aegc_version: str
    built_at: int


def registry(account: str, region: str) -> str:
    return f"{account}.dkr.ecr.{region}.amazonaws.com"


def base_ref(spec: ImageSpec, *, account: str, region: str, through_cache: bool = True) -> str:
    repo = f"{SOURCES[spec.source]}/{spec.env}:{spec.base_tag}"
    if through_cache:
        return f"{registry(account, region)}/{naming.pull_through_prefix()}/{repo}"
    return f"quay.io/{repo}"


def image_tag(python_minor: str, spec: ImageSpec, wheel_hash: str) -> str:
    """Immutable tag: Python, GCE and aegc versions, the base tag, and the aegc wheel's hash.

    The wheel hash gives each code change its own tag (hatchling wheels are reproducible).
    """
    return (
        f"py{python_minor}-gce{spec.gce_version}-aegc{__version__}-b{spec.base_tag}-w{wheel_hash}"
    )


def wheel_hash(wheel_dir: Path) -> str:
    h = hashlib.sha256()
    for whl in sorted(wheel_dir.glob("*.whl")):
        h.update(whl.read_bytes())
    return h.hexdigest()[:8]


def existing_image(ecr: Any, env: str, tag: str) -> str | None:
    """``<repo>@sha256:<digest>`` if the repository already has ``tag`` (tags are immutable)."""
    name = naming.image_repository(env)
    try:
        d = ecr.describe_images(repositoryName=name, imageIds=[{"imageTag": tag}])["imageDetails"]
    except Exception as e:
        if is_not_found(e):
            return None
        raise
    if not d:
        return None
    uri = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]["repositoryUri"]
    return f"{uri}@{d[0]['imageDigest']}"


def _owned_repository(ecr: Any, name: str) -> dict[str, Any]:
    """The repository ``name``, if aegc's bootstrap created it; raises OwnershipError if not."""
    repo = dict(ecr.describe_repositories(repositoryNames=[name])["repositories"][0])
    found = naming.tag_dict(ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"])["tags"])
    if not naming.is_owned(naming.PREFIX + "-images", found, naming.BOOTSTRAP):
        raise OwnershipError(f"ECR repository {name} exists but isn't tagged as aegc's")
    return repo


def apply_lifecycle(ecr: Any, name: str) -> None:
    """Set :data:`LIFECYCLE` on an aegc repository, so ones created before a rule existed get it.

    Always a put (it's idempotent): comparing first would need ``ecr:GetLifecyclePolicy``, which
    the builder policy doesn't grant.
    """
    ecr.put_lifecycle_policy(repositoryName=name, lifecyclePolicyText=json.dumps(LIFECYCLE))


def ensure_repository(ecr: Any, env: str, report: Report, *, dry_run: bool = False) -> str:
    name = naming.image_repository(env)
    tags = naming.tag_list(naming.tags(naming.BOOTSTRAP, "image-repo"))
    try:
        repo = _owned_repository(ecr, name)
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
    else:
        if not dry_run:
            apply_lifecycle(ecr, name)
        report.add("ecr repository", name, "exists")
        return str(repo["repositoryUri"])
    if dry_run:
        report.add("ecr repository", name, "would-create")
        return name
    repo = ecr.create_repository(
        repositoryName=name,
        imageTagMutability="IMMUTABLE",
        imageScanningConfiguration={"scanOnPush": True},
        tags=tags,
    )["repository"]
    apply_lifecycle(ecr, name)
    report.add("ecr repository", name, "created")
    return str(repo["repositoryUri"])


# --- in-use tags (#27) -------------------------------------------------------------------------


def inuse_tag(deployment: str, digest_ref: str) -> str:
    """``inuse-<deployment>-<first 12 hex digits of the digest>``."""
    _, sep, digest = digest_ref.partition("@sha256:")
    if not sep or len(digest) < 12:
        raise ValueError(f"not a pinned image reference: {digest_ref!r}")
    return f"{INUSE_PREFIX}{deployment}-{digest[:12]}"


def _is_inuse_tag(tag: str, deployment: str) -> bool:
    # Exactly inuse-<d>-<12 hex>: deployment "dev" mustn't match "inuse-dev-x-<hex>".
    head = f"{INUSE_PREFIX}{deployment}-"
    rest = tag.removeprefix(head)
    return tag.startswith(head) and len(rest) == 12 and all(c in "0123456789abcdef" for c in rest)


def protect(
    ecr: Any, env: str, digest_ref: str, deployment: str, *, report: Report | None = None
) -> str:
    """Tag the pinned image ``inuse-<d>-<sha12>`` so the lifecycle never expires it. Returns it.

    ``put_image`` with the image's existing manifest adds a tag without pushing anything; immutable
    repositories allow new tags, just not moving existing ones. Re-applies the lifecycle first.
    """
    name = naming.image_repository(env)
    repo_ref, _, digest = digest_ref.partition("@")
    if not repo_ref.endswith("/" + name):
        raise ValueError(f"image {digest_ref!r} isn't in aegc's repository {name}")
    tag = inuse_tag(deployment, digest_ref)
    _owned_repository(ecr, name)
    apply_lifecycle(ecr, name)
    got = ecr.batch_get_image(repositoryName=name, imageIds=[{"imageDigest": digest}])
    if not got.get("images"):
        raise RuntimeError(
            f"image {digest_ref} is gone from {name}; "
            f"rebuild it with `aegc image build --env {env}`"
        )
    image = got["images"][0]
    kw: dict[str, Any] = {}
    if image.get("imageManifestMediaType"):
        kw["imageManifestMediaType"] = image["imageManifestMediaType"]
    outcome: Outcome = "created"
    try:
        ecr.put_image(
            repositoryName=name,
            imageManifest=image["imageManifest"],
            imageTag=tag,
            imageDigest=digest,
            **kw,
        )
    except Exception as e:
        if error_code(e) != "ImageAlreadyExistsException":
            raise
        outcome = "exists"  # already tagged
    if report is not None:
        report.add("ecr image tag", f"{name}:{tag}", outcome)
    return tag


def release(
    ecr: Any,
    env: str,
    deployment: str,
    keep: set[str] | frozenset[str] = frozenset(),
    *,
    report: Report | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Remove ``deployment``'s ``inuse-`` tags from ``aegc/<env>``, except those in ``keep``.

    Deletes by tag, which removes the tag and leaves the image (it keeps its build tag). A tag
    that's an image's only one is kept, because deleting it would delete the image. Returns the
    tags removed (or that would be, on a dry run).
    """
    name = naming.image_repository(env)
    try:
        _owned_repository(ecr, name)
        details = list(
            paginate(
                ecr,
                "describe_images",
                "imageDetails",
                repositoryName=name,
                filter={"tagStatus": "TAGGED"},
            )
        )
    except Exception as e:
        if is_not_found(e):
            return []
        raise
    removed = []
    for d in details:
        tags = d.get("imageTags", [])
        for t in tags:
            if not _is_inuse_tag(t, deployment) or t in keep:
                continue
            if len(tags) == 1:
                if report is not None:
                    report.add("ecr image tag", f"{name}:{t}", "kept", "the image's only tag")
                continue
            if not dry_run:
                ecr.batch_delete_image(repositoryName=name, imageIds=[{"imageTag": t}])
            removed.append(t)
            if report is not None:
                report.add("ecr image tag", f"{name}:{t}", "would-delete" if dry_run else "deleted")
    return removed


def inuse_tags(ecr: Any, deployment: str) -> dict[str, list[str]]:
    """``{repository: [inuse tags of deployment]}`` across aegc's image repositories."""
    out: dict[str, list[str]] = {}
    for repo in paginate(ecr, "describe_repositories", "repositories"):
        name = str(repo["repositoryName"])
        if not name.startswith(naming.PREFIX + "/"):
            continue
        for d in paginate(
            ecr, "describe_images", "imageDetails", repositoryName=name,
            filter={"tagStatus": "TAGGED"},
        ):  # fmt: skip
            found = [t for t in d.get("imageTags", []) if _is_inuse_tag(t, deployment)]
            if found:
                out.setdefault(name, []).extend(found)
    return out


def record_image(ssm: Any, rec: ImageRecord) -> None:
    name = naming.image_parameter(rec.env)
    value = json.dumps(asdict(rec), sort_keys=True)
    try:
        ssm.get_parameter(Name=name)
        ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
    except Exception as e:
        if not is_not_found(e):
            raise
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Tags=naming.tag_list(naming.tags(naming.BOOTSTRAP, "image-record")),
        )


def read_image(ssm: Any, env: str) -> ImageRecord:
    value = ssm.get_parameter(Name=naming.image_parameter(env))["Parameter"]["Value"]
    return ImageRecord(**json.loads(value))


def build_context(dest: Path, *, wheel_dir: Path) -> Path:
    """Copy the Dockerfile, the env guard and the aegc wheel(s) into a fresh build context."""
    templates = resources.files("aegc") / "templates" / "worker"
    for name in ("Dockerfile", "aegc_env_guard.py"):
        (dest / name).write_bytes((templates / name).read_bytes())
    dist = dest / "dist"
    dist.mkdir()
    wheels = sorted(wheel_dir.glob("aws_elastic_globus_compute-*.whl"))
    if not wheels:
        raise FileNotFoundError(f"no aegc wheel in {wheel_dir}")
    shutil.copy2(wheels[-1], dist)
    return dest


def build_wheel(project_dir: Path, out_dir: Path, run: Runner = subprocess.run) -> Path:
    run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(project_dir)],
        check=True,
        text=True,
    )
    return out_dir


def docker_build_args(spec: ImageSpec, *, base: str, local_tag: str, context: Path) -> list[str]:
    return [
        "docker", "buildx", "build",
        "--platform", "linux/arm64",
        "--load",
        "--build-arg", f"BASE={base}",
        "--build-arg", f"GCE_VERSION={spec.gce_version}",
        "--build-arg", f"AEGC_ALLOW_CHANGE={','.join(spec.allow_change)}",
        "--label", f"org.opencontainers.image.base.name={base}",
        "--label", f"science.aarch.aegc.version={__version__}",
        "--label", f"science.aarch.aegc.gce={spec.gce_version}",
        "-t", local_tag,
        str(context),
    ]  # fmt: skip


def python_minor_of(image: str, run: Runner = subprocess.run) -> str:
    out = run(
        [
            "docker", "run", "--rm", "--entrypoint", "/opt/conda/bin/python", image,
            "-c", "import sys; print('%d.%d' % sys.version_info[:2])",
        ],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    minor = out.stdout.strip().splitlines()[-1]
    if not minor.count(".") == 1:
        raise RuntimeError(f"couldn't read the Python version from {image}: {out.stdout!r}")
    return minor


def docker_login(ecr: Any, run: Runner = subprocess.run) -> None:
    import base64

    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    run(
        ["docker", "login", "--username", user, "--password-stdin", auth["proxyEndpoint"]],
        input=password, check=True, text=True, capture_output=True,
    )  # fmt: skip


def push(local_tag: str, remote: str, run: Runner = subprocess.run) -> str:
    """Tag and push; returns ``<repo>@sha256:<digest>``."""
    run(["docker", "tag", local_tag, remote], check=True, text=True)
    run(["docker", "push", remote], check=True, text=True)
    out = run(
        ["docker", "inspect", "--format", "{{json .RepoDigests}}", remote],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    repo = remote.rsplit(":", 1)[0]
    for d in json.loads(out.stdout):
        if d.startswith(repo + "@sha256:"):
            return str(d)
    raise RuntimeError(f"no digest for {remote} after push: {out.stdout!r}")


def build(
    spec: ImageSpec,
    *,
    account: str,
    region: str,
    ecr: Any,
    ssm: Any,
    project_dir: Path,
    push_image: bool = True,
    through_cache: bool = True,
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
) -> ImageRecord | str:
    """Build, and by default push and record, a worker image. Returns the record or local tag."""
    base = base_ref(spec, account=account, region=region, through_cache=through_cache)
    local_tag = f"aegc-build/{spec.env}:{spec.base_tag}"
    with tempfile.TemporaryDirectory(prefix="aegc-image-") as tmp:
        wheels = build_wheel(project_dir, Path(tmp) / "wheels", run=run)
        whash = wheel_hash(wheels)
        ctx_dir = Path(tmp) / "ctx"
        ctx_dir.mkdir()
        ctx = build_context(ctx_dir, wheel_dir=wheels)
        if through_cache:
            docker_login(ecr, run=run)  # the base comes from the ECR pull-through cache
        log(f"building {spec.env} from {base}")
        run(
            docker_build_args(spec, base=base, local_tag=local_tag, context=ctx),
            check=True,
            text=True,
        )
    minor = python_minor_of(local_tag, run=run)
    log(f"built {local_tag}: Python {minor}")
    if not push_image:
        return local_tag
    report = Report()
    repo_uri = ensure_repository(ecr, spec.env, report)
    docker_login(ecr, run=run)
    tag = image_tag(minor, spec, whash)
    pinned = existing_image(ecr, spec.env, tag)
    if pinned:
        log(f"{repo_uri}:{tag} already exists (same aegc code and base); reusing it")
    else:
        pinned = push(local_tag, f"{repo_uri}:{tag}", run=run)
    rec = ImageRecord(
        env=spec.env,
        python_minor=minor,
        image=pinned,
        tag=tag,
        base=base,
        gce_version=spec.gce_version,
        aegc_version=__version__,
        built_at=int(time.time()),
    )
    record_image(ssm, rec)
    log(f"pushed {pinned}; recorded {naming.image_parameter(spec.env)}")
    return rec


def commands_used() -> Sequence[str]:
    """External tools ``aegc image build`` needs on PATH."""
    return ("uv", "docker")
