"""Worker images: a science environment plus globus-compute-endpoint and aegc-worker.

``aegc image build`` builds natively for arm64 with buildx, reads the Python version from the
built image, pushes it to ECR as ``aegc/<env>``, and records it at ``/aegc/images/<env>`` for
``aegc deploy``. One record per Python minor: an env can have images for several.

Bases:

- **aarchsci/aarchbio** (``--source``, ``--base-tag``): conda environments, built with
  ``templates/worker/Dockerfile`` through the ``aegc-quay`` pull-through cache (in-region pulls).
- **any arm64 image** (``--base REF``): if it's a micromamba/conda image, the same Dockerfile;
  otherwise ``templates/worker/Dockerfile.pip``, which pip-installs into the base's Python without
  changing its packages.

``aegc image register`` records an image you built yourself (``FROM`` yours, plus
``pip install`` the aegc wheel and globus-compute-endpoint) after checking it.
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


#: The uid user data chowns the host's /scratch to (aarchsci's mambauser).
WORKER_UID = 57439
CONDA_PYTHON = "/opt/conda/bin/python"


@dataclass(frozen=True)
class ImageSpec:
    env: str
    base_tag: str = (
        ""  # an immutable aarchsci tag (YYYY.MM.DD.HHMMSS) or aarchbio <version>--<build>
    )
    source: str = "aarchsci"
    gce_version: str = DEFAULT_GCE_VERSION
    allow_change: tuple[str, ...] = DEFAULT_ALLOW_CHANGE
    base: str = ""  # any arm64 image ref, pinned (``repo@sha256:...`` or an immutable tag)
    python: str = ""  # interpreter in a pip base (default: python3)

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {sorted(SOURCES)}")
        if bool(self.base) == bool(self.base_tag):
            raise ValueError("give either --base REF or --base-tag TAG (with --source)")
        if self.base_tag == "latest":
            raise ValueError("pin an immutable base tag, not 'latest'")
        if self.base and not _pinned(self.base):
            raise ValueError(
                f"pin the base image ({self.base!r}): use repo@sha256:... or an immutable tag, "
                "not latest or no tag"
            )

    @property
    def base_id(self) -> str:
        """Short, tag-safe identity of the base: the aarch tag, or a hash of the ref."""
        if self.base_tag:
            return self.base_tag
        return hashlib.sha256(self.base.encode()).hexdigest()[:12]


def _pinned(ref: str) -> bool:
    if "@sha256:" in ref:
        return True
    last = ref.rsplit("/", 1)[-1]
    return ":" in last and not last.endswith(":latest")


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
    base_kind: str = "conda"  # conda, pip or registered
    python: str = CONDA_PYTHON


def registry(account: str, region: str) -> str:
    return f"{account}.dkr.ecr.{region}.amazonaws.com"


def base_ref(spec: ImageSpec, *, account: str, region: str, through_cache: bool = True) -> str:
    if spec.base:  # straight from its registry: workers only ever pull the aegc/<env> copy
        return spec.base
    repo = f"{SOURCES[spec.source]}/{spec.env}:{spec.base_tag}"
    if through_cache:
        return f"{registry(account, region)}/{naming.pull_through_prefix()}/{repo}"
    return f"quay.io/{repo}"


def image_tag(
    python_minor: str, spec: ImageSpec, wheel_hash: str, base_id: str | None = None
) -> str:
    """Immutable tag: Python, GCE and aegc versions, the base, and the aegc wheel's hash.

    The wheel hash gives each code change its own tag (hatchling wheels are reproducible).
    ``base_id`` identifies a free-form base by its image id, so a moved tag builds anew.
    """
    b = base_id or spec.base_id
    return f"py{python_minor}-gce{spec.gce_version}-aegc{__version__}-b{b}-w{wheel_hash}"


def wheel_hash(wheel_dir: Path) -> str:
    h = hashlib.sha256()
    for whl in sorted(wheel_dir.glob("*.whl")):
        h.update(whl.read_bytes())
    return h.hexdigest()[:8]


def existing_image(ecr: Any, env: str, tag: str, *, name: str = "") -> str | None:
    """``<repo>@sha256:<digest>`` if the repository already has ``tag`` (tags are immutable)."""
    name = name or naming.image_repository(env)
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


def ensure_repository(
    ecr: Any, env: str, report: Report, *, dry_run: bool = False, name: str = ""
) -> str:
    """The worker image repository ``aegc/<env>`` (or ``name``), created if it's missing."""
    name = name or naming.image_repository(env)
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


