"""The cloud endpoint's Globus login in Secrets Manager (#22).

One secret per deployment, ``aegc-<d>-endpoint``: JSON with the endpoint's name and id and its
token storage (globus_sdk's SQLite ``storage.db``, base64). Only the endpoint's instance role can
read it. aegc creates it tagged, changes or deletes it only if the tags say it's aegc's, and
deletes it without a recovery window so a redeploy can reuse the name.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from aegc import naming
from aegc.awsutil import is_not_found
from aegc.infra import Outcome, OwnershipError, Report

#: Secrets Manager's limit for a secret value.
MAX_SECRET_BYTES = 64 * 1024


@dataclass(frozen=True)
class EndpointLogin:
    name: str  # the Globus Compute endpoint name
    endpoint_id: str
    storage_db: bytes  # ~/.globus_compute/storage.db

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "endpoint_id": self.endpoint_id,
                "storage_db_b64": base64.b64encode(self.storage_db).decode(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> EndpointLogin:
        d = json.loads(text)
        return cls(
            name=str(d["name"]),
            endpoint_id=str(d["endpoint_id"]),
            storage_db=base64.b64decode(d["storage_db_b64"]),
        )


def _owned(sm: Any, name: str, deployment: str) -> bool | None:
    """True if aegc's, False if someone else's, None if there's no such secret."""
    try:
        d = sm.describe_secret(SecretId=name)
    except Exception as e:
        if is_not_found(e):
            return None
        raise
    if d.get("DeletedDate"):
        return None
    return naming.is_owned(name, naming.tag_dict(d.get("Tags")), deployment)


def put_login(
    sm: Any, deployment: str, login: EndpointLogin, report: Report, *, dry_run: bool,
    extra_tags: dict[str, str] | None = None,
) -> str:  # fmt: skip
    """Create or update the secret; returns its ARN (or the name, in a dry run)."""
    name = naming.endpoint_secret(deployment)
    value = login.to_json()
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise ValueError(
            f"the endpoint's Globus login is {len(value.encode())} bytes; Secrets Manager allows "
            f"{MAX_SECRET_BYTES}"
        )
    owned = _owned(sm, name, deployment)
    if owned is False:
        raise OwnershipError(f"secret {name} exists but isn't tagged as {deployment}'s")
    outcome: Outcome
    if owned is None:
        outcome = "would-create" if dry_run else "created"
        arn = name
        if not dry_run:
            arn = sm.create_secret(
                Name=name,
                Description=f"aegc: Globus login of deployment {deployment}'s cloud endpoint",
                SecretString=value,
                Tags=naming.tag_list(naming.tags(deployment, "endpoint-secret", extra=extra_tags)),
            )["ARN"]
    else:
        outcome = "would-update" if dry_run else "updated"
        arn = name
        if not dry_run:
            sm.put_secret_value(SecretId=name, SecretString=value)
            arn = sm.describe_secret(SecretId=name)["ARN"]
    report.add("secret", name, outcome, f"endpoint {login.name} ({login.endpoint_id})")
    return str(arn)


def get_login(sm: Any, deployment: str) -> EndpointLogin:
    value = sm.get_secret_value(SecretId=naming.endpoint_secret(deployment))["SecretString"]
    return EndpointLogin.from_json(value)


def delete_login(sm: Any, deployment: str, report: Report, *, dry_run: bool) -> None:
    name = naming.endpoint_secret(deployment)
    owned = _owned(sm, name, deployment)
    if owned is None:
        return
    if owned is False:
        report.add("secret", name, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    report.add("secret", name, "would-delete" if dry_run else "deleted")


def exists(sm: Any, deployment: str) -> bool:
    return bool(_owned(sm, naming.endpoint_secret(deployment), deployment))
