"""Multi-user endpoints (#62): --map parsing, the identity mapping, per-user template rendering,
configure/start commands, the CLI's refusals and the cloud endpoint host."""

from __future__ import annotations

import base64
import json
import pwd
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from aegc import endpoint as ep
from aegc import multiuser
from aegc.infra import Report, deployment, endpoint_host, secrets, userdata
from aegc.infra.deployment import Clients, DeploymentSpec
from aegc.multiuser import MappingError, MultiUser
from tests.conftest import ACCOUNT, REGION
from tests.unit.test_deployment import env as env  # the fixture
from tests.unit.test_endpoint_host import EID, IMAGE, LOGIN
from tests.unit.test_endpoint_host import cloud as cloud  # the fixture

OWNER = "11111111-2222-3333-4444-555555555555"
CLIENT = "99999999-8888-7777-6666-555555555555"
MAPS = [
    f"{OWNER}=gc",
    "bob@uni.edu=bob:labb:labb-profile",
    f"{CLIENT}@clients.auth.globus.org=robot:labb:labb-profile",
    ".*@lab-a.uni.edu=alice:laba",
]
MU = multiuser.build(MAPS, contact_email="admin@uni.edu")
SPEC = ep.EndpointSpec(name="shared", deployment="dev", region="us-west-2", default_env="geo",
                       aws_profile="aws", multi_user=MU)  # fmt: skip


# --- --map rules ----------------------------------------------------------------------------


