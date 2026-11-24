"""EC2 user data for worker instances on the stock ECS-optimized Amazon Linux 2023 arm64 AMI.

The steps and their order come from spike #5: disable the ECS agent (and its volume plugin), start
Docker explicitly (it's disabled on this AMI), read identity from IMDSv2, log in to ECR, pull the
worker image by digest, and run it under systemd. A watchdog terminates the instance (decrementing
the group's desired capacity) if the worker stays down; any boot failure does the same.
"""

from __future__ import annotations

import base64
import json
import shlex
from dataclasses import dataclass
from string import Template
from typing import Any

#: aarchsci images run as mambauser; isolated pools run task processes as this uid (#32).
DEFAULT_WORKER_UID = 57439
#: Isolated pools: the agent's state (heartbeat, task credentials token), root-owned.
STATE_DIR = "/run/aegc"

_TEMPLATE = Template(
    r"""#!/bin/bash
# aegc worker boot: deployment ${deployment}, pool ${pool}
set -uo pipefail
exec > >(tee -a /var/log/aegc-boot.log) 2>&1
echo "aegc: boot start $$(date -Is)"

TOKEN=$$(curl -sfX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
md() { curl -sf -H "X-aws-ec2-metadata-token: $$TOKEN" \
  "http://169.254.169.254/latest/meta-data/$$1"; }
IID=$$(md instance-id)
REGION=$$(md placement/region)

drain() {
  # Leave the group and decrement its desired capacity. Never just power off: the group would
  # replace the instance. Retried; the watchdog keeps trying if this fails.
  for i in 1 2 3 4 5; do
    aws autoscaling terminate-instance-in-auto-scaling-group --region "$$REGION" \
      --instance-id "$$IID" --should-decrement-desired-capacity && return 0
    sleep $$((i * 5))
  done
  return 1
}
fail() { echo "aegc: boot failed: $$*"; drain; exit 1; }

systemctl disable --now ecs amazon-ecs-volume-plugin || true
systemctl enable --now docker || fail "docker didn't start"
for i in $$(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 || fail "docker isn't ready"

IMAGE=${image}
REGISTRY=$${IMAGE%%/*}
pulled=""
for i in 1 2 3 4 5; do
  if aws ecr get-login-password --region "$$REGION" \
       | docker login --username AWS --password-stdin "$$REGISTRY" \
     && docker pull "$$IMAGE"; then
    pulled=1; break
  fi
  sleep $$((i * 10))
done
[ -n "$$pulled" ] || fail "couldn't pull $$IMAGE"

mkdir -p /scratch && chown ${worker_uid}:${worker_uid} /scratch
${isolation}
cat > /etc/systemd/system/aegc-worker.service <<UNIT
[Unit]
Description=aegc worker (${deployment}/${pool})
After=docker.service
Requires=docker.service
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Restart=on-failure
RestartSec=10
TimeoutStopSec=150
ExecStartPre=-/usr/bin/docker rm -f aegc-worker
ExecStart=/usr/bin/docker run --rm --init --name aegc-worker --network host --ipc host \
  --log-driver awslogs --log-opt awslogs-region=$$REGION --log-opt awslogs-group=${log_group} \
  --log-opt awslogs-format=json/emf \
  --log-opt awslogs-stream=${pool}/$$IID -v /scratch:/scratch --stop-timeout 120 ${docker_flags}\
  -e AEGC_WORKER_CONFIG_B64=${config_b64} -e AEGC_INSTANCE_ID=$$IID -e AWS_REGION=$$REGION \
  -e AEGC_IMAGE=$$IMAGE \
  $$IMAGE aegc-worker run
ExecStop=/usr/bin/docker stop -t 120 aegc-worker

[Install]
WantedBy=multi-user.target
UNIT

cat > /usr/local/bin/aegc-watchdog <<'WATCH'
#!/bin/bash
# Terminate this instance if the worker has been down, or silent, for too long, or if the
# instance has outlived its lifetime (the worker stops claiming at max_lifetime_s; this is the
# backstop: lifetime + the longest walltime + 15 minutes).
HB=${heartbeat}
stale=$$(( $$(date +%s) - $$(stat -c %Y "$$HB" 2>/dev/null || echo 0) ))
up=$$(cut -d. -f1 /proc/uptime)
if ! systemctl is-active --quiet aegc-worker || [ "$$stale" -gt ${watchdog_stale_s} ] \
    || [ "$$up" -gt ${max_age_s} ]; then
  [ "$$up" -lt ${watchdog_grace_s} ] && exit 0
  echo "aegc-watchdog: worker down, stale ($$stale s) or past its lifetime ($$up s); draining"
  TOKEN=$$(curl -sfX PUT http://169.254.169.254/latest/api/token \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
  md() { curl -sf -H "X-aws-ec2-metadata-token: $$TOKEN" \
    "http://169.254.169.254/latest/meta-data/$$1"; }
  region=$$(md placement/region); iid=$$(md instance-id)
  # At the group's minimum the decrement is refused: terminate for a replacement instead.
  aws autoscaling terminate-instance-in-auto-scaling-group --region "$$region" \
    --instance-id "$$iid" --should-decrement-desired-capacity \
  || aws autoscaling terminate-instance-in-auto-scaling-group --region "$$region" \
    --instance-id "$$iid" --no-should-decrement-desired-capacity
fi
WATCH
chmod 755 /usr/local/bin/aegc-watchdog

cat > /etc/systemd/system/aegc-watchdog.service <<'UNIT'
[Unit]
Description=aegc worker watchdog
[Service]
Type=oneshot
ExecStart=/usr/local/bin/aegc-watchdog
UNIT
cat > /etc/systemd/system/aegc-watchdog.timer <<'UNIT'
[Unit]
Description=aegc worker watchdog timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now aegc-worker.service aegc-watchdog.timer || fail "couldn't start the worker"
echo "aegc: boot done $$(date -Is)"
"""
)