#: The record fields aegc 0.1/0.2 know: they read the single record with ImageRecord(**value).
LEGACY_FIELDS = (
    "env", "python_minor", "image", "tag", "base", "gce_version", "aegc_version", "built_at",
)  # fmt: skip


def _put(ssm: Any, name: str, value: str) -> None:
    """Create (tagged as aegc's) or overwrite an image-record parameter."""
    try:
        ssm.get_parameter(Name=name)
    except Exception as e:
        if not is_not_found(e):
            raise
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Tags=naming.tag_list(naming.tags(naming.BOOTSTRAP, "image-record")),
        )
        return
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


def _write_records(ssm: Any, env: str, records: dict[str, ImageRecord]) -> None:
    value = {"records": {m: asdict(r) for m, r in sorted(records.items())}}
    _put(ssm, naming.image_records_parameter(env), json.dumps(value, sort_keys=True))


def _write_legacy(ssm: Any, rec: ImageRecord) -> None:
    """The single record 0.1/0.2 CLIs read, in their shape (they reject unknown fields)."""
    value = {k: v for k, v in asdict(rec).items() if k in LEGACY_FIELDS}
    _put(ssm, naming.image_parameter(rec.env), json.dumps(value, sort_keys=True))


def record_image(ssm: Any, rec: ImageRecord) -> None:
    """Record ``rec`` as the env's image for its Python minor (other minors are kept).

    Two builds of one env at the same moment can lose one record (SSM has no compare-and-set);
    rerun the lost one.
    """
    try:
        records = read_images(ssm, rec.env)
    except Exception as e:
        if not is_not_found(e):
            raise
        records = {}
    records[rec.python_minor] = rec
    _write_records(ssm, rec.env, records)
    _write_legacy(ssm, rec)


def forget(ssm: Any, env: str, python_minor: str) -> dict[str, ImageRecord]:
    """Drop the env's record for ``python_minor``; returns the records left. The image itself
    stays in ECR (its lifecycle policy expires it unless a deployment protects it)."""
    records = read_images(ssm, env)
    if python_minor not in records:
        raise KeyError(f"no {env!r} image for Python {python_minor}; have {sorted(records)}")
    del records[python_minor]
    for name in (naming.image_records_parameter(env), naming.image_parameter(env)):
        _check_owned(ssm, name)
    if records:
        _write_records(ssm, env, records)
        _write_legacy(ssm, max(records.values(), key=lambda r: r.built_at))
    else:
        for name in (naming.image_records_parameter(env), naming.image_parameter(env)):
            try:
                ssm.delete_parameter(Name=name)
            except Exception as e:
                if not is_not_found(e):
                    raise
    return records


def _check_owned(ssm: Any, name: str) -> None:
    try:
        tags = naming.tag_dict(
            ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
        )
    except Exception as e:
        if is_not_found(e) or error_code(e) == "InvalidResourceId":
            return
        raise
    if tags.get(naming.TAG_MANAGED) != "true":
        raise OwnershipError(f"SSM parameter {name} isn't tagged as aegc's; not changing it")


def _record(raw: dict[str, Any]) -> ImageRecord:
    known = {f for f in ImageRecord.__dataclass_fields__}
    return ImageRecord(**{k: v for k, v in raw.items() if k in known})


def read_images(ssm: Any, env: str) -> dict[str, ImageRecord]:
    """The env's images, by Python minor.

    Merges the records map with the single record, which an aegc 0.1/0.2 ``image build`` may
    have written since (the newer one wins). Raises SSM's ParameterNotFound if there are neither.
    """
    records: dict[str, ImageRecord] = {}
    missing: Exception | None = None
    try:
        raw = ssm.get_parameter(Name=naming.image_records_parameter(env))["Parameter"]["Value"]
        records = {m: _record(r) for m, r in json.loads(raw).get("records", {}).items()}
    except Exception as e:
        if not is_not_found(e):
            raise
        missing = e
    try:
        raw = ssm.get_parameter(Name=naming.image_parameter(env))["Parameter"]["Value"]
    except Exception as e:
        if not is_not_found(e):
            raise
        if missing is not None:
            raise
        return records
    value = json.loads(raw)
    if "records" in value:  # written by an 0.3 pre-release
        for m, r in value["records"].items():
            records.setdefault(m, _record(r))
        return records
    legacy = _record(value)
    have = records.get(legacy.python_minor)
    if have is None or legacy.built_at > have.built_at:
        records[legacy.python_minor] = legacy
    return records


def read_image(ssm: Any, env: str, python_minor: str | None = None) -> ImageRecord:
    """One image of ``env``: for ``python_minor``, or the only/newest one."""
    records = read_images(ssm, env)
    if python_minor:
        if python_minor not in records:
            raise KeyError(f"no {env!r} image for Python {python_minor}; have {sorted(records)}")
        return records[python_minor]
    return max(records.values(), key=lambda r: r.built_at)