def test_parse_map_kinds_and_fields() -> None:
    m = multiuser.parse_map(f"{OWNER.upper()}=gc")
    assert (m.kind, m.local_user, m.deployment, m.aws_profile) == ("id", "gc", "", "")
    m = multiuser.parse_map("bob@uni.edu=bob:labb:labb-profile:us-east-1")
    assert (m.kind, m.deployment, m.aws_profile, m.region) == (
        "username", "labb", "labb-profile", "us-east-1")  # fmt: skip
    assert multiuser.parse_map(".*@lab-a.uni.edu=alice").kind == "pattern"
    assert multiuser.parse_map(f"{CLIENT}@clients.auth.globus.org=robot").kind == "username"


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ("bob@uni.edu", "expected"),
        ("=bob", "expected"),
        ("bob@uni.edu=Bob", "local user"),
        ("bob@uni.edu=root", "system account"),
        ("bob@uni.edu=nobody", "system account"),
        ("bob@uni.edu=bob:Lab_B", "deployment name"),
        ("bob@uni.edu=bob:labb:bad profile", "AWS profile"),
        ("bob@uni.edu=bob:labb:p:mars-1", "region"),
        ("bob@uni.edu=bob:a:b:us-east-1:x", "too many"),
        (".*=anyone", "every identity"),
        ("(.*)@(.*)=anyone", "every identity"),
        ("*@uni.edu=x", "can't start"),
        ("a\\w@uni.edu=x", "invalid escape"),
        ("a*?@uni.edu=x", "lazy"),
    ],
)
def test_parse_map_refusals(rule: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        multiuser.parse_map(rule)


def test_build_refusals() -> None:
    with pytest.raises(MappingError, match="--contact-email"):
        multiuser.build(MAPS, contact_email="")
    with pytest.raises(MappingError, match="at least one --map"):
        multiuser.build([], contact_email="a@b.org")
    with pytest.raises(MappingError, match="mapped twice"):
        multiuser.build(["bob@uni.edu=bob", "BOB@uni.edu=eve"], contact_email="a@b.org")
    with pytest.raises(MappingError, match="one trust domain"):
        multiuser.build(["bob@uni.edu=bob:laba", "bob@lab.org=bob:labb"], contact_email="a@b.org")
    # The same local user from two identities, same row: fine.
    multiuser.build(["bob@uni.edu=bob:labb", "bob@lab.org=bob:labb"], contact_email="a@b.org")


def test_local_users_and_table() -> None:
    assert MU.local_users() == {"alice": 2001, "bob": 2002, "gc": 1000, "robot": 2003}
    assert SPEC.users() == {
        "alice": {"deployment": "laba", "region": "us-west-2", "aws_profile": "aws"},
        "bob": {"deployment": "labb", "region": "us-west-2", "aws_profile": "labb-profile"},
        "gc": {"deployment": "dev", "region": "us-west-2", "aws_profile": "aws"},
        "robot": {"deployment": "labb", "region": "us-west-2", "aws_profile": "labb-profile"},
    }


def test_json_round_trip_keeps_the_configured_table() -> None:
    back = MultiUser.from_json(MU.to_json(SPEC.users()))
    assert back == MU and back.stored_users == SPEC.users()
    # The stored table wins over this run's defaults (e.g. a different --profile at start).
    assert replace(SPEC, aws_profile="other", multi_user=back).users() == SPEC.users()


def test_owner_check() -> None:
    multiuser.check_owner(MU, identity_ids=(OWNER,), usernames=())
    with pytest.raises(MappingError, match="admits the endpoint's owner"):
        multiuser.check_owner(MU, identity_ids=("aaaaaaaa-0000-0000-0000-000000000000",),
                              usernames=())  # fmt: skip
    by_name = multiuser.build(["me@uni.edu=gc"], contact_email="a@b.org")
    with pytest.raises(MappingError, match="--owner-username"):
        multiuser.check_owner(by_name, identity_ids=(OWNER,), usernames=())
    multiuser.check_owner(by_name, identity_ids=(OWNER,), usernames=("ME@uni.edu",))
    lab = multiuser.build([".*@lab-a.uni.edu=alice"], contact_email="a@b.org")
    multiuser.check_owner(lab, identity_ids=(OWNER,), usernames=("pi@lab-a.uni.edu",))
    with pytest.raises(MappingError):
        multiuser.check_owner(lab, identity_ids=(), usernames=("pi@lab-a.uni.edu.evil.org",))
    multiuser.check_owner(replace(lab, allow_unmapped_owner=True), identity_ids=(), usernames=())


# --- against Globus' own identity mapping ------------------------------------------------------


def _ident(i: str, username: str) -> dict[str, str]:
    return {"id": i, "sub": i, "username": username, "email": username, "status": "used"}


def test_mapping_document_with_globus_mapper(tmp_path: Path) -> None:
    from globus_compute_endpoint.endpoint.identity_mapper import PosixIdentityMapper

    path = tmp_path / "identity_mapping_config.json"
    path.write_text(json.dumps(MU.identity_mapping()))
    im = PosixIdentityMapper(path, "ep-id")
    try:

        def first(identity_set: list[dict[str, str]]) -> str | None:
            for found in im.map_identities(identity_set):
                if found:
                    return next(iter(found[0].values()))[0]
            return None

        assert first([_ident(OWNER, "me@uni.edu")]) == "gc"
        linked = [_ident("aaaaaaaa-0000-0000-0000-000000000001", "bob@gmail.com"),
                  _ident("aaaaaaaa-0000-0000-0000-000000000002", "BOB@uni.edu")]  # fmt: skip
        assert first(linked) == "bob"  # a linked identity, any case
        assert first([_ident(CLIENT, f"{CLIENT}@clients.auth.globus.org")]) == "robot"
        assert first([_ident("cccccccc-0000-0000-0000-000000000001", "pi@lab-a.uni.edu")]) == (
            "alice"
        )
        for nope in ("pi@lab-a.uni.edu.evil.org", "bob@uni.edu.evil.org", "bobXuni.edu"):
            assert first([_ident("dddddddd-0000-0000-0000-000000000001", nope)]) is None
    finally:
        im.stop_watching()


@pytest.mark.parametrize(
    "pattern",
    [".*@lab-a.uni.edu", "(alice|bob)@uni\\.edu", "a.?b@x.org", "x+y@[a].org", "^a$@b.org"],
)
def test_compile_match_agrees_with_globus(pattern: str) -> None:
    from globus_identity_mapping.mappers.expression import ExpressionIdentityMapping

    ours = multiuser.compile_match(pattern)
    theirs = ExpressionIdentityMapping._compile_match(pattern, ignore_case=True)
    for s in ("bob@uni.edu", "alice@uni.edu", "pi@lab-a.uni.edu", "ab@x.org", "a-b@x.org",
              "x+y@[a].org", "^a$@b.org", "aXb@x.org", "alice@uniXedu"):  # fmt: skip
        assert bool(ours.match(s)) == bool(theirs.match(s)), (pattern, s)


# --- the template, rendered as Globus Compute does for each mapped user ----------------------


def _render(spec: ep.EndpointSpec, user: str, uid: int, opts: dict[str, Any],
            tmp_path: Path) -> dict[str, Any]:  # fmt: skip
    from globus_compute_endpoint.endpoint.config import CoreEndpointConfig
    from globus_compute_endpoint.endpoint.config.utils import render_config_user_template
    from globus_compute_endpoint.endpoint.identity_mapper import MappedPosixIdentity

    template = tmp_path / "user_config_template.yaml.j2"
    template.write_text(ep.render_template(spec))
    rec = pwd.struct_passwd((user, "x", uid, uid, "", f"/home/{user}", "/usr/sbin/nologin"))
    rendered = render_config_user_template(
        parent_config=CoreEndpointConfig(),
        user_config_template=template.read_text(),
        user_config_template_path=template,
        mapped_identity=MappedPosixIdentity(rec, [], OWNER),
        user_config_schema=json.loads(ep._template("user_config_schema.json")),
        user_opts=opts,
        user_runtime={"python": {"version_tuple": [3, 14, 0]}},
    )
    return dict(yaml.safe_load(rendered))


@pytest.mark.parametrize(
    ("user", "uid", "opts", "want"),
    [
        ("gc", 1000, {}, {"deployment": "dev", "aws_profile": "aws", "env": "geo"}),
        ("bob", 2002, {"env": "climate"},
         {"deployment": "labb", "aws_profile": "labb-profile", "env": "climate"}),
        ("alice", 2001, {"size": "mem"},
         {"deployment": "laba", "aws_profile": "aws", "size": "mem"}),
        ("robot", 2003, {}, {"deployment": "labb", "aws_profile": "labb-profile"}),
    ],
)  # fmt: skip
def test_template_branches_per_mapped_user(
    tmp_path: Path, user: str, uid: int, opts: dict[str, Any], want: dict[str, str]
) -> None:
    data = _render(SPEC, user, uid, opts, tmp_path)
    engine = data["engine"]
    assert data["amqp_port"] == 5671 and engine["type"] == "AWSEngine"
    assert engine["region"] == "us-west-2" and engine["python_minor"] == "3.14"
    for k, v in want.items():
        assert engine[k] == v, k


def test_template_without_a_profile_and_unmapped_users(tmp_path: Path) -> None:
    import jinja2
    import jsonschema

    spec = replace(SPEC, aws_profile="")
    assert "aws_profile" not in _render(spec, "gc", 1000, {}, tmp_path)["engine"]
    with pytest.raises(jinja2.exceptions.UndefinedError, match="no_row_in_aegc_users"):
        _render(spec, "eve", 3000, {}, tmp_path)
    # A submitter can't pick another user's deployment or profile.
    for opts in ({"aws_profile": "aws"}, {"deployment": "laba"}, {"region": "us-east-1"}):
        with pytest.raises(jsonschema.ValidationError):
            _render(spec, "bob", 2002, opts, tmp_path)


def test_single_user_template_is_unchanged() -> None:
    one = replace(SPEC, multi_user=None)
    out = ep.render_template(one)
    assert "set aegc_users" not in out and "aws_profile:" not in out and "@@" not in out
    assert "deployment: dev\n" in out and "region: us-west-2\n" in out
    assert ep.user_environment(one) == "AWS_REGION: us-west-2\nAWS_PROFILE: aws\n"
    assert ep.user_environment(SPEC) == "AWS_REGION: us-west-2\n"  # profiles are per user


@pytest.mark.skipif(shutil.which("globus-compute-endpoint") is None, reason="needs the endpoint")
def test_render_user_config_cli_as_this_user(tmp_path: Path) -> None:
    """``globus-compute-endpoint render-user-config`` renders as the calling user: map this
    user, as `aegc endpoint render --as` does in the container."""
    me = pwd.getpwuid(__import__("os").getuid()).pw_name
    if not multiuser._LOCAL_USER.match(me):
        pytest.skip(f"local user {me!r} isn't a valid mapped user name")
    mu = multiuser.build([f"{OWNER}={me}:labx:px"], contact_email="a@b.org")
    t = tmp_path / "t.yaml.j2"
    t.write_text(ep.render_template(replace(SPEC, multi_user=mu)))
    (tmp_path / "s.json").write_text(ep._template("user_config_schema.json"))
    (tmp_path / "o.json").write_text('{"env": "climate"}')
    gce = Path(sys.executable).parent / "globus-compute-endpoint"
    out = subprocess.run(
        [str(gce if gce.exists() else "globus-compute-endpoint"), "render-user-config", "-t",
         str(t), "-s", str(tmp_path / "s.json"), "-o", str(tmp_path / "o.json")],
        check=True, text=True, capture_output=True,
    ).stdout  # fmt: skip
    engine = yaml.safe_load(out.split("Rendered user config:", 1)[-1])["engine"]
    assert (engine["deployment"], engine["aws_profile"], engine["env"]) == ("labx", "px", "climate")


# --- configure and start (Docker commands) ---------------------------------------------------


def _recorder(
    outputs: dict[str, subprocess.CompletedProcess[str]] | None = None,
) -> tuple[list[list[str]], Callable[..., subprocess.CompletedProcess[str]]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        for key, out in (outputs or {}).items():
            if key in " ".join(cmd):
                return out
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return calls, run


def test_configure_script_multi_user() -> None:
    fid = "11111111-1111-1111-1111-111111111111"
    script = ep.configure_script(replace(SPEC, allowed_functions=(fid,)))
    assert "globus-compute-endpoint configure --multi-user true --contact-email admin@uni.edu " \
        "shared" in script  # fmt: skip
    root = "/root/.globus_compute/shared"
    for f in ("identity_mapping_config.json", "aegc_local_users", "aegc_multi_user.json",
              "user_config_template.yaml.j2", "user_environment.yaml"):  # fmt: skip
        assert f"cat > {root}/{f} <<'AEGC_EOF'" in script
    assert "bob 2002\n" in script and "gc 1000\n" in script
    assert f"chmod 600 {root}/identity_mapping_config.json" in script
    assert "is a single-user endpoint" in script and "/aegc/single/storage.db" in script
    assert fid in script and "identity_mapping_config_path" in script
    subprocess.run(["sh", "-n"], input=script, text=True, check=True)
    # The cloud host: the mapping comes from the secret, not the script.
    cloud = ep.configure_script(
        replace(SPEC, multi_user=replace(MU, identity_mapping_file="/tmp/m.json"))
    )
    assert "identity_mapping_config.json <<'AEGC_EOF'" not in cloud
    assert f"install -m 600 /tmp/m.json {root}/identity_mapping_config.json" in cloud
    assert "bob@uni.edu" not in cloud and CLIENT not in cloud and OWNER not in cloud


def test_core_config_edit(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "display_name: null\namqp_port: 443\npublic: true\nemail: old@x.org\n"
        "identity_mapping_config_path: /x/example_identity_mapping_config.json\n"
    )
    step = ep._core_config_edit(str(cfg), email="admin@uni.edu", idmap="/r/s/idmap.json")
    subprocess.run(["sh", "-c", step.replace("python3", shlex.quote(sys.executable), 1)],
                   check=True)  # fmt: skip
    assert yaml.safe_load(cfg.read_text()) == {
        "display_name": None, "amqp_port": 5671, "public": False, "email": "admin@uni.edu",
        "identity_mapping_config_path": "/r/s/idmap.json",
    }  # fmt: skip


def test_multi_user_commands_are_composed() -> None:
    calls, run = _recorder({"endpoint.json": subprocess.CompletedProcess(
        [], 0, stdout='{"endpoint_id": "abc"}', stderr="")})  # fmt: skip
    ep.configure(SPEC, run=run)
    ep.start(SPEC, run=run)
    assert ep.endpoint_id(SPEC, run=run) == "abc"
    conf, start, ident = (" ".join(c) for c in calls)
    for c in (conf, start):
        assert "--user 0 --security-opt no-new-privileges --cap-drop ALL" in c
        assert "--cap-add SETUID --cap-add SETGID --cap-add KILL" in c and "--privileged" not in c
        assert "aegc-endpoint-mu:/root/.globus_compute" in c and "aegc-endpoint-mu-home:/home" in c
        assert "/home/gc/.globus_compute" not in c
    assert "aegc-endpoint-home:/aegc/single:ro" in conf  # to copy the login
    assert "aegc-endpoint-home" not in start
    assert "GC_MULTI_USER=1" in start and "GC_ENDPOINT=shared" in start
    assert "cat /root/.globus_compute/shared/endpoint.json" in ident
    # Single-user commands are as before.
    calls, run = _recorder()
    ep.start(replace(SPEC, multi_user=None), run=run)
    assert "--user" not in calls[0] and "GC_MULTI_USER=1" not in calls[0]


def test_detect_and_owner_identities() -> None:
    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    gone = subprocess.CompletedProcess([], 1, stdout="", stderr="no such volume")
    _, run = _recorder({"volume inspect": gone})
    assert ep.detect_multi_user(SPEC, run=run) is None

    def no_docker(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    assert ep.detect_multi_user(SPEC, run=no_docker) is None
    stored = subprocess.CompletedProcess([], 0, stdout=MU.to_json(SPEC.users()), stderr="")
    _, run = _recorder({"volume inspect": ok, "aegc_multi_user.json": stored})
    found = ep.detect_multi_user(SPEC, run=run)
    assert found == MU and found is not None and found.stored_users == SPEC.users()
    _, run = _recorder({"storage.db": subprocess.CompletedProcess(
        [], 0, stdout=f"{OWNER}\n{OWNER}\n", stderr="")})  # fmt: skip
    assert ep.owner_identities(SPEC, run=run) == (OWNER,)


def test_owner_identities_reads_globus_token_storage(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "storage.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE token_storage (namespace VARCHAR, resource_server VARCHAR, "
              "token_data_json VARCHAR)")  # fmt: skip
    c.execute("INSERT INTO token_storage VALUES ('DEFAULT', 'auth.globus.org', ?)",
              (json.dumps({"identity_id": OWNER, "access_token": "x"}),))  # fmt: skip
    c.commit()
    out = subprocess.run([sys.executable, "-c", ep._OWNER_IDS, str(db), str(tmp_path / "none")],
                         check=True, text=True, capture_output=True)  # fmt: skip
    assert out.stdout.split() == [OWNER]


def test_local_status_lists_each_users_processes() -> None:
    procs = (
        "bob\t41\t12s\tglobus-compute-endpoint _start-user-endpoint uep.x\nbob\t42\t9s\tpython\n"
    )
    _, run = _recorder({
        "inspect": subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
        "exec": subprocess.CompletedProcess([], 0, stdout=procs, stderr=""),
    })  # fmt: skip
    out = ep.local_status(SPEC, run=run)
    assert "local endpoint shared (multi-user): running" in out
    assert "bob (labb, profile labb-profile): 2 process(es)" in out
    assert "alice (laba, profile aws): 0 process(es)" in out and "41 12s globus" in out
    _, run = _recorder({"inspect": subprocess.CompletedProcess([], 1, stdout="", stderr="")})
    assert ep.local_status(SPEC, run=run).endswith("not running")


def test_entrypoint_syntax() -> None:
    subprocess.run(["sh", "-n"], input=ep._template("entrypoint.sh"), text=True, check=True)
    script = ep._template("entrypoint.sh")
    assert 'tail_ueps "/home/*/.globus_compute/uep.*/endpoint.log" 3' in script
    assert "refusing to map to system account" in script


# --- the CLI ---------------------------------------------------------------------------------


def _cli(monkeypatch: pytest.MonkeyPatch, ids: tuple[str, ...] = (OWNER,),
         detected: MultiUser | None = None) -> list[ep.EndpointSpec]:  # fmt: skip
    configured: list[ep.EndpointSpec] = []
    monkeypatch.setattr(ep, "owner_identities", lambda s, run=None: ids)
    monkeypatch.setattr(ep, "configure", lambda s, run=None: configured.append(s))
    monkeypatch.setattr(ep, "detect_multi_user", lambda s, run=None: detected)
    return configured


BASE = ["--region", "us-west-2", "endpoint", "configure", "--name", "shared", "--env", "geo",
        "--deployment", "dev"]  # fmt: skip


def test_cli_configure_multi_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from aegc import cli

    monkeypatch.setenv("AWS_PROFILE", "aws")
    configured = _cli(monkeypatch)
    argv = [*BASE, "--multi-user", "--contact-email", "admin@uni.edu"]
    for m in MAPS:
        argv += ["--map", m]
    assert cli.main(argv) == 0
    (spec,) = configured
    assert spec.multi_user == MU and spec.multi_user.owner_identity_id == OWNER
    assert spec.users() == SPEC.users()
    out = capsys.readouterr().out
    assert "4 rule(s), 4 local user(s)" in out
    assert "bob: deployment labb (us-west-2, profile labb-profile) <- bob@uni.edu" in out


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        (["--map", "bob@uni.edu=bob"], "admits the endpoint's owner"),
        (["--map", "me@uni.edu=gc"], "--owner-username"),
        (["--map", "bob@uni.edu=root"], "system account"),
        (["--map", f"{OWNER}=gc", "--contact-email", ""], "--contact-email"),
    ],
)
def test_cli_configure_multi_user_refusals(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], why: str
) -> None:
    from aegc import cli

    configured = _cli(monkeypatch)
    argv = [*BASE, "--multi-user", "--contact-email", "admin@uni.edu", *extra]
    with pytest.raises(SystemExit, match=why):
        cli.main(argv)
    assert configured == []


