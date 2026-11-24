"""``aegc endpoint ...``: run a Globus Compute endpoint with AWSEngine in a container on this host.

Endpoints need Linux, so the endpoint runs in Docker (also on macOS). It holds no inbound
connections: it talks to Globus over AMQPS and to AWS APIs. Its Globus login and endpoint
configuration live in a named Docker volume; AWS credentials come from ``~/.aws`` (read-only).

A multi-user endpoint (#62, :mod:`aegc.multiuser`) runs the same image as root with a few
capabilities, from its own volumes: ``aegc-endpoint-mu`` (root's ``~/.globus_compute``: the
login, the core endpoint and its identity mapping) and ``aegc-endpoint-mu-home`` (``/home``: one
home per mapped local user, where that user's endpoint processes keep their state).
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
from aegc.multiuser import MultiUser

IMAGE = "aegc-endpoint"
VOLUME = "aegc-endpoint-home"
HOME = "/home/gc"
GC_DIR = f"{HOME}/.globus_compute"
#: Python in the endpoint image; `aegc endpoint test` submits with it, so match a worker image.
DEFAULT_PYTHON = "3.14"

# --- multi-user endpoints (#62) ---
MU_VOLUME = "aegc-endpoint-mu"
MU_HOME_VOLUME = "aegc-endpoint-mu-home"
MU_GC_DIR = "/root/.globus_compute"
#: ~/.aws, read-only; the entrypoint copies it to each mapped user's home.
MU_AWS_DIR = "/etc/aegc/aws"
#: The single-user volume, read-only, in multi-user configure (to copy the login).
SU_MOUNT = "/aegc/single"
#: The configuration aegc keeps beside the core endpoint's (the --map rules, for deploy and status).
MU_FILE = "aegc_multi_user.json"
#: ``<name> <uid>`` per mapped local user; the entrypoint creates them.
USERS_FILE = "aegc_local_users"
#: The root core endpoint needs only these: change to mapped users (SETUID, SETGID), signal their
#: processes (KILL), and make their homes and ~/.aws copies (CHOWN, DAC_OVERRIDE, FOWNER).
#: Compute drops all of them, and sets no-new-privs, in each user endpoint process.
MU_CAPS = ("SETUID", "SETGID", "KILL", "CHOWN", "DAC_OVERRIDE", "FOWNER")
MU_DOCKER_FLAGS = (
    "--user", "0", "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
    *[a for c in MU_CAPS for a in ("--cap-add", c)],
)  # fmt: skip

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
    #: A multi-user endpoint (#62); None: single-user, as uid 1000.
    multi_user: MultiUser | None = None

    @property
    def container(self) -> str:
        return f"aegc-endpoint-{self.name}"

    @property
    def gc_dir(self) -> str:
        """The core endpoint's ``~/.globus_compute`` in the container."""
        return MU_GC_DIR if self.multi_user else GC_DIR

    def users(self) -> dict[str, dict[str, str]]:
        """Multi-user: local user -> deployment, region, AWS profile."""
        if not self.multi_user:
            return {}
        return self.multi_user.users(
            deployment=self.deployment, region=self.region, aws_profile=self.aws_profile
        )


def _template(name: str) -> str:
    return (resources.files("aegc") / "templates" / "endpoint" / name).read_text()


def render_template(spec: EndpointSpec) -> str:
    from aegc import multiuser

    out = _template("user_config_template.yaml.j2")
    if spec.multi_user:
        subs = {
            "@@MULTI_USER@@\n": multiuser.template_block(spec.users()),
            "@@DEPLOYMENT@@": '"{{ aegc_user.deployment }}"',
            "@@REGION@@": '"{{ aegc_user.region }}"',
            "@@AWS_PROFILE@@\n": "{%- if aegc_user.aws_profile %}\n"
            '  aws_profile: "{{ aegc_user.aws_profile }}"\n{%- endif %}\n',
        }
    else:  # single-user: as before
        subs = {
            "@@MULTI_USER@@\n": "",
            "@@DEPLOYMENT@@": spec.deployment,
            "@@REGION@@": spec.region,
            "@@AWS_PROFILE@@\n": "",
        }
    subs["@@DEFAULT_ENV@@"] = spec.default_env
    for key, value in subs.items():
        out = out.replace(key, value)
    if "@@" in out:
        raise ValueError("unrendered placeholder in the endpoint template")
    return out


def user_environment(spec: EndpointSpec) -> str:
    lines = [f"AWS_REGION: {spec.region}"]
    # Multi-user: each user's profile is in its template row, not in every user's environment.
    if spec.aws_profile and not spec.multi_user:
        lines.append(f"AWS_PROFILE: {spec.aws_profile}")
    return "\n".join(lines) + "\n"


