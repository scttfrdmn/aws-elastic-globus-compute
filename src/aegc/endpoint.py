"""``aegc endpoint ...``: run a Globus Compute endpoint with AWSEngine in a container on this host.

Endpoints need Linux, so the endpoint runs in Docker (also on macOS). It holds no inbound
connections: it talks to Globus over AMQPS and to AWS APIs. Its Globus login and endpoint
configuration live in a named Docker volume; AWS credentials come from ``~/.aws`` (read-only).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

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
        ]
    )
    run(["docker", "run", "--rm", *_mounts(spec), IMAGE, "sh", "-c", script], check=True, text=True)


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