def test_cli_owner_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegc import cli

    argv = [*BASE, "--multi-user", "--contact-email", "a@uni.edu", "--map", "me@uni.edu=gc"]
    configured = _cli(monkeypatch)
    assert cli.main([*argv, "--owner-username", "me@uni.edu"]) == 0
    assert cli.main([*argv[:-2], "--map", "bob@uni.edu=bob", "--allow-unmapped-owner"]) == 0
    _cli(monkeypatch, ids=())
    with pytest.raises(SystemExit, match="no Globus login found"):
        cli.main(argv)
    assert len(configured) == 2


def test_cli_mode_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegc import cli

    _cli(monkeypatch, detected=MU)
    with pytest.raises(SystemExit, match="go with configure --multi-user"):
        cli.main([*BASE, "--map", f"{OWNER}=gc"])
    with pytest.raises(SystemExit, match="goes with configure and login"):
        cli.main(["--region", "us-west-2", "endpoint", "start", "--multi-user"])
    with pytest.raises(SystemExit, match="is multi-user"):
        cli.main(BASE)  # single-user configure of a multi-user endpoint
    with pytest.raises(SystemExit, match="isn't multi-user"):
        _cli(monkeypatch, detected=None)
        cli.main(["--region", "us-west-2", "endpoint", "render", "--as", "bob"])


