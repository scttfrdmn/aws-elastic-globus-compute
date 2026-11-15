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

#: aarchsci images run as mambauser.
DEFAULT_WORKER_UID = 57439

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
  --log-opt awslogs-stream=${pool}/$$IID -v /scratch:/scratch --stop-timeout 120 \
  -e AEGC_WORKER_CONFIG_B64=${config_b64} -e AEGC_INSTANCE_ID=$$IID -e AWS_REGION=$$REGION \
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
HB=/scratch/.aegc-heartbeat
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
    )


def encode(script: str) -> str:
    data = script.encode()
    if len(data) > 16 * 1024:
        raise ValueError(f"user data is {len(data)} bytes; EC2 allows 16 KiB")
    return base64.b64encode(data).decode()