def _mounts(spec: EndpointSpec) -> list[str]:
    if spec.multi_user:
        return _mu_mounts(spec)
    args = ["-v", f"{VOLUME}:{GC_DIR}"]
    aws_dir = Path.home() / ".aws"
    if aws_dir.is_dir():
        args += ["-v", f"{aws_dir}:{HOME}/.aws:ro"]
    if spec.aws_profile:
        args += ["-e", f"AWS_PROFILE={spec.aws_profile}"]
    args += ["-e", f"AWS_REGION={spec.region}", "-e", f"AWS_DEFAULT_REGION={spec.region}"]
    return args


def _mu_mounts(spec: EndpointSpec, *, single: bool = False) -> list[str]:
    """Root, minimal capabilities, the multi-user volumes and ~/.aws (read-only, copied per user
    by the entrypoint). ``single``: also the single-user volume, read-only (configure)."""
    args = [*MU_DOCKER_FLAGS, "-v", f"{MU_VOLUME}:{MU_GC_DIR}", "-v", f"{MU_HOME_VOLUME}:/home"]
    if single:
        args += ["-v", f"{VOLUME}:{SU_MOUNT}:ro"]
    aws_dir = Path.home() / ".aws"
    if aws_dir.is_dir():
        args += ["-v", f"{aws_dir}:{MU_AWS_DIR}:ro"]
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
    mounts = _mu_mounts(spec, single=True) if spec.multi_user else _mounts(spec)
    run(["docker", "run", "--rm", *mounts, IMAGE, "sh", "-c", script], check=True, text=True)


#: Prints the Globus identity ids in a token storage (the owner's login), one per line.
_OWNER_IDS = (
    "import json, sqlite3, sys\n"
    "for p in sys.argv[1:]:\n"
    "    try:\n"
    "        rows = sqlite3.connect(p).execute('select token_data_json from token_storage')\n"
    "    except Exception:\n"
    "        continue\n"
    "    for (t,) in rows:\n"
    "        i = json.loads(t).get('identity_id')\n"
    "        if i: print(i)\n"
)


def owner_identities(spec: EndpointSpec, run: Runner = subprocess.run) -> tuple[str, ...]:
    """The Globus identity ids of the login a multi-user endpoint would run as (its owner): from
    its own volume, else the single-user volume (which configure copies). Empty: no login."""
    out = run(
        ["docker", "run", "--rm", *_mu_mounts(spec, single=True), IMAGE, "python", "-c",
         _OWNER_IDS, f"{MU_GC_DIR}/storage.db", f"{SU_MOUNT}/storage.db"],
        check=False, text=True, capture_output=True,
    )  # fmt: skip
    if out.returncode != 0:
        return ()
    return tuple(dict.fromkeys(ln.strip() for ln in out.stdout.splitlines() if ln.strip()))


def detect_multi_user(spec: EndpointSpec, run: Runner = subprocess.run) -> MultiUser | None:
    """The stored multi-user configuration of endpoint ``spec.name``, or None (single-user)."""
    try:
        vol = run(["docker", "volume", "inspect", MU_VOLUME], check=False, text=True,
                  capture_output=True)  # fmt: skip
    except (FileNotFoundError, OSError):  # no docker here
        return None
    if vol.returncode != 0:
        return None
    out = run(
        ["docker", "run", "--rm", "--user", "0", "--cap-drop", "ALL", "-v",
         f"{MU_VOLUME}:{MU_GC_DIR}:ro", IMAGE, "cat", f"{MU_GC_DIR}/{spec.name}/{MU_FILE}"],
        check=False, text=True, capture_output=True,
    )  # fmt: skip
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return MultiUser.from_json(out.stdout)


def configure_script(spec: EndpointSpec) -> str:
    """The shell script, run in the endpoint image, that creates and configures the endpoint
    (on this host, or on the cloud endpoint host at boot)."""
    if spec.multi_user:
        return _configure_script_mu(spec, spec.multi_user)
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


