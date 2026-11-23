"""AWS Fault Injection Service (FIS) for the Spot sweep tutorial: interrupt a pool's Spot workers.

    python examples/spot-sweep/fis.py setup   --deployment sweep --pool plain-py314
    python examples/spot-sweep/fis.py start   --deployment sweep --pool plain-py314 --wait
    python examples/spot-sweep/fis.py cleanup --deployment sweep          # dry run
    python examples/spot-sweep/fis.py cleanup --deployment sweep --yes

`setup` creates an IAM role that only FIS, acting for this account, can assume, with the AWS
managed policy AWSFaultInjectionSimulatorEC2Access, and an experiment template that sends Spot
interruption notices (two minutes' warning) to a share of the running Spot instances tagged
with the deployment and pool. `start` runs it. Both are tagged `created-by=aegc-tutorial-spot-sweep`
with the deployment, and `cleanup` deletes only what carries those tags. Plain boto3, no
CloudFormation. FIS charges $0.10 per action-minute.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

CREATED_BY = "aegc-tutorial-spot-sweep"
MANAGED_POLICY = "arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorEC2Access"
ROLE_PATH = "/aegc-tutorial/"
# aegc's own tag keys on worker instances (src/aegc/naming.py: TAG_DEPLOYMENT, TAG_POOL).
AEGC_DEPLOYMENT = "aegc:deployment"
AEGC_POOL = "aegc:pool"


def role_name(deployment: str, region: str) -> str:
    return f"aegc-tutorial-fis-{deployment}-{region}"


def our_tags(deployment: str, pool: str = "") -> dict[str, str]:
    tags = {"created-by": CREATED_BY, "deployment": deployment}
    if pool:
        tags["pool"] = pool
    return tags


def owned(tags: dict[str, str], deployment: str) -> bool:
    """Whether a role or template carries this script's tags for `deployment`."""
    return tags.get("created-by") == CREATED_BY and tags.get("deployment") == deployment


def trust_policy(account: str) -> dict[str, Any]:
    """Only FIS, acting for this account, may assume the role (confused-deputy guard)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "fis.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }


def template_request(
    *,
    deployment: str,
    pool: str,
    role_arn: str,
    percent: int = 50,
    before: str = "PT2M",
    client_token: str = "",
) -> dict[str, Any]:
    """The CreateExperimentTemplate request: interrupt `percent`% of the pool's running Spot
    workers, `before` (an ISO 8601 duration, PT2M to PT15M) after the notice."""
    if not 1 <= percent <= 100:
        raise ValueError(f"percent must be 1 to 100, not {percent}")
    return {
        "clientToken": client_token or str(uuid.uuid4()),
        "description": f"aegc tutorial: interrupt {percent}% of {deployment}/{pool} Spot workers",
        "roleArn": role_arn,
        "stopConditions": [{"source": "none"}],
        "targets": {
            "SpotWorkers": {
                "resourceType": "aws:ec2:spot-instance",
                "resourceTags": {AEGC_DEPLOYMENT: deployment, AEGC_POOL: pool},
                "filters": [{"path": "State.Name", "values": ["running"]}],
                "selectionMode": f"PERCENT({percent})",
            }
        },
        "actions": {
            "interrupt": {
                "actionId": "aws:ec2:send-spot-instance-interruptions",
                "description": "Spot interruption notice, then the interruption",
                "parameters": {"durationBeforeInterruption": before},
                "targets": {"SpotInstances": "SpotWorkers"},
            }
        },
        "tags": our_tags(deployment, pool),
    }


def _session(profile: str | None, region: str | None) -> Any:
    import boto3

    sess = boto3.Session(profile_name=profile, region_name=region)
    if not sess.region_name:
        raise SystemExit("fis.py: no region; pass --region or configure one")
    return sess


def _templates(fis: Any, deployment: str, pool: str = "") -> list[dict[str, Any]]:
    found, token = [], None
    while True:
        page = fis.list_experiment_templates(**({"nextToken": token} if token else {}))
        for t in page.get("experimentTemplates", []):
            tags = t.get("tags", {})
            if owned(tags, deployment) and (not pool or tags.get("pool") == pool):
                found.append(t)
        token = page.get("nextToken")
        if not token:
            return found


def ensure_role(iam: Any, deployment: str, region: str, account: str) -> str:
    name = role_name(deployment, region)
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            Path=ROLE_PATH,
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_policy(account)),
            Description=f"FIS for the aegc Spot sweep tutorial (deployment {deployment})",
            Tags=[{"Key": k, "Value": v} for k, v in our_tags(deployment).items()],
        )["Role"]
        print(f"created  iam role {name}")
    else:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=name)["Tags"]}
        if not owned(tags, deployment):
            raise SystemExit(f"fis.py: role {name} exists but this script didn't create it")
        print(f"exists   iam role {name}")
    iam.attach_role_policy(RoleName=name, PolicyArn=MANAGED_POLICY)  # idempotent
    return str(role["Arn"])


def setup(sess: Any, deployment: str, pool: str, percent: int) -> str:
    fis = sess.client("fis")
    for t in _templates(fis, deployment, pool):
        print(f"exists   fis experiment template {t['id']}")
        return str(t["id"])
    account = sess.client("sts").get_caller_identity()["Account"]
    arn = ensure_role(sess.client("iam"), deployment, sess.region_name, account)
    req = template_request(deployment=deployment, pool=pool, role_arn=arn, percent=percent)
    for _ in range(12):  # a new role takes a few seconds to become assumable
        try:
            tid = fis.create_experiment_template(**req)["experimentTemplate"]["id"]
            break
        except fis.exceptions.ValidationException as e:
            if "role" not in str(e).lower():
                raise
            time.sleep(5)
    else:
        raise SystemExit("fis.py: FIS still can't use the new role; run setup again in a minute")
    print(f"created  fis experiment template {tid}")
    return str(tid)


def start(sess: Any, deployment: str, pool: str, *, wait: bool = False) -> str:
    """Start the pool's experiment; with `wait`, poll until it ends and print its state."""
    fis = sess.client("fis")
    templates = _templates(fis, deployment, pool)
    if not templates:
        raise SystemExit(f"fis.py: no template for {deployment}/{pool}; run setup first")
    exp = fis.start_experiment(
        clientToken=str(uuid.uuid4()),
        experimentTemplateId=templates[0]["id"],
        tags=our_tags(deployment, pool),
    )["experiment"]
    print(f"started  fis experiment {exp['id']} at {time.strftime('%H:%M:%S')}", flush=True)
    if wait:
        wait_for(sess, exp["id"])
    return str(exp["id"])