def test_cli_start_uses_the_detected_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegc import cli

    _cli(monkeypatch, detected=MultiUser.from_json(MU.to_json(SPEC.users())))
    started: list[ep.EndpointSpec] = []
    monkeypatch.setattr(ep, "start", lambda s, run=None: started.append(s))
    monkeypatch.setattr(ep, "wait_for_id", lambda s, timeout_s=90, run=None: EID)
    assert cli.main(["--region", "us-west-2", "endpoint", "start", "--name", "shared"]) == 0
    assert started[0].multi_user is not None and started[0].gc_dir == ep.MU_GC_DIR


# --- the cloud endpoint host -----------------------------------------------------------------


def test_login_secret_carries_the_mapping(aws: Callable[[str], Any]) -> None:
    sm = aws("secretsmanager")
    mapped = replace(LOGIN, identity_mapping=MU.identity_mapping())
    secrets.put_login(sm, "dev", mapped, Report(), dry_run=False)
    assert secrets.get_login(sm, "dev") == mapped
    assert "identity_mapping" not in json.loads(LOGIN.to_json())  # single-user: same hash


def test_for_cloud_restrictions() -> None:
    same = multiuser.build([f"{OWNER}=gc", "bob@uni.edu=bob:dev"], contact_email="a@b.org")
    c = same.for_cloud(deployment="dev", region=REGION, identity_mapping_file="/tmp/m.json")
    assert c.identity_mapping_file == "/tmp/m.json" and c.stored_users is None
    for bad in (MU, multiuser.build([f"{OWNER}=gc:dev::us-east-2"], contact_email="a@b.org")):
        with pytest.raises(MappingError, match="host's role"):
            bad.for_cloud(deployment="dev", region=REGION, identity_mapping_file="/x")


