"""boto3 session and client helpers shared by the engine, worker and CLI."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

#: Standard retry mode with a bounded number of attempts and timeouts that never hang a heartbeat.
CLIENT_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 5},
    connect_timeout=10,
    read_timeout=30,
)


def session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def client(sess: boto3.Session, service: str) -> Any:
    return sess.client(service, config=CLIENT_CONFIG)  # type: ignore[call-overload]


def paginate(
    client: Any, operation: str, key: str, *, page_size: int | None = None, **kwargs: Any
) -> Iterator[Any]:
    """Every item under ``key`` across all pages of ``operation``.

    Use it for any listing that decides what aegc owns or deletes: a single call returns only the
    first page, and resources on later pages would be missed. Some APIs page only when asked
    (SQS ListQueues returns at most 1,000 URLs and no token unless MaxResults is set), so pass
    ``page_size`` for those.
    """
    config = {"PageSize": page_size} if page_size else {}
    for page in client.get_paginator(operation).paginate(PaginationConfig=config, **kwargs):
        yield from page.get(key, [])


def account_id(sess: boto3.Session) -> str:
    return str(client(sess, "sts").get_caller_identity()["Account"])


def error_code(e: BaseException) -> str:
    if isinstance(e, ClientError):
        return str(e.response.get("Error", {}).get("Code", ""))
    return ""


def is_not_found(e: BaseException) -> bool:
    code = error_code(e)
    return code.endswith(("NotFound", "NotFoundException")) or code in {
        "NoSuchEntity",
        "NoSuchBucket",
        "NoSuchKey",
        "ParameterNotFound",
        "AWS.SimpleQueueService.NonExistentQueue",
        "QueueDoesNotExist",
        "PullThroughCacheRuleNotFoundException",
        "RepositoryNotFoundException",
        "ResourceNotFoundException",
    }
