"""Large task outputs: write them to S3 inside a task and return the URI (#54).

Globus Compute results are capped at about 10 MB and Globus Flows state at 200 KB, so a task that
makes something bigger writes it under one of the deployment's write grants and returns the
``s3://`` URI instead::

    def render(scene):
        from aegc import outputs
        path = make_movie(scene)  # a file path (str or Path)
        return outputs.put(path)  # s3://bucket/out/<task uuid>/movie.mp4

Deploy with ``aegc deploy <d> --grant write=s3://bucket/out/`` first. Keys are
``<grant prefix>/<task uuid>/<name>``, so tasks never overwrite each other. The worker's instance
role provides the credentials; :func:`get` and :func:`download` also work on a laptop with your own.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aegc.infra.iam import Grant, split_s3_uri

_counter = itertools.count(1)


def task_uuid() -> str:
    """The running task's uuid (``local`` outside an aegc worker)."""
    return os.environ.get("AEGC_TASK_UUID") or "local"


def _config() -> Any:
    from aegc.worker.config import WorkerConfig

    if not os.environ.get("AEGC_WORKER_CONFIG_B64"):
        return None
    return WorkerConfig.from_env()


def _client(region: str | None = None) -> Any:
    import boto3

    if region is None and (cfg := _config()) is not None:
        region = cfg.region
    return boto3.client("s3", region_name=region)


#: How long a worker trusts its copy of the deployment's grants.
GRANTS_TTL_S = 60.0
_grants_cache: tuple[float, list[dict[str, str]]] | None = None


def _current_grants(cfg: Any) -> list[dict[str, str]]:
    """The deployment's grants now, from its manifest (a redeploy may have changed them since
    this worker booted); the boot-time copy if the manifest can't be read."""
    global _grants_cache
    now = time.monotonic()
    if _grants_cache is not None and now - _grants_cache[0] < GRANTS_TTL_S:
        return _grants_cache[1]
    grants = [dict(g) for g in cfg.grants]
    try:
        import boto3

        from aegc import naming

        ssm = boto3.client("ssm", region_name=cfg.region)
        name = naming.deployment_manifest_parameter(cfg.deployment)
        manifest = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
        grants = [dict(g) for g in manifest.get("grants", [])]
    except Exception:
        pass
    _grants_cache = (now, grants)
    return grants


def write_grants() -> list[Grant]:
    """This deployment's write grants, in the order they were given."""
    cfg = _config()
    if cfg is None:
        return []
    return [g for g in (Grant.from_dict(d) for d in _current_grants(cfg)) if g.access == "write"]


def _join(*parts: str) -> str:
    out = ""
    for p in parts:
        if not p:
            continue
        out = p if not out else (out + p if out.endswith("/") else f"{out}/{p}")
    return out


def put(
    data: bytes | bytearray | memoryview | str | os.PathLike[str],
    name: str | None = None,
    *,
    prefix: str | None = None,
) -> str:
    """Upload ``data`` and return its ``s3://`` URI.

    ``data`` is bytes, or a path (``str`` or ``pathlib.Path``) to a file; for text, pass
    ``text.encode()``. A path that doesn't exist raises FileNotFoundError.
    It goes to ``<prefix>/<task uuid>/<name>``, where the prefix is the first write grant's, or
    ``prefix`` (``s3://bucket/some/prefix``, inside a write grant). ``name`` defaults to the
    file's name, or ``output-<n>``. Large files go up in parts.
    """
    grants = write_grants()
    if not grants:
        raise PermissionError(
            "no write grant: deploy with `aegc deploy <d> --grant write=s3://bucket/prefix`"
        )
    path = Path(data) if isinstance(data, (str, os.PathLike)) else None
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(
                f"no file {str(path)!r} to upload (put takes bytes or a file path; for text, "
                "pass text.encode())"
            )
        name = name or path.name
    if not name:
        name = f"output-{next(_counter)}"
    if prefix is None:
        bucket, base = grants[0].bucket, grants[0].prefix
    else:
        bucket, key_prefix = split_s3_uri(prefix)
        base = key_prefix or ""
    key = _join(base, task_uuid(), name.lstrip("/"))
    if not any(g.covers(bucket, key) for g in grants):
        raise PermissionError(
            f"s3://{bucket}/{key} isn't inside a write grant ("
            + ", ".join(g.uri for g in grants)
            + "); grant it with `aegc deploy <d> --grant write=s3://...`"
        )
    s3 = _client()
    if path is not None:
        s3.upload_file(str(path), bucket, key)
    else:
        assert not isinstance(data, (str, os.PathLike))
        s3.upload_fileobj(io.BytesIO(bytes(data)), bucket, key)
    return f"s3://{bucket}/{key}"


def _object(uri: str) -> tuple[str, str]:
    bucket, key = split_s3_uri(uri)
    if not key:
        raise ValueError(f"not an object URI: {uri!r}")
    return bucket, key


def get(uri: str) -> bytes:
    """An object's bytes."""
    bucket, key = _object(uri)
    return bytes(_client().get_object(Bucket=bucket, Key=key)["Body"].read())


def download(uri: str, path: str | os.PathLike[str]) -> Path:
    """Download an object to ``path`` (in parts if it's large); returns the path."""
    bucket, key = _object(uri)
    _client().download_file(bucket, key, str(path))
    return Path(path)


def uri_to_https(uri: str, *, expires_s: int = 0, region: str | None = None) -> str:
    """An ``https://`` URL for an ``s3://`` URI.

    With ``expires_s``, a presigned GET URL that anyone can use until it expires. Signed on a
    worker, it also stops working when the instance role's credentials expire (a few hours), so
    presign long-lived links on your own machine. Without it, the plain virtual-hosted URL, which
    needs the object to be readable some other way.
    """
    bucket, key = _object(uri)
    s3 = _client(region)
    if expires_s:
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=int(expires_s)
        )
        return str(url)
    region = region or s3.meta.region_name or "us-east-1"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{quote(key)}"
