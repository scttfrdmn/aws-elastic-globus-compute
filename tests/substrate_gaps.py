"""Workarounds for the places where substrate still differs from AWS.

Each one names the substrate issue that tracks it. When substrate fixes the issue, delete the
workaround and its uses, so the test exercises substrate directly.

Gaps without code here:
- substrate#1469: DescribeLaunchTemplates with an unknown name returns [] instead of raising
  InvalidLaunchTemplateName.NotFoundException. src handles both.
- substrate#1471: launch templates drop MetadataOptions (a strict xfail in test_deployment.py).
- Not filed yet: the Resource Groups Tagging API (GetResources) omits EC2 security groups, launch
  templates and volumes, and IAM instance profiles. aegc footprint's name scan still finds the
  launch templates (a strict xfail in test_ops.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def launch_on_substrate(autoscaling: Any, group_name: str) -> None:
    """substrate#1497: no Graviton types in the catalog, so aegc's groups launch nothing.

    Retargets the group's attribute-based selection at a family substrate carries: Intel, m5.
    Everything else in the group (Spot, the launch template, subnets, protection, tags) stays as
    aegc made it. Call it after ``deploy`` and before asking for capacity. A later ``deploy``
    restores aegc's requirements without touching instances that are already running.
    """
    g = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group_name])[
        "AutoScalingGroups"
    ][0]
    mixed = g["MixedInstancesPolicy"]
    for o in mixed["LaunchTemplate"]["Overrides"]:
        req = o["InstanceRequirements"]
        req["CpuManufacturers"] = ["intel"]
        req["AllowedInstanceTypes"] = ["m5.*"]
    autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=group_name, MixedInstancesPolicy=mixed
    )


def no_network_interfaces(ec2: Any) -> Any:
    """Not filed yet: substrate has no DescribeNetworkInterfaces (InvalidAction).

    Answers it on this client with an empty list, which is what AWS returns once a deployment's
    instances have terminated, so destroy's verification can run. Every other call still goes to
    substrate. A strict xfail in test_deployment.py calls the real operation.
    """

    def empty(**_: Any) -> tuple[Any, dict[str, Any]]:
        return SimpleNamespace(status_code=200, headers={}), {"NetworkInterfaces": []}

    ec2.meta.events.register("before-call.ec2.DescribeNetworkInterfaces", empty)
    return ec2
