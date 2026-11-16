import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from aegc import endpoint as ep

SPEC = ep.EndpointSpec(
    name="aegc", deployment="dev", region="us-west-2", default_env="geospatial", aws_profile="aws"
)


def test_template_renders_and_parses() -> None:
    out = ep.render_template(SPEC)
    assert "@@" not in out
    assert "type: AWSEngine" in out and "amqp_port: 5671" in out
    assert "deployment: dev" in out and "region: us-west-2" in out


def test_registration_hook_makes_engine_resolvable() -> None:
    import globus_compute_endpoint.engines as engines

    import aegc._register  # noqa: F401  (the .pth does this at start-up)
    from aegc.engine import AWSEngine

    assert engines.AWSEngine is AWSEngine
    assert "AWSEngine" in dir(engines)
    with pytest.raises(AttributeError):
        engines.NoSuchEngine  # noqa: B018


def test_dispatcher_builds_awsengine() -> None:
    from globus_compute_endpoint.endpoint.config.dispatch import EngineDispatcher

    import aegc._register  # noqa: F401

    eng = EngineDispatcher.build_instance(
        {"type": "AWSEngine", "deployment": "dev", "env": "geospatial"}
    )
    assert type(eng).__name__ == "AWSEngine" and eng.deployment == "dev"


@pytest.mark.parametrize(
    ("opts", "env"),
    [
        ({}, "geospatial"),
        ({"env": "climate"}, "climate"),
        ({"env": "climate", "size": "mem", "walltime_s": 600}, "climate"),
        ({"cpus": 4, "memory_gib": 24.5, "pool": "geospatial-py314-large"}, "geospatial"),
    ],
)
def test_rendered_template_with_globus_renderer(
    tmp_path: Path, opts: dict[str, Any], env: str
) -> None:
    """Render the template exactly as the endpoint does for a Python 3.14 submitter."""
    import pwd

    from globus_compute_endpoint.endpoint.config import CoreEndpointConfig
    from globus_compute_endpoint.endpoint.config.utils import render_config_user_template
    from globus_compute_endpoint.endpoint.identity_mapper import MappedPosixIdentity

    template = tmp_path / "user_config_template.yaml.j2"
    template.write_text(ep.render_template(SPEC))
    rendered = render_config_user_template(
        parent_config=CoreEndpointConfig(),
        user_config_template=template.read_text(),
        user_config_template_path=template,
        mapped_identity=MappedPosixIdentity(pwd.getpwuid(os.getuid()), [], None),
        user_config_schema=json.loads(ep._template("user_config_schema.json")),
        user_opts=opts,
        user_runtime={"python": {"version_tuple": [3, 14, 8]}},
    )
    data = yaml.safe_load(rendered)
    assert data["amqp_port"] == 5671 and data["idle_heartbeats_soft"] == 0
    engine = data["engine"]
    assert engine["type"] == "AWSEngine" and engine["deployment"] == "dev"
    assert engine["env"] == env and engine["python_minor"] == "3.14"
    for key in ("pool", "size", "cpus", "memory_gib", "walltime_s"):
        assert engine.get(key) == opts.get(key)
    from globus_compute_endpoint.endpoint.config.dispatch import EngineDispatcher

    built = EngineDispatcher.build_instance(engine)
    assert built.size == opts.get("size", "") and built.cpus == opts.get("cpus", 0)


