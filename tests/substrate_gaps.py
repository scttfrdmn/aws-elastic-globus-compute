"""Workarounds for the places where substrate still differs from AWS.

Each one names the substrate issue that tracks it. When substrate fixes the issue, delete the
workaround and its uses, so the test exercises substrate directly.

Gaps without code here (substrate v0.129.0 fixed #1523 and #1525):
- substrate#1524: no ECR lifecycle policy preview (a strict xfail in test_images.py).
- substrate#1595: GetMetricData returns no MetricDataResults for metrics put with
  PutMetricData, and ListMetrics drops their dimensions; test_observability_ops.py stubs
  CloudWatch for ``aegc metrics``.
- substrate#1596: FilterLogEvents ignores JSON filter patterns (``{ $.task_uuid = "..." }``
  matches nothing); ``aegc provenance`` uses a quoted-term pattern, which works on both.
"""

from __future__ import annotations

import fnmatch
import re
from types import SimpleNamespace
from typing import Any


def no_network_interfaces(ec2: Any) -> Any:
    """substrate#1522: no DescribeNetworkInterfaces (InvalidAction).

    Answers it on this client with an empty list, which is what AWS returns once a deployment's
    instances have terminated, so destroy's verification can run. Every other call still goes to
    substrate. A strict xfail in test_deployment.py calls the real operation.
    """

    def empty(**_: Any) -> tuple[Any, dict[str, Any]]:
        return SimpleNamespace(status_code=200, headers={}), {"NetworkInterfaces": []}

    ec2.meta.events.register("before-call.ec2.DescribeNetworkInterfaces", empty)
    return ec2


def metrics_collection(autoscaling: Any) -> list[dict[str, Any]]:
    """substrate#1594: no EnableMetricsCollection (InvalidAction).

    Answers it on this client with success and returns a list that collects each call's request
    body (``AutoScalingGroupName``, ``Granularity``), so deploy's call can be checked. Without the
    shim, deploy reports the group metrics as ``kept`` (not enabled) and carries on, as it would
    with a deployer policy from before 0.4.
    """
    calls: list[dict[str, Any]] = []

    def ok(params: dict[str, Any], **_: Any) -> tuple[Any, dict[str, Any]]:
        body = params.get("body")
        calls.append(dict(body) if isinstance(body, dict) else {"body": body})
        return SimpleNamespace(status_code=200, headers={}), {}

    autoscaling.meta.events.register("before-call.auto-scaling.EnableMetricsCollection", ok)
    return calls


def instance_type_offerings(ec2: Any, offered: dict[str, list[str]]) -> list[dict[str, Any]]:
    """substrate#1621: DescribeInstanceTypeOfferings offers every type in every
    zone, so a zone without Graviton (us-east-1's use1-az3) can't be modelled. Subnets also lack
    AvailabilityZoneId.

    Answers it on this client from ``offered`` (zone name -> instance types), applying the
    ``instance-type`` filter's globs as EC2 does, for LocationType=availability-zone. Returns a
    list that collects each call's request body, so callers can check the caching.
    """
    calls: list[dict[str, Any]] = []

    def answer(params: dict[str, Any], **_: Any) -> tuple[Any, dict[str, Any]]:
        body = dict(params.get("body") or {})
        calls.append(body)
        patterns: list[str] = []
        for key, name in body.items():
            m = re.fullmatch(r"Filter\.(\d+)\.Name", key)
            if m and name == "instance-type":
                n = m.group(1)
                patterns += [v for k, v in body.items() if k.startswith(f"Filter.{n}.Value.")]
        found = [
            {"InstanceType": t, "LocationType": "availability-zone", "Location": zone}
            for zone, types in offered.items()
            for t in types
            if not patterns or any(fnmatch.fnmatchcase(t, p) for p in patterns)
        ]
        return SimpleNamespace(status_code=200, headers={}), {"InstanceTypeOfferings": found}

    ec2.meta.events.register("before-call.ec2.DescribeInstanceTypeOfferings", answer)
    return calls


def secrets_with_tags(sm: Any) -> Any:
    """substrate#1622: ListSecrets omits Tags. Fills them in from DescribeSecret on this
    client, so ownership checks see what AWS returns."""

    def add_tags(parsed: Any, **_: Any) -> None:
        for s in parsed.get("SecretList", []):
            if "Tags" not in s:
                s["Tags"] = sm.describe_secret(SecretId=s["ARN"]).get("Tags", [])

    sm.meta.events.register("after-call.secrets-manager.ListSecrets", add_tags)
    return sm


def _stub(client: Any, service: str, op: str, respond: Any) -> None:
    """Answer ``op`` on this client with ``respond(api_params)`` instead of calling substrate."""
    last: dict[str, Any] = {}

    def capture(params: dict[str, Any], **_: Any) -> None:
        last.clear()
        last.update(params)

    def answer(**_: Any) -> tuple[Any, dict[str, Any]]:
        return SimpleNamespace(status_code=200, headers={}), respond(dict(last))

    client.meta.events.register(f"before-parameter-build.{service}.{op}", capture)
    client.meta.events.register(f"before-call.{service}.{op}", answer)


def secret_resource_policies(sm: Any) -> dict[str, str]:
    """substrate#1623: no PutResourcePolicy/GetResourcePolicy. Records them on this client;
    returns the policies by secret id."""
    policies: dict[str, str] = {}

    def put(p: dict[str, Any]) -> dict[str, Any]:
        policies[p["SecretId"]] = p["ResourcePolicy"]
        return {"Name": p["SecretId"]}

    def get(p: dict[str, Any]) -> dict[str, Any]:
        sid = p["SecretId"]
        return {"Name": sid, **({"ResourcePolicy": policies[sid]} if sid in policies else {})}

    _stub(sm, "secrets-manager", "PutResourcePolicy", put)
    _stub(sm, "secrets-manager", "GetResourcePolicy", get)
    return policies


def instance_refreshes(autoscaling: Any) -> list[dict[str, Any]]:
    """substrate#1624: no StartInstanceRefresh. Records the requests on this client."""
    calls: list[dict[str, Any]] = []

    def start(p: dict[str, Any]) -> dict[str, Any]:
        calls.append(p)
        return {"InstanceRefreshId": "r-1"}

    _stub(autoscaling, "auto-scaling", "StartInstanceRefresh", start)
    return calls
