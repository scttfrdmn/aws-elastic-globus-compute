import base64
import json
import shutil
import subprocess

import pytest

from aegc.infra import userdata

BOOT = userdata.WorkerBoot(
    deployment="dev",
    pool="sci-py314",
    image="123456789012.dkr.ecr.us-west-2.amazonaws.com/aegc/geospatial@sha256:" + "a" * 64,
    log_group="/aegc/dev",
    worker_config={"task_queue_url": "https://sqs/aegc-dev-sci-py314-tasks", "slots": "auto"},
)


def test_render_has_the_spike_d_steps_in_order() -> None:
    s = userdata.render(BOOT)
    steps = [
        "http://169.254.169.254/latest/api/token",
        "systemctl disable --now ecs amazon-ecs-volume-plugin",
        "systemctl enable --now docker",
        "aws ecr get-login-password",
        'docker pull "$IMAGE"',
        "chown 57439:57439 /scratch",
        "aegc-worker run",
        "systemctl enable --now aegc-worker.service aegc-watchdog.timer",
    ]
    positions = [s.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "--should-decrement-desired-capacity" in s
    assert "shutdown -h" not in s, "the group would just replace a shut-down instance"


def test_worker_config_round_trips() -> None:
    s = userdata.render(BOOT)
    b64 = s.split("AEGC_WORKER_CONFIG_B64=")[1].split()[0]
    assert json.loads(base64.b64decode(b64)) == BOOT.worker_config


def test_image_must_be_pinned_by_digest() -> None:
    with pytest.raises(ValueError, match="digest"):
        userdata.render(
            userdata.WorkerBoot(**{**BOOT.__dict__, "image": "quay.io/aarchsci/geospatial:latest"})
        )


def test_fits_ec2_user_data_limit() -> None:
    assert len(base64.b64decode(userdata.encode(userdata.render(BOOT)))) < 16 * 1024
    with pytest.raises(ValueError, match="16 KiB"):
        userdata.encode("x" * (16 * 1024 + 1))


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_bash_syntax(tmp_path: object) -> None:
    script = userdata.render(BOOT)
    subprocess.run(["bash", "-n"], input=script.encode(), check=True)
    # The watchdog script embedded in a quoted heredoc must also parse.
    watchdog = script.split("<<'WATCH'\n", 1)[1].split("\nWATCH\n", 1)[0]
    subprocess.run(["bash", "-n"], input=watchdog.encode(), check=True)