@dataclass(frozen=True)
class WorkerBoot:
    deployment: str
    pool: str
    image: str  # <registry>/<repo>@sha256:<digest>
    log_group: str
    worker_config: dict[str, Any]
    isolate: bool = False  # task processes without instance-role access (#32)
    worker_uid: int = DEFAULT_WORKER_UID
    watchdog_stale_s: int = 600
    watchdog_grace_s: int = 900
    max_age_s: int = 24 * 3600 + 41_400 + 900  # lifetime + max walltime + 15 min


def render(boot: WorkerBoot) -> str:
    """The user-data script (plain text; base64-encode it for a launch template)."""
    if "@sha256:" not in boot.image:
        raise ValueError(f"worker image must be pinned by digest, got {boot.image!r}")
    config_b64 = base64.b64encode(
        json.dumps(boot.worker_config, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    return _TEMPLATE.substitute(
        deployment=boot.deployment,
        pool=boot.pool,
        image=shlex.quote(boot.image),
        log_group=boot.log_group,
        config_b64=config_b64,
        worker_uid=boot.worker_uid,
        watchdog_stale_s=boot.watchdog_stale_s,
        watchdog_grace_s=boot.watchdog_grace_s,
        max_age_s=boot.max_age_s,
        isolation=_isolation(boot) if boot.isolate else "",
        docker_flags=_DOCKER_ISOLATED if boot.isolate else "",
        heartbeat=f"{STATE_DIR}/heartbeat" if boot.isolate else "/scratch/.aegc-heartbeat",
    )


#: The agent runs as root, with only the capabilities it needs to start task processes as the
#: task uid, own their directories and signal them; no-new-privileges stops setuid binaries.
_DOCKER_ISOLATED = (
    "--user 0 --security-opt no-new-privileges --cap-drop ALL "
    + " ".join(
        f"--cap-add {c}" for c in ("SETUID", "SETGID", "KILL", "CHOWN", "DAC_OVERRIDE", "FOWNER")
    )
    + f" -v {STATE_DIR}:{STATE_DIR} "
)


def _isolation(boot: WorkerBoot) -> str:
    """Root-owned agent state, and no instance metadata for the task uid: tasks get the task
    role's credentials from the agent instead of the worker role's (#32)."""
    uid = boot.worker_uid
    return (
        f"mkdir -p {STATE_DIR} && chown root:root {STATE_DIR} && chmod 755 {STATE_DIR}\n"
        f"iptables -w -I OUTPUT -d 169.254.169.254 -m owner --uid-owner {uid} -j REJECT \\\n"
        '  || fail "couldn\'t block instance metadata for tasks"\n'
        f"ip6tables -w -I OUTPUT -d fd00:ec2::254 -m owner --uid-owner {uid} -j REJECT "
        "2>/dev/null || true\n"
    )


def encode(script: str) -> str:
    data = script.encode()
    if len(data) > 16 * 1024:
        raise ValueError(f"user data is {len(data)} bytes; EC2 allows 16 KiB")
    return base64.b64encode(data).decode()


# --- the cloud endpoint host (#22) -------------------------------------------------------------

_ENDPOINT_TEMPLATE = Template(
    r"""#!/bin/bash
# aegc cloud endpoint: deployment ${deployment}, endpoint ${name}
set -uo pipefail
exec > >(tee -a /var/log/aegc-boot.log) 2>&1
echo "aegc: endpoint boot start $$(date -Is)"

TOKEN=$$(curl -sfX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
md() { curl -sf -H "X-aws-ec2-metadata-token: $$TOKEN" \
  "http://169.254.169.254/latest/meta-data/$$1"; }
IID=$$(md instance-id)
REGION=$$(md placement/region)
# A failed boot marks the instance unhealthy, so the group replaces it (not a paid, dead host).
fail() {
  echo "aegc: endpoint boot failed: $$*"
  aws autoscaling set-instance-health --region "$$REGION" --instance-id "$$IID" \
    --health-status Unhealthy || true
  exit 1
}
retry() { local n; for n in 1 2 3 4 5 6; do "$$@" && return 0; sleep $$((n * 10)); done; return 1; }

systemctl disable --now ecs amazon-ecs-volume-plugin || true
systemctl enable --now docker || fail "docker didn't start"
for i in $$(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done

IMAGE=${image}
REGISTRY=$${IMAGE%%/*}
pulled=""
for i in 1 2 3 4 5; do
  if aws ecr get-login-password --region "$$REGION" \
       | docker login --username AWS --password-stdin "$$REGISTRY" \
     && docker pull "$$IMAGE"; then
    pulled=1; break
  fi
  sleep $$((i * 10))
done
[ -n "$$pulled" ] || fail "couldn't pull $$IMAGE"

# The Globus login (token storage) and the endpoint id, from Secrets Manager.
GC=${gc_host}
install -d -m 700 -o ${gc_uid} -g ${gc_uid} "$$GC"
${mu_dirs}readsecret() {
  SECRET=$$(aws secretsmanager get-secret-value --region "$$REGION" --secret-id ${secret} \
    --query SecretString --output text)
}
retry readsecret || fail "couldn't read the secret ${secret}"
SECRET="$$SECRET" python3 - "$$GC" <<'PY' || fail "couldn't restore the Globus login"
import base64, json, os, sys
d, s = sys.argv[1], json.loads(os.environ["SECRET"])
with open(os.path.join(d, "storage.db"), "wb") as f:
    f.write(base64.b64decode(s["storage_db_b64"]))
os.chmod(os.path.join(d, "storage.db"), 0o600)
env = "/var/lib/aegc-endpoint/evaluate.env"
with open(env, "w") as f:
    f.write("AEGC_METRIC_TOKEN=" + s.get("metric_token", "") + "\n")
os.chmod(env, 0o600)
if s.get("identity_mapping") is not None:  # a multi-user endpoint's mapping (#62)
    m = "/var/lib/aegc-endpoint/identity_mapping.json"
    with open(m, "w") as f:
        json.dump(s["identity_mapping"], f)
    os.chmod(m, 0o600)
PY
unset SECRET
chown -R ${gc_uid}:${gc_uid} "$$GC"

echo ${configure_b64} | base64 -d > /var/lib/aegc-endpoint/configure.sh
configure() {
  docker run --rm ${run_flags}-v "$$GC":${gc_mount} ${configure_mounts}\
    -v /var/lib/aegc-endpoint/configure.sh:/tmp/configure.sh:ro \
    -e AWS_REGION="$$REGION" "$$IMAGE" sh /tmp/configure.sh
}
retry configure || fail "couldn't configure the endpoint"
# After configure (which creates the endpoint directory): keep the registered endpoint id.
printf '{"endpoint_id": "%s"}' ${endpoint_id} > "$$GC"/${name}/endpoint.json
chown ${gc_uid}:${gc_uid} "$$GC"/${name}/endpoint.json

LOGS="--log-driver awslogs --log-opt awslogs-region=$$REGION --log-opt awslogs-group=${log_group}"
cat > /etc/systemd/system/aegc-endpoint.service <<UNIT
[Unit]
Description=aegc cloud endpoint ${name} (${deployment})
After=docker.service
Requires=docker.service
[Service]
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker rm -f aegc-endpoint
ExecStart=/usr/bin/docker run --rm --init --name aegc-endpoint $$LOGS ${run_flags}\
  --log-opt awslogs-stream=endpoint/$$IID -v $$GC:${gc_mount} ${home_mount}\
  -e GC_ENDPOINT=${name} -e GC_ENDPOINT_UUID=${endpoint_id} ${mu_env}\
  -e AWS_REGION=$$REGION -e AWS_DEFAULT_REGION=$$REGION $$IMAGE
ExecStop=/usr/bin/docker exec aegc-endpoint globus-compute-endpoint stop ${name}
[Install]
WantedBy=multi-user.target
UNIT

# Every 5 minutes: liveness metric, spend caps, lease, idle result queues (aegc evaluate).
cat > /etc/systemd/system/aegc-evaluate.service <<UNIT
[Unit]
Description=aegc evaluate ${deployment}
[Service]
Type=oneshot
TimeoutStartSec=240
EnvironmentFile=/var/lib/aegc-endpoint/evaluate.env
ExecStart=/usr/bin/docker run --rm --init $$LOGS \
  --log-opt awslogs-stream=endpoint/$$IID/evaluate ${eval_flags}-v $$GC:${gc_mount} \
  -e AWS_REGION=$$REGION -e AWS_DEFAULT_REGION=$$REGION -e AEGC_METRIC_TOKEN $$IMAGE \
  aegc --region $$REGION evaluate ${deployment} --endpoint-id ${endpoint_id}
UNIT
cat > /etc/systemd/system/aegc-evaluate.timer <<'UNIT'
[Unit]
Description=aegc evaluate timer
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
AccuracySec=1s
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now aegc-endpoint.service aegc-evaluate.timer || fail "couldn't start"
echo "aegc: endpoint boot done $$(date -Is)"
"""
)


@dataclass(frozen=True)
class EndpointBoot:
    deployment: str
    name: str  # the Globus Compute endpoint name
    endpoint_id: str
    image: str  # <registry>/aegc/endpoint@sha256:<digest>
    secret: str  # the Secrets Manager secret name
    log_group: str
    configure_script: str  # aegc.endpoint.configure_script(...), run in the image at boot
    #: A multi-user endpoint (#62): a root core endpoint with minimal capabilities, its login in
    #: a root-only directory, mapped users' homes on the host, the identity mapping from the secret.
    multi_user: bool = False


#: The multi-user endpoint container's flags (aegc.endpoint.MU_DOCKER_FLAGS, kept here to keep
#: this module free of the endpoint module's imports).
_MU_FLAGS = (
    "--user 0 --security-opt no-new-privileges --cap-drop ALL "
    + " ".join(
        f"--cap-add {c}" for c in ("SETUID", "SETGID", "KILL", "CHOWN", "DAC_OVERRIDE", "FOWNER")
    )
    + " "
)
#: Where the cloud host's configure step finds the identity mapping (from the secret).
MU_IDENTITY_MAPPING = "/tmp/identity_mapping.json"


def _endpoint_mode(multi_user: bool) -> dict[str, str]:
    if not multi_user:
        return {
            "gc_host": "/var/lib/aegc-endpoint/gc", "gc_uid": "1000", "mu_dirs": "",
            "gc_mount": "/home/gc/.globus_compute", "run_flags": "", "configure_mounts": "",
            "home_mount": "", "mu_env": "", "eval_flags": "",
        }  # fmt: skip
    home = "-v /var/lib/aegc-endpoint/home:/home "
    return {
        "gc_host": "/var/lib/aegc-endpoint/core",
        "gc_uid": "0",
        "mu_dirs": "install -d -m 755 -o 0 -g 0 /var/lib/aegc-endpoint/home\n",
        "gc_mount": "/root/.globus_compute",
        "run_flags": _MU_FLAGS,
        "configure_mounts": home + "-v /var/lib/aegc-endpoint/identity_mapping.json:"
        f"{MU_IDENTITY_MAPPING}:ro ",
        "home_mount": home,
        "mu_env": "-e GC_MULTI_USER=1 ",
        # aegc evaluate reads the root-owned login; it needs no capabilities.
        "eval_flags": "--user 0 --security-opt no-new-privileges --cap-drop ALL ",
    }


def render_endpoint(boot: EndpointBoot) -> str:
    if "@sha256:" not in boot.image:
        raise ValueError(f"endpoint image must be pinned by digest, got {boot.image!r}")
    return _ENDPOINT_TEMPLATE.substitute(
        **_endpoint_mode(boot.multi_user),
        deployment=boot.deployment,
        name=shlex.quote(boot.name),
        endpoint_id=shlex.quote(boot.endpoint_id),
        image=shlex.quote(boot.image),
        secret=shlex.quote(boot.secret),
        log_group=boot.log_group,
        configure_b64=base64.b64encode(boot.configure_script.encode()).decode(),
    )
