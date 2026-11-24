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
from aegc.infra import Outcome, OwnershipError, Report

#: Secrets Manager's limit for a secret value.
MAX_SECRET_BYTES = 64 * 1024


@dataclass(frozen=True)
class EndpointLogin:
    name: str  # the Globus Compute endpoint name
    endpoint_id: str
    storage_db: bytes  # ~/.globus_compute/storage.db
    # Dimension of the EndpointOnline metric, so task code (printing EMF into worker logs, which
    # has no Secrets Manager access) can't keep the offline alarm quiet. It's also a tag on the
    # secret, readable by whoever may ListSecrets.
    metric_token: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "endpoint_id": self.endpoint_id,
                "storage_db_b64": base64.b64encode(self.storage_db).decode(),
                "metric_token": self.metric_token,
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
            metric_token=str(d.get("metric_token", "")),
        )


def _describe(sm: Any, name: str) -> dict[str, Any] | None:
    """The secret's metadata (never its value), found with ListSecrets so that no
    resource-tag condition applies; None if there's no such secret."""
    for page in sm.get_paginator("list_secrets").paginate(
        Filters=[{"Key": "name", "Values": [name]}]
    ):
        for s in page.get("SecretList", []):
            if s.get("Name") == name and not s.get("DeletedDate"):
                return dict(s)
    return None


def _owned(sm: Any, name: str, deployment: str) -> bool | None:
    """True if aegc's, False if someone else's, None if there's no such secret."""
    d = _describe(sm, name)
    if d is None:
        return None
    return naming.is_owned(name, naming.tag_dict(d.get("Tags")), deployment)


def resource_policy(*, endpoint_role_arn: str) -> dict[str, Any]:
    """Only the endpoint host's role may read the login, whatever else IAM allows in the account
    (account-wide GetSecretValue, the endpoint-deployer policy...)."""
    return reader_policy(endpoint_role_arn, sid="OnlyTheEndpointHost")


