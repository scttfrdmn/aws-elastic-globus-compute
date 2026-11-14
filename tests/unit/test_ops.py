import time
from collections.abc import Callable
from typing import Any

import pytest

from aegc import cli, naming, ops


def test_parse_pool() -> None:
    assert cli.parse_pool("geospatial") == {"env": "geospatial"}
    assert cli.parse_pool("climate:max=8,vcpu=4-16,mem=8192,slots=2,on-demand,idle=120") == {
        "env": "climate", "max_workers": 8, "vcpu_min": 4, "vcpu_max": 16,
        "memory_mib_min": 8192, "slots": "2", "spot": False, "idle_timeout_s": 120,
    }  # fmt: skip
    with pytest.raises(SystemExit):
        cli.parse_pool("x:gpu=1")


def test_reap_only_idle_empty_owned_queues(aws: Callable[[str], Any]) -> None:
    sqs = aws("sqs")
    tags = naming.tags("dev", "result-queue")
    idle = sqs.create_queue(QueueName="aegc-dev-res-aaaaaaaaaaaa", tags=tags)["QueueUrl"]
    busy = sqs.create_queue(QueueName="aegc-dev-res-bbbbbbbbbbbb", tags=tags)["QueueUrl"]
    sqs.send_message(QueueUrl=busy, MessageBody="{}")
    foreign = sqs.create_queue(QueueName="aegc-dev-res-cccccccccccc")["QueueUrl"]  # untagged
    later = time.time() + ops.RESULT_QUEUE_IDLE_S + 60

    dry = {a.name: a.outcome for a in ops.reap(sqs=sqs, deployment="dev", now=later).actions}
    assert dry == {"aegc-dev-res-aaaaaaaaaaaa": "would-delete", "aegc-dev-res-bbbbbbbbbbbb": "kept"}
    assert {a.name: a.outcome for a in ops.reap(sqs=sqs, deployment="dev").actions}[
        "aegc-dev-res-aaaaaaaaaaaa"
    ] == "kept"  # not idle yet

    ops.reap(sqs=sqs, deployment="dev", dry_run=False, now=later)
    left = sqs.list_queues(QueueNamePrefix="aegc-dev-res-").get("QueueUrls", [])
    assert sorted(left) == sorted([busy, foreign]) and idle not in left
