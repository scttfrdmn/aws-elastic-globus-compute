from collections.abc import Callable
from typing import Any

from aegc.awsutil import paginate


def test_paginate_follows_every_page(aws: Callable[[str], Any]) -> None:
    # Listings that decide ownership must see past the first page (#26).
    sqs = aws("sqs")
    names = [f"aegc-pg-q{i}" for i in range(3)]
    for n in names:
        sqs.create_queue(QueueName=n)
    urls = list(paginate(sqs, "list_queues", "QueueUrls", page_size=1, QueueNamePrefix="aegc-pg-"))
    assert sorted(u.rsplit("/", 1)[-1] for u in urls) == names

    logs = aws("logs")
    for i in range(3):
        logs.create_log_group(logGroupName=f"/aegc/pg{i}")
    groups = list(
        paginate(
            logs, "describe_log_groups", "logGroups", page_size=1, logGroupNamePrefix="/aegc/pg"
        )
    )
    assert len(groups) == 3
