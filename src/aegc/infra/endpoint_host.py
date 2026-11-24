"""The cloud-hosted endpoint (#22): one small on-demand Graviton instance per deployment.

An Auto Scaling group of exactly one replaces the instance if it dies. Its user data restores the
user's Globus login from Secrets Manager (:mod:`aegc.infra.secrets`), configures the endpoint
like ``aegc endpoint configure`` does, and runs it under systemd, plus ``aegc evaluate`` every 5
minutes (liveness metric, spend caps, lease, idle result queues). A CloudWatch alarm fires when
the endpoint is offline or the metric stops arriving (#60).

Everything is named ``aegc-<d>-endpoint*`` and tagged ``aegc:role=endpoint*``, so worker and
operator policies (which only touch groups tagged ``aegc:role=asg``) can't touch it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aegc import naming
from aegc.awsutil import is_not_found
from aegc.infra import Outcome, OwnershipError, Report, iam, userdata
from aegc.infra.deployment import (
    Clients,
    DeploymentSpec,
    _put_role_policy,
    _retry,
    ensure_launch_template,
    ensure_log_group,
    ensure_security_group,
    resolve_ami,
    resolve_network,
)

DEFAULT_INSTANCE_TYPE = "t4g.micro"


class _NoTemplates(Exception):
    pass


ROOT_GIB = 30  # the ECS-optimized AMI's snapshot size


@dataclass(frozen=True)
class EndpointHostSpec:
    deployment: str
    account: str
    region: str
    name: str  # the Globus Compute endpoint name
    endpoint_id: str
    image: str  # <registry>/aegc/endpoint@sha256:...
    configure_script: str
    instance_type: str = DEFAULT_INSTANCE_TYPE
    alarm_email: str = ""
    subnet_ids: tuple[str, ...] = ()
    metric_token: str = ""  # the liveness metric's Token dimension (kept in the secret)
    session_manager: bool = False
    multi_user: bool = False  # a root core endpoint with identity mapping (#62)

    @property
    def deployment_spec(self) -> DeploymentSpec:
        """For the shared helpers (security group, log group, network, launch template)."""
        return DeploymentSpec(
            deployment=self.deployment, account=self.account, region=self.region,
            bucket=naming.bucket_name(self.account, self.region), pools=(),
            subnet_ids=self.subnet_ids,
        )  # fmt: skip


def ensure_role(c: Clients, spec: EndpointHostSpec, report: Report, *, dry_run: bool) -> str:
    d, name = spec.deployment, naming.endpoint_role(spec.deployment, spec.region)
    tags = naming.tag_list(naming.tags(d, "endpoint-role"))
    try:
        found = naming.tag_dict(c.iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as {d}'s")
        report.add("iam role", name, "exists")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        report.add("iam role", name, "would-create" if dry_run else "created")
        if not dry_run:
            c.iam.create_role(
                Path=naming.IAM_PATH, RoleName=name,
                AssumeRolePolicyDocument=json.dumps(iam.ec2_trust_policy()),
                Description=f"aegc cloud endpoint for deployment {d}", Tags=tags,
            )  # fmt: skip
    policy = iam.endpoint_role_policy(
        deployment=d, region=spec.region, account=spec.account,
        bucket=naming.bucket_name(spec.account, spec.region),
        session_manager=spec.session_manager,
    )  # fmt: skip
    _put_role_policy(c, name, iam.ENDPOINT_POLICY, policy, report, dry_run=dry_run)
    arn = f"arn:aws:iam::{spec.account}:instance-profile{naming.IAM_PATH}{name}"
    if dry_run:
        return arn
    try:
        prof = c.iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
        found = naming.tag_dict(c.iam.list_instance_profile_tags(InstanceProfileName=name)["Tags"])
        if not naming.is_owned(name, found, d):
            raise OwnershipError(f"instance profile {name} exists but isn't tagged as {d}'s")
        report.add("iam instance profile", name, "exists")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        prof = c.iam.create_instance_profile(
            Path=naming.IAM_PATH, InstanceProfileName=name, Tags=tags
        )["InstanceProfile"]
        report.add("iam instance profile", name, "created")
    if name not in [r["RoleName"] for r in prof.get("Roles", [])]:
        c.iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    return str(prof.get("Arn", arn))


def launch_template_data(
    spec: EndpointHostSpec, *, ami: str, sg: str, profile_arn: str
) -> dict[str, Any]:
    d = spec.deployment
    boot = userdata.EndpointBoot(
        deployment=d, name=spec.name, endpoint_id=spec.endpoint_id, image=spec.image,
        secret=naming.endpoint_secret(d), log_group=naming.log_group(d),
        configure_script=spec.configure_script, multi_user=spec.multi_user,
    )  # fmt: skip
    tags = naming.tag_list(naming.tags(d, "endpoint"))
    return {
        "ImageId": ami,
        "InstanceType": spec.instance_type,
        "IamInstanceProfile": {"Arn": profile_arn},
        "SecurityGroupIds": [sg],
        "UserData": userdata.encode(userdata.render_endpoint(boot)),
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 2,  # the endpoint container is on docker's bridge
            "HttpEndpoint": "enabled",
            "HttpProtocolIpv6": "disabled",
        },
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": ROOT_GIB,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                    "Encrypted": True,
                },
            },
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
    }


def ensure_group(
    c: Clients, spec: EndpointHostSpec, *, lt_name: str, subnets: list[str], report: Report,
    dry_run: bool, roll: bool = False,
) -> str:  # fmt: skip
    d, name = spec.deployment, naming.endpoint_group(spec.deployment)
    req = {
        "MinSize": 1,
        "MaxSize": 1,
        "VPCZoneIdentifier": ",".join(subnets),
        "HealthCheckType": "EC2",
        "LaunchTemplate": {"LaunchTemplateName": lt_name, "Version": "$Default"},
    }
    tags = [
        {"Key": k, "Value": v, "PropagateAtLaunch": False, "ResourceId": name,
         "ResourceType": "auto-scaling-group"}
        for k, v in naming.tags(d, "endpoint-asg").items()
    ]  # fmt: skip
    groups = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
        "AutoScalingGroups"
    ]
    if not groups:
        if dry_run:
            report.add("auto scaling group", name, "would-create")
            return name
        # A just-created instance profile can take a few seconds to be usable.
        _retry(
            lambda: c.autoscaling.create_auto_scaling_group(
                AutoScalingGroupName=name, DesiredCapacity=1, Tags=tags, **req
            ),
            codes={"ValidationError"}, attempts=10, delay_s=5,
        )  # fmt: skip
        c.autoscaling.suspend_processes(AutoScalingGroupName=name, ScalingProcesses=["AZRebalance"])
        report.add("auto scaling group", name, "created", spec.instance_type)
        return name
    if not naming.is_owned(name, naming.tag_dict(groups[0].get("Tags", [])), d):
        raise OwnershipError(f"auto scaling group {name} exists but isn't tagged as {d}'s")
    outcome: Outcome = "exists"
    if not dry_run:
        c.autoscaling.update_auto_scaling_group(AutoScalingGroupName=name, DesiredCapacity=1, **req)
        # A second host on the same endpoint id would fight the first: never two at once.
        c.autoscaling.suspend_processes(AutoScalingGroupName=name, ScalingProcesses=["AZRebalance"])
    report.add("auto scaling group", name, outcome, spec.instance_type)
    if roll:
        # The host only reads its template, image and login at boot: replace it, terminating the
        # old one first (two hosts can't share one endpoint id).
        if not dry_run:
            c.autoscaling.start_instance_refresh(
                AutoScalingGroupName=name,
                Strategy="Rolling",
                Preferences={"MinHealthyPercentage": 0, "InstanceWarmup": 60},
            )
        report.add("instance refresh", name, "would-update" if dry_run else "updated",
                   "replacing the host with the new configuration")  # fmt: skip
    return name


def ensure_alarm(c: Clients, spec: EndpointHostSpec, report: Report, *, dry_run: bool) -> None:
    """Alarm when the endpoint is offline, or ``aegc evaluate`` stops reporting, for 15 minutes."""
    from aegc import evaluator

    d, name = spec.deployment, naming.endpoint_alarm(spec.deployment)
    actions: list[str] = []
    existing = _topic_arn(c, d)
    if existing and not spec.alarm_email:
        actions = [existing]  # a redeploy without --alarm-email keeps the alarm's email
    if spec.alarm_email:
        topic = naming.endpoint_topic(d)
        if dry_run:
            report.add("sns topic", topic, "would-create", spec.alarm_email)
        else:
            arn = c.sns.create_topic(  # idempotent: returns the existing topic's ARN
                Name=topic, Tags=naming.tag_list(naming.tags(d, "endpoint-alarm"))
            )["TopicArn"]
            subs = c.sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
            if not any(s.get("Endpoint") == spec.alarm_email for s in subs):
                c.sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=spec.alarm_email)
                report.add("sns subscription", spec.alarm_email, "created",
                           "confirm it from the email AWS sends")  # fmt: skip
            actions = [arn]
    if dry_run:
        report.add("cloudwatch alarm", name, "would-update")
        return
    c.cloudwatch.put_metric_alarm(
        AlarmName=name,
        AlarmDescription=f"aegc: deployment {d}'s cloud endpoint is offline (or aegc evaluate "
        "stopped reporting)",
        Namespace=iam.ENDPOINT_NAMESPACE, MetricName="EndpointOnline",
        Dimensions=evaluator.liveness_dimensions(d, spec.metric_token),
        Statistic="Maximum", Period=300, EvaluationPeriods=3, DatapointsToAlarm=3,
        Threshold=1, ComparisonOperator="LessThanThreshold", TreatMissingData="breaching",
        AlarmActions=actions, OKActions=actions,
        Tags=naming.tag_list(naming.tags(d, "endpoint-alarm")),
    )  # fmt: skip
    # A new host reports after ~5 minutes; seed one datapoint so a fresh alarm doesn't start in
    # ALARM (missing data counts as breaching).
    evaluator.put_liveness(c.cloudwatch, d, spec.metric_token, True)
    report.add("cloudwatch alarm", name, "updated", "offline for 15 min (alerts in ~20-25 min)")


def _topics(c: Clients) -> list[str]:
    if c.sns is None:
        return []
    out: list[str] = []
    for page in c.sns.get_paginator("list_topics").paginate():
        out += [str(t["TopicArn"]) for t in page.get("Topics", [])]
    return out


def _topic_arn(c: Clients, d: str) -> str:
    for arn in _topics(c):
        if arn.rsplit(":", 1)[-1] == naming.endpoint_topic(d):
            return arn
    return ""


def deploy(
    c: Clients, spec: EndpointHostSpec, *, dry_run: bool = False, roll: bool = False
) -> Report:
    """Create or update the host. ``roll``: the login changed, so replace the running host."""
    from aegc import images
    from aegc.infra.deployment import OfferingCache, offered_zones, subnet_zones

    if not spec.metric_token:
        raise ValueError("the endpoint host needs a metric token (secrets.metric_token or new)")
    report = Report()
    ds = spec.deployment_spec
    profile = ensure_role(c, spec, report, dry_run=dry_run)
    vpc, subnets = resolve_network(c.ec2, ds)
    if not spec.subnet_ids:  # only zones that offer the host's instance type (#64)
        cache = OfferingCache()
        offered = offered_zones(c.ec2, (spec.instance_type,), cache)
        if offered:
            zones = subnet_zones(c.ec2, subnets, cache)
            subnets = [s for s in subnets if zones.get(s, ("", ""))[0] in offered] or subnets
    sg = ensure_security_group(c, ds, vpc, report, dry_run=dry_run)
    ensure_log_group(c, ds, report, dry_run=dry_run)
    if c.ecr is not None and not dry_run:  # keep the image past the repository's lifecycle
        tag = images.protect(c.ecr, "", spec.image, spec.deployment, report=report,
                             repository=naming.endpoint_repository())  # fmt: skip
        images.release(c.ecr, "", spec.deployment, {tag}, repository=naming.endpoint_repository(),
                       report=report)  # fmt: skip
    data = launch_template_data(spec, ami=resolve_ami(c.ssm), sg=sg, profile_arn=profile)
    before = len(report.actions)
    lt = ensure_launch_template(c, ds, None, data, report, dry_run=dry_run)
    lt_changed = any(
        a.resource == "launch template" and a.outcome == "updated" for a in report.actions[before:]
    )
    ensure_group(c, spec, lt_name=lt, subnets=subnets, report=report, dry_run=dry_run,
                 roll=roll or lt_changed)  # fmt: skip
    ensure_alarm(c, spec, report, dry_run=dry_run)
    return report


def undeploy(
    c: Clients, deployment: str, region: str, *, dry_run: bool = True, wait_s: float = 600,
    poll_s: float = 5, host: bool = True,
) -> Report:  # fmt: skip
    """Remove the endpoint host, its role, alarm and topic (the secret: :mod:`secrets`)."""
    from aegc.infra.deployment import _destroy_role, _wait_gone

    report = Report()
    d = deployment
    verb: Outcome = "would-delete" if dry_run else "deleted"
    group = naming.endpoint_group(d)
    groups = (
        c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group])[
            "AutoScalingGroups"
        ]
        if host
        else []  # destroy removes the group and template with the deployment's own
    )
    if groups:
        if not naming.is_owned(group, naming.tag_dict(groups[0].get("Tags", [])), d):
            report.add("auto scaling group", group, "kept", "not tagged as aegc's")
        else:
            if not dry_run:
                c.autoscaling.delete_auto_scaling_group(
                    AutoScalingGroupName=group, ForceDelete=True
                )
                _wait_gone(c, group, wait_s=wait_s, poll_s=poll_s)
            report.add("auto scaling group", group, verb)
    try:
        if not host:
            raise _NoTemplates
        lts = c.ec2.describe_launch_templates(LaunchTemplateNames=[naming.endpoint_group(d)])[
            "LaunchTemplates"
        ]
    except _NoTemplates:
        lts = []
    except Exception as e:
        if not is_not_found(e):
            raise
        lts = []
    for lt in lts:
        if naming.is_owned(lt["LaunchTemplateName"], naming.tag_dict(lt.get("Tags")), d):
            if not dry_run:
                c.ec2.delete_launch_template(LaunchTemplateName=lt["LaunchTemplateName"])
            report.add("launch template", lt["LaunchTemplateName"], verb)
    from aegc.awsutil import error_code
    from aegc.infra.deployment import ACCESS_DENIED

    alarm = naming.endpoint_alarm(d)
    try:  # the alarm and topic need the endpoint-deployer policy; the rest doesn't
        if c.cloudwatch is not None and c.cloudwatch.describe_alarms(AlarmNames=[alarm]).get(
            "MetricAlarms"
        ):
            if not dry_run:
                c.cloudwatch.delete_alarms(AlarmNames=[alarm])
            report.add("cloudwatch alarm", alarm, verb)
        arn = _topic_arn(c, d)
        if arn:
            tags = naming.tag_dict(c.sns.list_tags_for_resource(ResourceArn=arn).get("Tags"))
            if naming.is_owned(naming.endpoint_topic(d), tags, d):
                if not dry_run:
                    c.sns.delete_topic(TopicArn=arn)
                report.add("sns topic", naming.endpoint_topic(d), verb)
    except Exception as e:
        if error_code(e) not in ACCESS_DENIED:
            raise
        report.add("cloud endpoint alarm", alarm, "kept",
                   "not checked: needs the endpoint-deployer policy")  # fmt: skip
    if c.ecr is not None:
        from aegc import images

        try:
            images.release(c.ecr, "", d, repository=naming.endpoint_repository(), report=report,
                           dry_run=dry_run)  # fmt: skip
        except Exception as e:
            if not is_not_found(e):
                raise
    role = naming.endpoint_role(d, region)
    try:
        prof_tags = naming.tag_dict(
            c.iam.list_instance_profile_tags(InstanceProfileName=role)["Tags"]
        )
        if naming.is_owned(role, prof_tags, d):
            if not dry_run:
                prof = c.iam.get_instance_profile(InstanceProfileName=role)["InstanceProfile"]
                for r in prof.get("Roles", []):
                    c.iam.remove_role_from_instance_profile(
                        InstanceProfileName=role, RoleName=r["RoleName"]
                    )
                c.iam.delete_instance_profile(InstanceProfileName=role)
            report.add("iam instance profile", role, verb)
    except Exception as e:
        if not is_not_found(e):
            raise
    _destroy_role(c, d, role, report, dry_run=dry_run)
    return report
