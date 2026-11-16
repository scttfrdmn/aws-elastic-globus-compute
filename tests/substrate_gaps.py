"""Workarounds for the places where substrate still differs from AWS.

Each one names the substrate issue that tracks it. When substrate fixes the issue, delete the
workaround and its uses, so the test exercises substrate directly.

Gaps without code here (substrate v0.129.0 fixed #1523 and #1525):
- substrate#1524: no ECR lifecycle policy preview (a strict xfail in test_images.py).
- (issue to be filed) GetMetricData returns no MetricDataResults for metrics put with
  PutMetricData, and ListMetrics drops their dimensions; test_observability_ops.py stubs
  CloudWatch for ``aegc metrics``.
- (issue to be filed) FilterLogEvents ignores JSON filter patterns (``{ $.task_uuid = "..." }``
  matches nothing); ``aegc provenance`` uses a quoted-term pattern, which works on both.
"""

from __future__ import annotations

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
    """substrate (issue to be filed): no EnableMetricsCollection (InvalidAction).

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
