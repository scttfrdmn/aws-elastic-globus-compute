"""``aegc endpoint ...``: run a Globus Compute endpoint with AWSEngine in a container on this host.

Endpoints need Linux, so the endpoint runs in Docker (also on macOS). It holds no inbound
connections: it talks to Globus over AMQPS and to AWS APIs. Its Globus login and endpoint
configuration live in a named Docker volume; AWS credentials come from ``~/.aws`` (read-only).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from aegc import images

IMAGE = "aegc-endpoint"
VOLUME = "aegc-endpoint-home"
HOME = "/home/gc"
GC_DIR = f"{HOME}/.globus_compute"
#: Python in the endpoint image; `aegc endpoint test` submits with it, so match a worker image.
DEFAULT_PYTHON = "3.14"

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class EndpointSpec:
    name: str  # the Globus Compute endpoint name
    deployment: str
    region: str
    default_env: str
    aws_profile: str = ""
    #: Function ids the endpoint may run (#57); None leaves config.yaml's list as it is, and ()
    #: removes it (any function).
    allowed_functions: tuple[str, ...] | None = None

    @property
    def container(self) -> str:
        return f"aegc-endpoint-{self.name}"


def _template(name: str) -> str:
    return (resources.files("aegc") / "templates" / "endpoint" / name).read_text()


def render_template(spec: EndpointSpec) -> str:
    out = _template("user_config_template.yaml.j2")
    for key, value in {
        "@@DEPLOYMENT@@": spec.deployment,
        "@@REGION@@": spec.region,
        "@@DEFAULT_ENV@@": spec.default_env,
    }.items():
        out = out.replace(key, value)
    if "@@" in out:
        raise ValueError("unrendered placeholder in the endpoint template")
    return out


def user_environment(spec: EndpointSpec) -> str:
    lines = [f"AWS_REGION: {spec.region}"]
    if spec.aws_profile:
        lines.append(f"AWS_PROFILE: {spec.aws_profile}")
    return "\n".join(lines) + "\n"


def _mounts(spec: EndpointSpec) -> list[str]:
    args = ["-v", f"{VOLUME}:{GC_DIR}"]
    aws_dir = Path.home() / ".aws"
    if aws_dir.is_dir():
        args += ["-v", f"{aws_dir}:{HOME}/.aws:ro"]
    if spec.aws_profile:
        args += ["-e", f"AWS_PROFILE={spec.aws_profile}"]
    args += ["-e", f"AWS_REGION={spec.region}", "-e", f"AWS_DEFAULT_REGION={spec.region}"]
    return args


def build(
    project_dir: Path,
    *,
    gce_version: str = images.DEFAULT_GCE_VERSION,
    python: str = DEFAULT_PYTHON,
    run: Runner = subprocess.run,
) -> str:
    with tempfile.TemporaryDirectory(prefix="aegc-endpoint-") as tmp:
        ctx = Path(tmp)
        images.build_wheel(project_dir, ctx / "dist", run=run)
        for name in ("Dockerfile", "entrypoint.sh"):
            (ctx / name).write_text(_template(name))
        run(
            ["docker", "build", "--build-arg", f"GCE_VERSION={gce_version}",
             "--build-arg", f"PYTHON={python}", "-t", IMAGE, str(ctx)],
            check=True, text=True,
        )  # fmt: skip
    return IMAGE


def login(spec: EndpointSpec, run: Runner = subprocess.run) -> None:
    run(
        ["docker", "run", "--rm", "-it", *_mounts(spec), IMAGE, "globus-compute-endpoint", "login"],
        check=True,
        text=True,
    )


def configure(spec: EndpointSpec, run: Runner = subprocess.run) -> None:
    """Create (if needed) and configure the endpoint inside the volume."""
    script = configure_script(spec)
    run(["docker", "run", "--rm", *_mounts(spec), IMAGE, "sh", "-c", script], check=True, text=True)


def configure_script(spec: EndpointSpec) -> str:
    """The shell script, run in the endpoint image, that creates and configures the endpoint
    (on this host, or on the cloud endpoint host at boot)."""
    ep = f"{GC_DIR}/{spec.name}"
    files = {
        "user_config_template.yaml.j2": render_template(spec),
        "user_config_schema.json": _template("user_config_schema.json"),
        "user_environment.yaml": user_environment(spec),
    }
    script = "\n".join(
        [
            "set -e",
            f"test -d {ep} || globus-compute-endpoint configure {spec.name} >/dev/null",
            *[f"cat > {ep}/{name} <<'AEGC_EOF'\n{body}AEGC_EOF" for name, body in files.items()],
            f"grep -q '^amqp_port:' {ep}/config.yaml"
            f" || printf 'amqp_port: 5671\\n' >> {ep}/config.yaml",
            *([_allowed_functions_edit(f"{ep}/config.yaml", spec.allowed_functions)]
              if spec.allowed_functions is not None else []),
        ]
    )  # fmt: skip
    return script


def _allowed_functions_edit(path: str, functions: tuple[str, ...]) -> str:
    """A shell step that sets (or, with none, removes) ``allowed_functions`` in config.yaml.

    It goes in the manager's config: registered with Globus at start (so other functions get 403)
    and passed to every user endpoint process.
    """
    code = (
        "import sys, yaml\n"
        "p, fns = sys.argv[1], sys.argv[2:]\n"
        "c = yaml.safe_load(open(p)) or {}\n"
        "c.pop('allowed_functions', None)\n"
        "if fns: c['allowed_functions'] = fns\n"
        "yaml.safe_dump(c, open(p, 'w'), sort_keys=False)\n"
    )
    return " ".join(["python3", "-c", shlex.quote(code), shlex.quote(path), *functions])


def start(spec: EndpointSpec, run: Runner = subprocess.run) -> None:
    run(
        ["docker", "run", "-d", "--name", spec.container, "--restart", "unless-stopped",
         *_mounts(spec), "-e", f"GC_ENDPOINT={spec.name}", IMAGE],
        check=True, text=True, capture_output=True,
    )  # fmt: skip


def stop(spec: EndpointSpec, run: Runner = subprocess.run) -> None:
    # Stop the endpoint cleanly first: a bare `docker stop` leaves a stale daemon.pid.
    run(
        ["docker", "exec", spec.container, "globus-compute-endpoint", "stop", spec.name],
        check=False,
        text=True,
    )
    run(["docker", "stop", "-t", "60", spec.container], check=False, text=True, capture_output=True)
    run(["docker", "rm", spec.container], check=False, text=True, capture_output=True)


def endpoint_id(spec: EndpointSpec, run: Runner = subprocess.run) -> str:
    out = run(
        ["docker", "run", "--rm", *_mounts(spec), IMAGE,
         "cat", f"{GC_DIR}/{spec.name}/endpoint.json"],
        check=True, text=True, capture_output=True,
    )  # fmt: skip
    return str(json.loads(out.stdout)["endpoint_id"])


def wait_for_id(spec: EndpointSpec, timeout_s: float = 90, run: Runner = subprocess.run) -> str:
    """The endpoint id, waiting for a first start to register the endpoint."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return endpoint_id(spec, run)
        except subprocess.CalledProcessError:
            if time.monotonic() > deadline:
                raise
            time.sleep(3)