def build_context(dest: Path, *, wheel_dir: Path, kind: str = "conda") -> Path:
    """Copy the Dockerfile for the base's kind, the env guard and the aegc wheel(s) into a fresh
    build context (always named ``Dockerfile`` there)."""
    templates = resources.files("aegc") / "templates" / "worker"
    source = "Dockerfile.pip" if kind == "pip" else "Dockerfile"
    (dest / "Dockerfile").write_bytes((templates / source).read_bytes())
    (dest / "aegc_env_guard.py").write_bytes((templates / "aegc_env_guard.py").read_bytes())
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


def docker_build_args(
    spec: ImageSpec, *, base: str, local_tag: str, context: Path, kind: str = "conda"
) -> list[str]:
    extra = (
        ["--build-arg", f"PYTHON={spec.python or 'python3'}",
         "--build-arg", f"AEGC_UID={WORKER_UID}"]
        if kind == "pip" else []
    )  # fmt: skip
    return [
        "docker", "buildx", "build",
        "--platform", "linux/arm64",
        "--load",
        "--build-arg", f"BASE={base}",
        "--build-arg", f"GCE_VERSION={spec.gce_version}",
        "--build-arg", f"AEGC_ALLOW_CHANGE={','.join(spec.allow_change)}",
        *extra,
        "--label", f"org.opencontainers.image.base.name={base}",
        "--label", f"science.aarch.aegc.version={__version__}",
        "--label", f"science.aarch.aegc.gce={spec.gce_version}",
        "-t", local_tag,
        str(context),
    ]  # fmt: skip