def wait_for(sess: Any, experiment_id: str, poll_s: float = 10) -> dict[str, Any]:
    fis = sess.client("fis")
    while True:
        exp = fis.get_experiment(id=experiment_id)["experiment"]
        state = exp["state"]
        if state["status"] in ("completed", "stopped", "failed", "cancelled"):
            took = (exp.get("endTime") or exp["startTime"]) - exp["startTime"]
            targets = fis.list_experiment_resolved_targets(experimentId=experiment_id)
            info = [t["targetInformation"] for t in targets["resolvedTargets"]]
            # FIS gives each target's ARN (arn:aws:ec2:...:instance/i-...).
            ids = [str(i.get("resource-id") or i.get("arn", "?")).rsplit("/", 1)[-1] for i in info]
            print(
                f"{state['status']}: fis experiment {experiment_id} in {took.total_seconds():.0f} s"
                f" ({state.get('reason', '')}); interrupted {len(ids)}: {', '.join(ids)}"
            )
            return dict(exp)
        time.sleep(poll_s)


def cleanup(sess: Any, deployment: str, *, yes: bool) -> None:
    fis, iam = sess.client("fis"), sess.client("iam")
    verb = "deleted " if yes else "would-delete"
    for t in _templates(fis, deployment):
        if yes:
            fis.delete_experiment_template(id=t["id"])
        print(f"{verb} fis experiment template {t['id']}")
    name = role_name(deployment, sess.region_name)
    try:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=name)["Tags"]}
    except iam.exceptions.NoSuchEntityException:
        tags = None
    if tags is not None and not owned(tags, deployment):
        print(f"kept     iam role {name} (not tagged created-by={CREATED_BY})")
    elif tags is not None:
        if yes:
            iam.detach_role_policy(RoleName=name, PolicyArn=MANAGED_POLICY)
            iam.delete_role(RoleName=name)
        print(f"{verb} iam role {name}")
    if not yes:
        print("dry run; pass --yes to delete")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile")
    p.add_argument("--region")
    p.add_argument("command", choices=["setup", "start", "cleanup"])
    p.add_argument("--deployment", required=True)
    p.add_argument("--pool", help="the pool, e.g. plain-py314 (setup, start)")
    p.add_argument("--percent", type=int, default=50, help="share of running workers (setup)")
    p.add_argument("--wait", action="store_true", help="start: wait for the experiment to end")
    p.add_argument("--yes", action="store_true", help="cleanup: actually delete")
    args = p.parse_args(argv)
    sess = _session(args.profile, args.region)
    if args.command == "cleanup":
        cleanup(sess, args.deployment, yes=args.yes)
        return 0
    if not args.pool:
        p.error(f"{args.command} needs --pool")
    if args.command == "setup":
        setup(sess, args.deployment, args.pool, args.percent)
    else:
        start(sess, args.deployment, args.pool, wait=args.wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