def _configure_script_mu(spec: EndpointSpec, mu: MultiUser) -> str:
    """Multi-user: run as root in the image with the multi-user volumes (and, locally, the
    single-user volume read-only at SU_MOUNT, to copy the login)."""
    ep = f"{MU_GC_DIR}/{spec.name}"
    idmap = f"{ep}/identity_mapping_config.json"
    users = mu.local_users()
    files = {
        "user_config_template.yaml.j2": render_template(spec),
        "user_config_schema.json": _template("user_config_schema.json"),
        "user_environment.yaml": user_environment(spec),
        USERS_FILE: "".join(f"{n} {uid}\n" for n, uid in sorted(users.items())),
        MU_FILE: mu.to_json(spec.users()) + "\n",
    }
    if mu.identity_mapping_file:  # the cloud host: no identities in the launch template
        files[MU_FILE] = json.dumps({"version": 1, "users": spec.users(),
                                     "identity_mapping": "in the login secret"}) + "\n"  # fmt: skip
    else:
        files["identity_mapping_config.json"] = json.dumps(mu.identity_mapping(), indent=2) + "\n"
    su_ep = f"{SU_MOUNT}/{spec.name}"
    q = shlex.quote
    lines = [
        "set -e",
        "umask 077",
        "test \"$(id -u)\" = 0 || { echo 'aegc: a multi-user endpoint configures as root'; "
        "exit 1; }",
        # One name, one mode: a single-user endpoint of this name would be a second endpoint id.
        f"if [ -d {su_ep} ]; then echo 'aegc: {spec.name} is a single-user endpoint (in volume "
        f"{VOLUME}); use another --name for the multi-user one'; exit 1; fi",
        f"install -d -m 700 {MU_GC_DIR}",
        # The owner's login: from the single-user volume unless this volume has one already.
        f"if [ ! -s {MU_GC_DIR}/storage.db ] && [ -s {SU_MOUNT}/storage.db ]; then "
        f"cp {SU_MOUNT}/storage.db {MU_GC_DIR}/storage.db; fi",
        f"chmod 600 {MU_GC_DIR}/storage.db 2>/dev/null || true",
        f"test -d {ep} || globus-compute-endpoint configure --multi-user true "
        f"--contact-email {q(mu.contact_email)} {spec.name} >/dev/null",
        *[f"cat > {ep}/{name} <<'AEGC_EOF'\n{body}AEGC_EOF" for name, body in files.items()],
        *([f"install -m 600 {q(mu.identity_mapping_file)} {idmap}"]
          if mu.identity_mapping_file else []),
        f"chmod 600 {idmap} {ep}/{MU_FILE}",
        f"chmod 644 {ep}/user_config_template.yaml.j2 {ep}/user_config_schema.json",
        f"rm -f {ep}/example_identity_mapping_config.json",
        _core_config_edit(f"{ep}/config.yaml", email=mu.contact_email, idmap=idmap),
        *([_allowed_functions_edit(f"{ep}/config.yaml", spec.allowed_functions)]
          if spec.allowed_functions is not None else []),
    ]  # fmt: skip
    return "\n".join(lines)


def _core_config_edit(path: str, *, email: str, idmap: str) -> str:
    """A shell step that sets the core endpoint's multi-user keys in config.yaml.

    ``public: false``: Globus says visibility isn't a security feature (identity mapping decides
    who may use the endpoint), and aegc endpoints stay out of listings.
    """
    code = (
        "import sys, yaml\n"
        "p, email, idmap = sys.argv[1:4]\n"
        "c = yaml.safe_load(open(p)) or {}\n"
        "c.update({'email': email, 'identity_mapping_config_path': idmap, 'amqp_port': 5671})\n"
        "c['public'] = False\n"
        "c.pop('multi_user', None)\n"
        "yaml.safe_dump(c, open(p, 'w'), sort_keys=False)\n"
    )
    return " ".join(["python3", "-c", shlex.quote(code), *map(shlex.quote, (path, email, idmap))])


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
    mu = ["-e", "GC_MULTI_USER=1"] if spec.multi_user else []
    run(
        ["docker", "run", "-d", "--name", spec.container, "--restart", "unless-stopped",
         *_mounts(spec), "-e", f"GC_ENDPOINT={spec.name}", *mu, IMAGE],
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
         "cat", f"{spec.gc_dir}/{spec.name}/endpoint.json"],
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


def local_allowed_functions(spec: EndpointSpec, run: Runner = subprocess.run) -> tuple[str, ...]:
    """The local endpoint's ``allowed_functions`` (empty: any function)."""
    code = (
        "import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}; "
        "print(' '.join(c.get('allowed_functions') or []))"
    )
    out = run(
        ["docker", "run", "--rm", *_mounts(spec), IMAGE, "python", "-c", code,
         f"{spec.gc_dir}/{spec.name}/config.yaml"],
        check=False, text=True, capture_output=True,
    )  # fmt: skip
    return tuple(out.stdout.split()) if out.returncode == 0 else ()


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
        ["docker", "run", "--rm", *_mounts(spec), IMAGE, "python", "-c", code, spec.gc_dir,
         spec.name],
        check=True, text=True, capture_output=True,
    )  # fmt: skip
    d = json.loads(out.stdout.strip().splitlines()[-1])
    return EndpointLogin(
        name=spec.name, endpoint_id=str(d["id"]), storage_db=base64.b64decode(d["db"]),
        identity_mapping=spec.multi_user.identity_mapping() if spec.multi_user else None,
    )  # fmt: skip


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


