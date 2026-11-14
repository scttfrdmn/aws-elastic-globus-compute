"""Inline-or-S3 bodies for protocol messages, shared by the engine and the worker."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from aegc.protocol import DEFAULT_INLINE_LIMIT, Payload, ProtocolError, S3Ref

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class PayloadMissing(ProtocolError):
    """The S3 object a payload points to no longer exists."""


def make_payload(
    data: bytes,
    *,
    s3: S3Client,
    bucket: str,
    key: str,
    inline_limit: int = DEFAULT_INLINE_LIMIT,
) -> Payload:
    """Return an inline payload for small bodies; otherwise upload to S3 and return a pointer."""
    if len(data) <= inline_limit:
        return Payload.inline(data)
    digest = hashlib.sha256(data).hexdigest()
    s3.put_object(Bucket=bucket, Key=key, Body=data, ChecksumAlgorithm="SHA256")
    return Payload(s3=S3Ref(bucket=bucket, key=key, size=len(data), sha256=digest))


def read_payload(payload: Payload, *, s3: S3Client | None) -> bytes:
    """Return the payload's bytes, fetching and verifying it from S3 if needed."""
    if payload.inline_b64 is not None:
        return payload.inline_bytes()
    ref = payload.s3
    assert ref is not None
    if s3 is None:
        raise ProtocolError("an S3 client is needed to read an S3 payload")
    try:
        obj = s3.get_object(Bucket=ref.bucket, Key=ref.key)
    except s3.exceptions.NoSuchKey as e:
        raise PayloadMissing(f"s3://{ref.bucket}/{ref.key} is missing") from e
    data = obj["Body"].read()
    if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ProtocolError(f"s3://{ref.bucket}/{ref.key} doesn't match its size or sha256")
    return data


def delete_payload(payload: Payload, *, s3: S3Client) -> None:
    """Delete an S3-backed payload. Inline payloads need nothing."""
    if payload.s3 is not None:
        s3.delete_object(Bucket=payload.s3.bucket, Key=payload.s3.key)