def is_running(spec: EndpointSpec, run: Runner = subprocess.run) -> bool:
    out = run(
        ["docker", "inspect", "-f", "{{.State.Running}}", spec.container],
        check=False, text=True, capture_output=True,
    )  # fmt: skip
    return out.returncode == 0 and out.stdout.strip() == "true"


def export_login(spec: EndpointSpec, run: Runner = subprocess.run) -> Any:
    """This host's Globus login and the endpoint's id, for the cloud endpoint (#22)."""
    import base64

    from aegc.infra.secrets import EndpointLogin

    code = (
        "import base64, json, sys; d = sys.argv[1]; "
        "print(json.dumps({'db': base64.b64encode(open(d + '/storage.db', 'rb').read()).decode(),"
        " 'id': json.load(open(d + '/' + sys.argv[2] + '/endpoint.json'))['endpoint_id']}))"
    )
    out = run(
        ["docker", "run", "--rm", *_mounts(spec), IMAGE, "python", "-c", code, GC_DIR, spec.name],
        check=True, text=True, capture_output=True,
    )  # fmt: skip
    d = json.loads(out.stdout.strip().splitlines()[-1])
    return EndpointLogin(
        name=spec.name, endpoint_id=str(d["id"]), storage_db=base64.b64decode(d["db"])
    )


def build_push(
    project_dir: Path,
    *,
    ecr: Any,
    gce_version: str = images.DEFAULT_GCE_VERSION,
    python: str = DEFAULT_PYTHON,
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
) -> str:
    """Build the endpoint image for linux/arm64 and push it to ECR ``aegc/endpoint``; returns
    ``<repo>@sha256:<digest>`` (the cloud endpoint runs on Graviton)."""
    from aegc import __version__, naming
    from aegc.infra import Report

    repo = naming.endpoint_repository()
    local = "aegc-endpoint:arm64"
    with tempfile.TemporaryDirectory(prefix="aegc-endpoint-") as tmp:
        ctx = Path(tmp)
        wheels = images.build_wheel(project_dir, ctx / "dist", run=run)
        whash = images.wheel_hash(wheels)
        for name in ("Dockerfile", "entrypoint.sh"):
            (ctx / name).write_text(_template(name))
        run(
            ["docker", "buildx", "build", "--platform", "linux/arm64", "--load",
             "--build-arg", f"GCE_VERSION={gce_version}", "--build-arg", f"PYTHON={python}",
             "-t", local, str(ctx)],
            check=True, text=True,
        )  # fmt: skip
    uri = images.ensure_repository(ecr, "", Report(), name=repo)
    tag = f"py{python}-gce{gce_version}-aegc{__version__}-w{whash}"
    pinned = images.existing_image(ecr, "", tag, name=repo)
    if pinned:
        log(f"{uri}:{tag} already exists; reusing it")
        return pinned
    images.docker_login(ecr, run=run)
    return images.push(local, f"{uri}:{tag}", run=run)


def logs(spec: EndpointSpec, run: Runner = subprocess.run) -> None:
    run(["docker", "logs", "-f", spec.container], check=False, text=True)


def smoke(spec: EndpointSpec, n: int, env: str | None, run: Runner = subprocess.run) -> None:
    run(
        ["docker", "exec", spec.container, "python", "-m", "aegc.gc_smoke", spec.name, str(n),
         *(["--env", env] if env else [])],
        check=True, text=True,
    )  # fmt: skip


def default_profile() -> str:
    return os.environ.get("AWS_PROFILE", "")