def test_boot_script_multi_user() -> None:
    mu = multiuser.build([f"{OWNER}=gc", "bob@uni.edu=bob"], contact_email="a@b.org")
    cloud_mu = mu.for_cloud(deployment="dev", region=REGION,
                            identity_mapping_file=userdata.MU_IDENTITY_MAPPING)  # fmt: skip
    spec = ep.EndpointSpec(name="aegc-cloud", deployment="dev", region=REGION, default_env="geo",
                           multi_user=cloud_mu)  # fmt: skip
    host = endpoint_host.EndpointHostSpec(
        deployment="dev", account=ACCOUNT, region=REGION, name="aegc-cloud", endpoint_id=EID,
        image=IMAGE, configure_script=ep.configure_script(spec), metric_token="t0ken",
        multi_user=True,
    )  # fmt: skip
    data = endpoint_host.launch_template_data(host, ami="ami-1", sg="sg-1", profile_arn="arn:p")
    raw = base64.b64decode(data["UserData"])
    assert len(raw) < 16 * 1024
    script = raw.decode()
    assert "GC=/var/lib/aegc-endpoint/core" in script and 'chown -R 0:0 "$GC"' in script
    assert "-v $GC:/root/.globus_compute -v /var/lib/aegc-endpoint/home:/home" in script
    assert "-e GC_MULTI_USER=1" in script and "--cap-drop ALL --cap-add SETUID" in script
    assert "identity_mapping.json:/tmp/identity_mapping.json:ro" in script
    (line,) = [ln for ln in script.splitlines() if "| base64 -d" in ln]
    configure = base64.b64decode(line.split("echo ", 1)[1].split(" | ", 1)[0]).decode()
    assert "--multi-user true" in configure
    assert "bob@uni.edu" not in configure and OWNER not in configure  # they're in the secret
    evaluate = script.split("aegc-evaluate.service <<UNIT", 1)[1].split("UNIT\n", 1)[0]
    assert "--user 0 --security-opt no-new-privileges --cap-drop ALL -v $GC:/root" in evaluate
    assert "--cap-add" not in evaluate
    if shutil.which("bash"):
        subprocess.run(["bash", "-n"], input=raw, check=True)