#: Lists the container's processes that don't run as root: on a multi-user endpoint, the user
#: endpoint processes (and their children), by local user.
_PROCS = (
    "import os, pwd, time\n"
    "tick = os.sysconf('SC_CLK_TCK'); up = float(open('/proc/uptime').read().split()[0])\n"
    "for pid in sorted((p for p in os.listdir('/proc') if p.isdigit()), key=int):\n"
    "    try:\n"
    "        st = os.stat('/proc/' + pid)\n"
    "        cmd = open('/proc/%s/cmdline' % pid, 'rb').read().replace(b'\\0', b' ')\n"
    "        start = int(open('/proc/%s/stat' % pid).read().rsplit(')', 1)[1].split()[19])\n"
    "    except OSError:\n"
    "        continue\n"
    "    if st.st_uid == 0 or not cmd.strip():\n"
    "        continue\n"
    "    try:\n"
    "        user = pwd.getpwuid(st.st_uid).pw_name\n"
    "    except KeyError:\n"
    "        user = str(st.st_uid)\n"
    "    age = int(up - start / tick)\n"
    "    print('%s\\t%s\\t%ss\\t%s' % (user, pid, age, cmd.decode(errors='replace').strip()))\n"
)


def user_processes(spec: EndpointSpec, run: Runner = subprocess.run) -> list[tuple[str, ...]]:
    """(local user, pid, age, command) of the running container's non-root processes."""
    out = run(["docker", "exec", spec.container, "python", "-c", _PROCS], check=False, text=True,
              capture_output=True)  # fmt: skip
    if out.returncode != 0:
        return []
    return [tuple(ln.split("\t", 3)) for ln in out.stdout.splitlines() if ln.count("\t") >= 3]


def local_status(spec: EndpointSpec, run: Runner = subprocess.run) -> str:
    """The local endpoint: running or not, and (multi-user) each local user's processes."""
    if not is_running(spec, run):
        return f"local endpoint {spec.name}: not running"
    mode = "multi-user" if spec.multi_user else "single-user"
    lines = [f"local endpoint {spec.name} ({mode}): running ({spec.container})"]
    if spec.multi_user:
        users = spec.users()
        procs = user_processes(spec, run)
        for u in sorted(users):
            row = users[u]
            mine = [p for p in procs if p[0] == u]
            where = row["deployment"] + (f", profile {row['aws_profile']}" if row["aws_profile"]
                                         else "")  # fmt: skip
            lines.append(f"  {u} ({where}): {len(mine)} process(es)")
            lines += [f"    {pid} {age} {cmd[:100]}" for _, pid, age, cmd in mine]
    return "\n".join(lines)


def render_as(
    spec: EndpointSpec, user: str, options: str, run: Runner = subprocess.run
) -> subprocess.CompletedProcess[str]:
    """Render the running multi-user endpoint's template as local ``user`` would get it, with
    ``globus-compute-endpoint render-user-config`` run as that user (``options``: JSON)."""
    ep = f"{MU_GC_DIR}/{spec.name}"
    u = shlex.quote(user)
    script = (
        'set -e; d=$(mktemp -d); chmod 755 "$d"; '
        f'cp {ep}/user_config_template.yaml.j2 {ep}/user_config_schema.json "$d"/; '
        'printf \'%s\' "$1" > "$d"/opts.json; chmod 644 "$d"/*; '
        f"HOME=/home/{u} setpriv --reuid {u} --regid {u} --init-groups "
        'globus-compute-endpoint render-user-config -t "$d"/user_config_template.yaml.j2 '
        '-s "$d"/user_config_schema.json -o "$d"/opts.json; rm -rf "$d"'
    )
    return run(["docker", "exec", spec.container, "sh", "-c", script, "render", options],
               check=False, text=True)  # fmt: skip


def smoke(spec: EndpointSpec, n: int, env: str | None, run: Runner = subprocess.run) -> None:
    run(
        ["docker", "exec", spec.container, "python", "-m", "aegc.gc_smoke", spec.name, str(n),
         *(["--env", env] if env else [])],
        check=True, text=True,
    )  # fmt: skip


def default_profile() -> str:
    return os.environ.get("AWS_PROFILE", "")
