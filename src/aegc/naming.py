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
#: Trigger names (#83): the longest name built from one is the trigger role,
#: ``aegc-<deployment>-trigger-<name>-<region>``, and IAM allows 64 characters.
MAX_TRIGGER = 15
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
IAM_PATH = "/aegc/"


class NameError_(ValueError):
    """An invalid deployment or pool name."""


def validate_name(kind: str, value: str) -> str:
    """Deployment (up to 20 chars) and pool (up to 40) names: lowercase [a-z0-9-], from a letter."""
    limit = {"pool": MAX_POOL, "trigger": MAX_TRIGGER}.get(kind, MAX_DEPLOYMENT)
    if len(value) > limit or not _NAME.match(value) or value.endswith("-") or "--" in value:
        raise NameError_(
            f"{kind} name {value!r} must be 1-{limit} chars of [a-z0-9-], start with a letter, "
            "and not end with '-' or contain '--'"
        )
    if kind == "pool" and (value == TRIGGER or value.startswith(f"{TRIGGER}-")):
        # A pool's function, role and lease schedule would collide with a trigger's (#83).
        raise NameError_(f"pool names can't be {TRIGGER!r} or start with '{TRIGGER}-' (reserved)")
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


def state_parameter(deployment: str) -> str:
    return f"/{PREFIX}/{deployment}/state"


def image_parameter(env: str) -> str:
    """The env's newest image, in the single-record form aegc 0.1 and 0.2 read."""
    return f"/{PREFIX}/images/{env}"


def image_records_parameter(env: str) -> str:
    """All of the env's images, one per Python minor (aegc 0.3+)."""
    return f"/{PREFIX}/images/{env}/records"


def worker_role(deployment: str, region: str) -> str:
    # IAM names are account-global, so include the region.
    return f"{PREFIX}-{deployment}-worker-{region}"


def task_role(deployment: str, region: str) -> str:
    """The role task code runs with (its grants), assumed by the agent for its tasks (#32)."""
    return f"{PREFIX}-{deployment}-task-{region}"


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
    if env == ENDPOINT:
        raise NameError_(f"env name {ENDPOINT!r} is reserved for the endpoint image")
    return f"{PREFIX}/{env}"


# --- the cloud-hosted endpoint (#22): one per deployment ---------------------------------------

#: Reserved: the endpoint's image repository (aegc/endpoint) and its resources' names.
ENDPOINT = "endpoint"


def endpoint_role(deployment: str, region: str) -> str:
    return f"{PREFIX}-{deployment}-endpoint-{region}"


def endpoint_group(deployment: str) -> str:
    """The endpoint host's launch template and Auto Scaling group."""
    return f"{PREFIX}-{deployment}-{ENDPOINT}"


def endpoint_secret(deployment: str) -> str:
    """The Secrets Manager secret with the endpoint's Globus login."""
    return f"{PREFIX}-{deployment}-{ENDPOINT}"


def endpoint_alarm(deployment: str) -> str:
    return f"{PREFIX}-{deployment}-endpoint-offline"


def endpoint_topic(deployment: str) -> str:
    return f"{PREFIX}-{deployment}-endpoint-alarm"


def endpoint_repository() -> str:
    return f"{PREFIX}/{ENDPOINT}"


# --- S3-event triggers (#83): aegc-<d>-trigger-<name> for the rule, function, secret ----------

#: Reserved: pools can't be named ``trigger`` or ``trigger-*``.
TRIGGER = "trigger"


def trigger_resource(deployment: str, name: str) -> str:
    """A trigger's EventBridge rule, Lambda function and Secrets Manager secret."""
    return f"{PREFIX}-{deployment}-{TRIGGER}-{name}"


def trigger_prefix(deployment: str) -> str:
    return f"{PREFIX}-{deployment}-{TRIGGER}-"


def trigger_dlq(deployment: str, name: str) -> str:
    return f"{trigger_resource(deployment, name)}-dlq"


def trigger_role(deployment: str, name: str, region: str) -> str:
    return f"{trigger_resource(deployment, name)}-{region}"


def trigger_parameter(deployment: str, name: str = "") -> str:
    """The trigger's spec (``/aegc/<d>/triggers/<name>``); without a name, their path."""
    return f"/{PREFIX}/{deployment}/triggers" + (f"/{name}" if name else "")
