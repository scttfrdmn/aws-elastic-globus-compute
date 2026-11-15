"""Workarounds for the places where substrate still differs from AWS.

Each one names the substrate issue that tracks it. When substrate fixes the issue, delete the
workaround and its uses, so the test exercises substrate directly.

Gaps without code here (substrate v0.127.0 fixed #1469, #1471, #1497, #1498, #1501, #1502):
- substrate#1523: GetResources omits EC2 security groups, launch templates and volumes, and IAM
  instance profiles. aegc footprint's name scan still finds the launch templates (a strict xfail
  in test_ops.py).
- substrate#1524: no ECR lifecycle policy preview (a strict xfail in test_images.py).
- substrate#1525: DescribeInstances omits Architecture (tests check the instance type instead).
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