def reader_policy(role_arn: str, *, sid: str) -> dict[str, Any]:
    """A secret's resource policy: no principal but ``role_arn`` may read its value."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": sid,
                "Effect": "Deny",
                "Principal": "*",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": "*",
                "Condition": {"StringNotEquals": {"aws:PrincipalArn": role_arn}},
            }
        ],
    }


def put_owned_secret(
    sm: Any, *, name: str, deployment: str, value: str, role: str, description: str,
    reader_role_arn: str, sid: str, report: Report, dry_run: bool, detail: str = "",
    extra_tags: dict[str, str] | None = None,
) -> str:  # fmt: skip
    """Create or update an aegc secret readable by ``reader_role_arn`` only (tagged, with a hash
    tag so an unchanged value isn't rewritten). Refuses one that isn't aegc's. Returns its ARN
    (or the name, in a dry run)."""
    import hashlib

    if len(value.encode()) > MAX_SECRET_BYTES:
        raise ValueError(
            f"secret {name} would be {len(value.encode())} bytes; Secrets Manager allows "
            f"{MAX_SECRET_BYTES}"
        )
    current = _describe(sm, name)
    if current is not None and not naming.is_owned(
        name, naming.tag_dict(current.get("Tags")), deployment
    ):
        raise OwnershipError(f"secret {name} exists but isn't tagged as {deployment}'s")
    digest = hashlib.sha256(value.encode()).hexdigest()
    tags = {**naming.tags(deployment, role, extra=extra_tags), TAG_LOGIN_HASH: digest}
    outcome: Outcome
    arn = str(current["ARN"]) if current else name
    if current is None:
        outcome = "would-create" if dry_run else "created"
        if not dry_run:
            arn = str(
                sm.create_secret(
                    Name=name, Description=description, SecretString=value,
                    Tags=naming.tag_list(tags),
                )["ARN"]
            )  # fmt: skip
    elif naming.tag_dict(current.get("Tags")).get(TAG_LOGIN_HASH) == digest:
        outcome = "exists"
    else:
        outcome = "would-update" if dry_run else "updated"
        if not dry_run:
            sm.put_secret_value(SecretId=name, SecretString=value)
            sm.tag_resource(SecretId=name, Tags=naming.tag_list(tags))
    if not dry_run:
        sm.put_resource_policy(
            SecretId=name, ResourcePolicy=json.dumps(reader_policy(reader_role_arn, sid=sid))
        )
    report.add("secret", name, outcome, detail)
    return arn


def delete_owned_secret(
    sm: Any, name: str, deployment: str, report: Report, *, dry_run: bool
) -> None:
    """Delete an aegc secret (no recovery window, so the name can be reused); others are kept."""
    owned = _owned(sm, name, deployment)
    if owned is None:
        return
    if owned is False:
        report.add("secret", name, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    report.add("secret", name, "would-delete" if dry_run else "deleted")


def owned_secrets(sm: Any, prefix: str, deployment: str) -> list[tuple[dict[str, Any], bool]]:
    """Secrets whose name starts with ``prefix``, each with whether it's ``deployment``'s."""
    out = []
    for page in sm.get_paginator("list_secrets").paginate(
        Filters=[{"Key": "name", "Values": [prefix]}]
    ):
        for s in page.get("SecretList", []):
            name = str(s.get("Name", ""))
            if name.startswith(prefix) and not s.get("DeletedDate"):
                out.append(
                    (dict(s), naming.is_owned(name, naming.tag_dict(s.get("Tags")), deployment))
                )
    return out


def metadata(sm: Any, deployment: str) -> dict[str, str]:
    """The endpoint's name and id from the secret's tags (for status: no read of the value)."""
    d = _describe(sm, naming.endpoint_secret(deployment))
    tags = naming.tag_dict(d.get("Tags")) if d else {}
    return {"name": tags.get(TAG_NAME, ""), "endpoint_id": tags.get(naming.TAG_ENDPOINT, "")}


def find_endpoint(sm: Any, endpoint_id: str) -> list[str]:
    """Deployments whose secret holds ``endpoint_id`` (one endpoint id runs in one place)."""
    out = []
    for page in sm.get_paginator("list_secrets").paginate(
        Filters=[{"Key": "tag-key", "Values": [naming.TAG_ENDPOINT]}]
    ):
        for s in page.get("SecretList", []):
            tags = naming.tag_dict(s.get("Tags"))
            if (
                tags.get(naming.TAG_ENDPOINT) == endpoint_id
                and tags.get(naming.TAG_MANAGED) == "true"
            ):
                out.append(tags.get(naming.TAG_DEPLOYMENT, "?"))
    return out


TAG_NAME = "aegc:endpoint-name"
TAG_TOKEN = "aegc:metric-token"
TAG_LOGIN_HASH = "aegc:login-sha256"


def metric_token(sm: Any, deployment: str) -> str:
    """The deployment's liveness-metric token from the secret's tags ("" if none yet)."""
    d = _describe(sm, naming.endpoint_secret(deployment))
    return naming.tag_dict(d.get("Tags")).get(TAG_TOKEN, "") if d else ""


def put_login(
    sm: Any, deployment: str, login: EndpointLogin, report: Report, *, dry_run: bool,
    extra_tags: dict[str, str] | None = None, endpoint_role_arn: str = "",
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
    elsewhere = [x for x in find_endpoint(sm, login.endpoint_id) if x != deployment]
    if elsewhere:
        raise ValueError(
            f"endpoint {login.name} ({login.endpoint_id}) is already the cloud endpoint of "
            f"deployment {', '.join(elsewhere)}; one endpoint id can run in one place "
            "(`aegc endpoint undeploy` it there first)"
        )
    import hashlib

    digest = hashlib.sha256(value.encode()).hexdigest()
    tags = naming.tags(deployment, "endpoint-secret", extra=extra_tags)
    tags.update({
        naming.TAG_ENDPOINT: login.endpoint_id,
        TAG_NAME: login.name,
        TAG_TOKEN: login.metric_token,
        TAG_LOGIN_HASH: digest,
    })  # fmt: skip
    current = _describe(sm, name)
    unchanged = (
        current is not None and naming.tag_dict(current.get("Tags")).get(TAG_LOGIN_HASH) == digest
    )
    outcome: Outcome
    if owned is None:
        outcome = "would-create" if dry_run else "created"
        arn = name
        if not dry_run:
            arn = sm.create_secret(
                Name=name,
                Description=f"aegc: Globus login of deployment {deployment}'s cloud endpoint",
                SecretString=value,
                Tags=naming.tag_list(tags),
            )["ARN"]
    elif unchanged:
        outcome = "exists"
        arn = str(current["ARN"]) if current else name
    else:
        outcome = "would-update" if dry_run else "updated"
        arn = name
        if not dry_run:
            sm.put_secret_value(SecretId=name, SecretString=value)
            sm.tag_resource(SecretId=name, Tags=naming.tag_list(tags))
            found = _describe(sm, name)
            arn = str(found["ARN"]) if found else name
    if endpoint_role_arn and not dry_run:
        sm.put_resource_policy(
            SecretId=name,
            ResourcePolicy=json.dumps(resource_policy(endpoint_role_arn=endpoint_role_arn)),
        )
    report.add("secret", name, outcome, f"endpoint {login.name} ({login.endpoint_id})")
    return str(arn)


def get_login(sm: Any, deployment: str) -> EndpointLogin:
    value = sm.get_secret_value(SecretId=naming.endpoint_secret(deployment))["SecretString"]
    return EndpointLogin.from_json(value)


def delete_login(sm: Any, deployment: str, report: Report, *, dry_run: bool) -> None:
    delete_owned_secret(sm, naming.endpoint_secret(deployment), deployment, report, dry_run=dry_run)


def exists(sm: Any, deployment: str) -> bool:
    return bool(_owned(sm, naming.endpoint_secret(deployment), deployment))