def test_cli_deploy_multi_user(
    cloud: Clients, env: tuple[Clients, DeploymentSpec], monkeypatch: pytest.MonkeyPatch
) -> None:
    from aegc import cli

    c = cloud
    _, dspec = env
    deployment.deploy(c, dspec)
    mu = multiuser.build([f"{OWNER}=gc", "bob@uni.edu=bob"], contact_email="a@b.org")
    stored = MultiUser.from_json(mu.to_json(mu.users(deployment="dev", region=REGION,
                                                     aws_profile="aws")))  # fmt: skip
    hosts: list[endpoint_host.EndpointHostSpec] = []
    logins: list[secrets.EndpointLogin] = []
    real_deploy, real_put = endpoint_host.deploy, secrets.put_login
    monkeypatch.setattr(cli, "_clients", lambda sess: c)
    monkeypatch.setattr(ep, "detect_multi_user", lambda s, run=None: stored)
    monkeypatch.setattr(ep, "is_running", lambda s, run=None: False)
    monkeypatch.setattr(ep, "local_allowed_functions", lambda s, run=None: ())
    monkeypatch.setattr(ep, "export_login", lambda s, run=None: replace(
        LOGIN, name=s.name, identity_mapping=s.multi_user.identity_mapping()))  # fmt: skip
    monkeypatch.setattr(endpoint_host, "deploy",
                        lambda c, h, **kw: hosts.append(h) or real_deploy(c, h, **kw))  # fmt: skip
    monkeypatch.setattr(
        secrets, "put_login",
        lambda sm, d, lg, r, **kw: logins.append(lg) or real_put(sm, d, lg, r, **kw),
    )  # fmt: skip
    argv = ["--region", REGION, "endpoint", "deploy", "--name", "aegc-cloud", "--deployment",
            "dev", "--env", "geo", "--image", IMAGE, "--dry-run"]  # fmt: skip
    assert cli.main(argv) == 0
    (host,) = hosts
    assert host.multi_user and "--multi-user true" in host.configure_script
    assert '"aws_profile": ""' in host.configure_script  # the host's role, no local profile
    assert "bob@uni.edu" not in host.configure_script
    assert logins[0].identity_mapping == mu.identity_mapping()
    # A rule with another deployment or a profile can't go to the cloud host.
    monkeypatch.setattr(ep, "detect_multi_user", lambda s, run=None: MU)
    with pytest.raises(SystemExit, match="host's role"):
        cli.main(argv)