def detect_kind(base: str, run: Runner = subprocess.run) -> str:
    """``conda`` for a mambaorg/micromamba-style image (what ``templates/worker/Dockerfile``
    assumes: micromamba, ``/opt/conda``, ``_entrypoint.sh`` and ``mambauser``), else ``pip``."""
    probe = (
        "test -x /opt/conda/bin/python && command -v micromamba >/dev/null"
        " && test -x /usr/local/bin/_entrypoint.sh && id mambauser >/dev/null 2>&1"
        " && echo conda || echo pip"
    )
    try:
        out = run(
            ["docker", "run", "--rm", "--platform", "linux/arm64", "--entrypoint", "sh", base,
             "-c", probe],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
    except subprocess.CalledProcessError as e:
        raise ValueError(
            f"couldn't run a shell in {base}: a base image needs sh (distroless images don't "
            f"have one): {(e.stderr or '').strip()[-300:]}"
        ) from None
    return "conda" if out.stdout.strip().endswith("conda") else "pip"


def _private_registry(ref: str, account: str, region: str) -> bool:
    return ref.startswith(f"{account}.dkr.ecr.{region}.amazonaws.com/")


def pull_and_identify(ref: str, run: Runner = subprocess.run) -> tuple[str, str]:
    """Pull ``ref`` for linux/arm64 (a local-only image is used as is) and return
    ``(architecture, image id)``: the id, not the tag, says which image it is."""
    pulled = run(
        ["docker", "pull", "--platform", "linux/arm64", ref],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    out = run(
        ["docker", "image", "inspect", "--format", "{{.Architecture}} {{.Id}}", ref],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    if out.returncode != 0:
        raise ValueError(f"can't pull {ref}: {(pulled.stderr or out.stderr or '').strip()[-300:]}")
    arch, _, image_id = out.stdout.strip().partition(" ")
    return arch, image_id


def short_id(image_id: str) -> str:
    return image_id.removeprefix("sha256:")[:12]


def python_minor_of(image: str, run: Runner = subprocess.run, python: str = CONDA_PYTHON) -> str:
    out = run(
        [
            "docker", "run", "--rm", "--entrypoint", python, image,
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
    local_tag = f"aegc-build/{spec.env}:{spec.base_id}"
    cached = through_cache and not spec.base
    with tempfile.TemporaryDirectory(prefix="aegc-image-") as tmp:
        wheels = build_wheel(project_dir, Path(tmp) / "wheels", run=run)
        whash = wheel_hash(wheels)
        ctx_dir = Path(tmp) / "ctx"
        ctx_dir.mkdir()
        if cached or _private_registry(base, account, region):
            docker_login(ecr, run=run)  # the ECR pull-through cache, or your own ECR image
        kind, base_id = "conda", None
        if spec.base:
            arch, image_id = pull_and_identify(base, run=run)
            if arch and arch != "arm64":
                raise ValueError(f"{base} is {arch}, not arm64: workers are Graviton")
            base_id = short_id(image_id)
            kind = detect_kind(base, run=run)
        ctx = build_context(ctx_dir, wheel_dir=wheels, kind=kind)
        log(f"building {spec.env} from {base} ({kind} base)")
        run(
            docker_build_args(spec, base=base, local_tag=local_tag, context=ctx, kind=kind),
            check=True,
            text=True,
        )
    python = CONDA_PYTHON if kind == "conda" else (spec.python or "python3")
    minor = python_minor_of(local_tag, run=run, python=python)
    log(f"built {local_tag}: Python {minor}")
    if not push_image:
        return local_tag
    report = Report()
    repo_uri = ensure_repository(ecr, spec.env, report)
    docker_login(ecr, run=run)
    tag = image_tag(minor, spec, whash, base_id)
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
        base=f"{base} ({base_id})" if base_id else base,
        gce_version=spec.gce_version,
        aegc_version=__version__,
        built_at=int(time.time()),
        base_kind=kind,
        python=python,
    )
    record_image(ssm, rec)
    log(f"pushed {pinned}; recorded it for {spec.env} (Python {minor})")
    return rec


#: ``aegc image register`` needs workers that know size classes, grants and outputs.
MIN_REGISTER_VERSION = (0, 3)


def probe_worker(image: str, run: Runner = subprocess.run) -> dict[str, Any]:
    """``aegc-worker version`` in ``image``, run the way user data runs it: through the image's
    own entrypoint and user, so what passes here is what instances run."""
    out = run(
        ["docker", "run", "--rm", image, "aegc-worker", "version"],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    lines = (out.stdout or "").strip().splitlines()
    try:
        info = json.loads(lines[-1]) if out.returncode == 0 and lines else None
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict):
        raise ValueError(
            f"`docker run {image} aegc-worker version` failed: the image needs aegc 0.3 or later "
            "with aegc-worker on PATH, and an entrypoint that runs the command it's given "
            f"({(out.stderr or out.stdout or '').strip()[-300:]})"
        )
    return info


def register(
    env: str,
    image: str,
    *,
    account: str,
    region: str,
    ecr: Any,
    ssm: Any,
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
) -> ImageRecord:
    """Check an image you built, copy it into ``aegc/<env>`` and record it.

    The image must be ``linux/arm64`` and pinned, run ``aegc-worker`` (aegc >= 0.3) through its
    own entrypoint, as root or uid 57439 (which owns the host's ``/scratch``), and pass
    ``aegc-worker selftest``.
    """
    naming.validate_name("pool", f"{env}-py314")  # the env must fit in a pool name
    if not _pinned(image):
        raise ValueError(f"pin the image ({image!r}): repo@sha256:... or an immutable tag")
    if _private_registry(image, account, region):
        docker_login(ecr, run=run)
    arch, image_id = pull_and_identify(image, run=run)
    if arch != "arm64":
        raise ValueError(f"{image} is {arch}, not arm64: workers are Graviton")
    info = probe_worker(image, run=run)
    version = str(info.get("aegc", ""))
    if _version_tuple(version) < MIN_REGISTER_VERSION:
        raise ValueError(f"{image} has aegc {version}; registered images need aegc 0.3 or later")
    uid = int(info.get("uid", -1))
    if uid not in (0, WORKER_UID):
        raise ValueError(
            f"{image} runs as uid {uid}; workers need root or uid {WORKER_UID}, which owns the "
            f"host's /scratch (USER {WORKER_UID} in your Dockerfile)"
        )
    run(["docker", "run", "--rm", image, "aegc-worker", "selftest"], check=True, text=True)
    minor = str(info["python"])
    repo_uri = ensure_repository(ecr, env, Report())
    docker_login(ecr, run=run)
    tag = f"py{minor}-aegc{version}-r{short_id(image_id)}"
    pinned = existing_image(ecr, env, tag)
    if pinned:
        log(f"{repo_uri}:{tag} already exists; reusing it")
    else:
        pinned = push(image, f"{repo_uri}:{tag}", run=run)
    rec = ImageRecord(
        env=env,
        python_minor=minor,
        image=pinned,
        tag=tag,
        base=f"{image} ({short_id(image_id)})",
        gce_version=str(info.get("gce", "")),
        aegc_version=version,
        built_at=int(time.time()),
        base_kind="registered",
        python=str(info.get("executable", "python3")),
    )
    record_image(ssm, rec)
    log(f"registered {image} as {pinned} (Python {minor})")
    return rec


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for p in text.split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def commands_used() -> Sequence[str]:
    """External tools ``aegc image build`` needs on PATH."""
    return ("uv", "docker")