@pytest.mark.parametrize("bad", [{"size": "huge"}, {"cpus": 0}, {"walltime_s": 1.5}, {"vcpus": 2}])
def test_bad_endpoint_options_are_refused(tmp_path: Path, bad: dict[str, Any]) -> None:
    import jsonschema

    schema = json.loads(ep._template("user_config_schema.json"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_commands_are_composed(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["uv", "build"]:
            out = Path(cmd[cmd.index("--out-dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "aws_elastic_globus_compute-0.1.0-py3-none-any.whl").write_bytes(b"w")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"endpoint_id": "abc"}', stderr="")

    ep.build(tmp_path, run=run)
    ep.configure(SPEC, run=run)
    ep.start(SPEC, run=run)
    ep.stop(SPEC, run=run)
    assert ep.endpoint_id(SPEC, run=run) == "abc"
    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("docker build") and "GCE_VERSION=4.21.0" in c for c in flat)
    conf = next(c for c in calls if c[:3] == ["docker", "run", "--rm"] and "sh" in c)
    assert "globus-compute-endpoint configure aegc" in conf[-1] and "amqp_port: 5671" in conf[-1]
    assert any("GC_ENDPOINT=aegc" in c for c in flat)
    stop_cmds = [c for c in flat if c.startswith(("docker exec", "docker stop"))]
    assert stop_cmds[0] == "docker exec aegc-endpoint-aegc globus-compute-endpoint stop aegc"
    assert stop_cmds[1].startswith("docker stop")


def test_configure_writes_allowed_functions(tmp_path: Path) -> None:
    # #57: the list goes in the manager's config.yaml, registered with Globus at start.
    import shlex as _shlex

    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    fid = "11111111-1111-1111-1111-111111111111"
    ep.configure(ep.EndpointSpec(**{**SPEC.__dict__, "allowed_functions": (fid,)}), run=run)
    script = calls[-1][-1]
    assert "allowed_functions" in script and fid in script
    ep.configure(SPEC, run=run)  # None: config.yaml's list is left alone
    assert "allowed_functions" not in calls[-1][-1]
    # The edit itself, on a real file.

    cfg = tmp_path / "config.yaml"
    cfg.write_text("display_name: null\namqp_port: 5671\n")
    step = ep._allowed_functions_edit(str(cfg), (fid,)).replace(
        "python3", _shlex.quote(sys.executable), 1
    )
    subprocess.run(["sh", "-c", step], check=True)
    assert yaml.safe_load(cfg.read_text()) == {
        "display_name": None,
        "amqp_port": 5671,
        "allowed_functions": [fid],
    }
    step = ep._allowed_functions_edit(str(cfg), ()).replace(
        "python3", _shlex.quote(sys.executable), 1
    )
    subprocess.run(["sh", "-c", step], check=True)
    assert "allowed_functions" not in yaml.safe_load(cfg.read_text())


def test_cli_refuses_bad_function_ids() -> None:
    from aegc import cli

    with pytest.raises(SystemExit, match="UUIDs"):
        cli.main(["--region", "us-west-2", "endpoint", "configure", "--env", "geo",
                  "--allow-function", "not-a-uuid"])  # fmt: skip


@pytest.mark.parametrize(
    ("runtime", "opts", "minor"),
    [
        ({"python": {"version_tuple": [3, 13, 2]}}, {"python": "3.12"}, "3.13"),  # SDK wins
        ({}, {"python": "3.12"}, "3.12"),  # Globus Flows: no runtime, the option says it
        ({}, {}, ""),
    ],
)
def test_template_python_without_a_runtime(
    tmp_path: Path, runtime: dict[str, Any], opts: dict[str, Any], minor: str
) -> None:
    import pwd

    from globus_compute_endpoint.endpoint.config import CoreEndpointConfig
    from globus_compute_endpoint.endpoint.config.utils import render_config_user_template
    from globus_compute_endpoint.endpoint.identity_mapper import MappedPosixIdentity

    template = tmp_path / "user_config_template.yaml.j2"
    template.write_text(ep.render_template(SPEC))
    rendered = render_config_user_template(
        parent_config=CoreEndpointConfig(),
        user_config_template=template.read_text(),
        user_config_template_path=template,
        mapped_identity=MappedPosixIdentity(pwd.getpwuid(os.getuid()), [], None),
        user_config_schema=json.loads(ep._template("user_config_schema.json")),
        user_opts=opts,
        user_runtime=runtime,
    )
    assert yaml.safe_load(rendered)["engine"]["python_minor"] == minor
