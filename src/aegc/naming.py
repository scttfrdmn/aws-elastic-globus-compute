"""Resource names, ownership tags, and the rule for what aegc may delete.

Every resource aegc creates carries ``aegc:managed=true`` and ``aegc:deployment=<d>`` (except
region-wide bootstrap resources, which carry ``aegc:deployment=_bootstrap``), applied on the create
call itself. aegc deletes a resource only if both its name and its tags say aegc made it for that
deployment: see :func:`is_owned`. Anything else is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from aegc import __version__

PREFIX = "aegc"
TAG_MANAGED = "aegc:managed"
TAG_DEPLOYMENT = "aegc:deployment"
TAG_POOL = "aegc:pool"
TAG_ROLE = "aegc:role"
TAG_VERSION = "aegc:version"
TAG_ENDPOINT = "aegc:endpoint-id"

#: Deployment value used for region-wide resources created by ``aegc bootstrap``.
BOOTSTRAP = "_bootstrap"

#: Deployment names stay short: they appear in every resource name, including the IAM role.
MAX_DEPLOYMENT = 20
#: Pool names are ``<env>-py<NNN>``. The longest name built from one is the dead-letter queue,
#: ``aegc-<deployment>-<pool>-dlq``, and SQS allows 80 characters.
MAX_POOL = 40
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
IAM_PATH = "/aegc/"


class NameError_(ValueError):
    """An invalid deployment or pool name."""


def validate_name(kind: str, value: str) -> str:
    """Deployment (up to 20 chars) and pool (up to 40) names: lowercase [a-z0-9-], from a letter."""
    limit = MAX_POOL if kind == "pool" else MAX_DEPLOYMENT
    if len(value) > limit or not _NAME.match(value) or value.endswith("-") or "--" in value:
        raise NameError_(
            f"{kind} name {value!r} must be 1-{limit} chars of [a-z0-9-], start with a letter, "
            "and not end with '-' or contain '--'"
        )
    return value


def tags(
    deployment: str,
    role: str,
    *,
    pool: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The ownership tag set for a new resource."""
    t = {
        TAG_MANAGED: "true",
        TAG_DEPLOYMENT: deployment,
        TAG_ROLE: role,
        TAG_VERSION: __version__,
    }
    if pool is not None:
        t[TAG_POOL] = pool
    if extra:
        for k in extra:
            if k.startswith("aegc:"):
                raise ValueError(f"user tags can't use the reserved aegc: prefix ({k})")
        t.update(extra)
    return t


def tag_list(t: Mapping[str, str]) -> list[dict[str, str]]:
    """``[{"Key": ..., "Value": ...}]`` form used by EC2, IAM, ECR and Auto Scaling."""
    return [{"Key": k, "Value": v} for k, v in t.items()]


def tag_dict(items: Iterable[Mapping[str, str]] | Mapping[str, str] | None) -> dict[str, str]:
    """Normalise any AWS tag representation to a plain dict."""
    if items is None:
        return {}
    if isinstance(items, Mapping) and "Key" not in items:
        return {str(k): str(v) for k, v in items.items()}
    return {str(i["Key"]): str(i["Value"]) for i in items}  # type: ignore[index]


def is_owned(name: str, resource_tags: Mapping[str, str], deployment: str) -> bool:
    """True only if aegc created this resource for ``deployment``.

    Both checks must pass: the name carries aegc's prefix for this deployment (or the bootstrap
    prefix), and the tags say ``aegc:managed=true`` with the matching ``aegc:deployment``.
    """
    if resource_tags.get(TAG_MANAGED) != "true":
        return False
    if resource_tags.get(TAG_DEPLOYMENT) != deployment:
        return False
    leaf = name.rsplit("/", 1)[-1]
    if deployment == BOOTSTRAP:
        return leaf.startswith(f"{PREFIX}-")
    return leaf.startswith(f"{PREFIX}-{deployment}-") or leaf == f"{PREFIX}-{deployment}"


# --- names ---------------------------------------------------------------------------------


def bucket_name(account: str, region: str) -> str:
    return f"{PREFIX}-{account}-{region}"


def pull_through_prefix() -> str:
    return f"{PREFIX}-quay"


def bootstrap_parameter() -> str:
    return f"/{PREFIX}/bootstrap"


def deployment_manifest_parameter(deployment: str) -> str:
    return f"/{PREFIX}/{deployment}/manifest"


def image_parameter(env: str) -> str:
    return f"/{PREFIX}/images/{env}"


def worker_role(deployment: str, region: str) -> str:
    # IAM names are account-global, so include the region.
    return f"{PREFIX}-{deployment}-worker-{region}"


def ecr_template_role(region: str) -> str:
    """Region-wide role ECR assumes to create and tag pull-through cache repositories."""
    return f"{PREFIX}-ecr-template-{region}"


def security_group(deployment: str) -> str:
    return f"{PREFIX}-{deployment}-workers"


def log_group(deployment: str) -> str:
    return f"/{PREFIX}/{deployment}"


def task_queue(deployment: str, pool: str) -> str:
    return f"{PREFIX}-{deployment}-{pool}-tasks"


def dead_letter_queue(deployment: str, pool: str) -> str:
    return f"{PREFIX}-{deployment}-{pool}-dlq"


def result_queue(deployment: str, endpoint_id: str) -> str:
    """One result queue per user endpoint process (each has its own endpoint id)."""
    return f"{PREFIX}-{deployment}-res-{endpoint_id.replace('-', '')[:12]}"


def launch_template(deployment: str, pool: str) -> str:
    return f"{PREFIX}-{deployment}-{pool}"


def auto_scaling_group(deployment: str, pool: str) -> str:
    return f"{PREFIX}-{deployment}-{pool}"


def image_repository(env: str) -> str:
    return f"{PREFIX}/{env}"
