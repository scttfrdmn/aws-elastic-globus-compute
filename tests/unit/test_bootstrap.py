import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import boto3
import pytest

from aegc import naming
from aegc.infra import OwnershipError, bootstrap
from tests.conftest import ACCOUNT, REGION

BUCKET = naming.bucket_name(ACCOUNT, REGION)
ROLE = naming.ecr_template_role(REGION)
PREFIX = naming.pull_through_prefix()


def clients(aws: Callable[[str], Any]) -> dict[str, Any]:
    return {
        "s3": aws("s3"),
        "ecr": aws("ecr"),
        "ssm": aws("ssm"),
        "iam": aws("iam"),
        "account": ACCOUNT,
        "region": REGION,
    }


def outcomes(report: bootstrap.Report) -> dict[tuple[str, str], str]:
    return {(a.resource, a.name): a.outcome for a in report.actions}


def pull_through_rules(ecr: Any) -> dict[str, str]:
    try:
        rules = ecr.describe_pull_through_cache_rules(ecrRepositoryPrefixes=[PREFIX])
    except ecr.exceptions.PullThroughCacheRuleNotFoundException:
        return {}
    return {
        r["ecrRepositoryPrefix"]: r["upstreamRegistryUrl"] for r in rules["pullThroughCacheRules"]
    }


def creation_template(ecr: Any) -> dict[str, Any] | None:
    try:
        found = ecr.describe_repository_creation_templates(prefixes=[PREFIX])
    except ecr.exceptions.TemplateNotFoundException:
        return None
    templates = found["repositoryCreationTemplates"]
    return dict(templates[0]) if templates else None


def test_bootstrap_creates_then_is_idempotent(aws: Callable[[str], Any]) -> None:
    c = clients(aws)

    first = outcomes(bootstrap.bootstrap(**c))
    assert first[("s3 bucket", BUCKET)] == "created"
    assert first[("ecr pull-through rule", "aegc-quay")] == "created"
    assert first[("ecr creation template", "aegc-quay")] == "created"
    assert first[("ssm parameter", "/aegc/bootstrap")] == "created"
    assert first[("iam role", ROLE)] == "created"

    second = outcomes(bootstrap.bootstrap(**c))
    assert set(second.values()) == {"exists"}

    s3 = aws("s3")
    tags = naming.tag_dict(s3.get_bucket_tagging(Bucket=BUCKET)["TagSet"])
    assert naming.is_owned(BUCKET, tags, naming.BOOTSTRAP)
    rules = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    by_id = {r["ID"]: r for r in rules}
    assert by_id["aegc-expire-payloads"]["Expiration"]["Days"] == bootstrap.PAYLOAD_EXPIRY_DAYS
    assert by_id["aegc-expire-ledger"]["Filter"]["Prefix"] == "ledger/"
    assert by_id["aegc-expire-ledger"]["Expiration"]["Days"] == bootstrap.LEDGER_EXPIRY_DAYS
    assert s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"][
        "RestrictPublicBuckets"
    ]
    assert "aws:SecureTransport" in s3.get_bucket_policy(Bucket=BUCKET)["Policy"]
    ecr = aws("ecr")
    assert pull_through_rules(ecr) == {PREFIX: "quay.io"}
    template = creation_template(ecr)
    assert template is not None
    assert template["appliedFor"] == ["PULL_THROUGH_CACHE"]
    assert naming.is_owned(PREFIX, naming.tag_dict(template["resourceTags"]), naming.BOOTSTRAP)

    # The template uses aegc's own role; ECR's default role can't tag (spike #5).
    iam = aws("iam")
    role = iam.get_role(RoleName=ROLE)["Role"]
    assert template["customRoleArn"] == role["Arn"]
    assert role["Path"] == naming.IAM_PATH
    assert naming.is_owned(
        ROLE, naming.tag_dict(iam.list_role_tags(RoleName=ROLE)["Tags"]), naming.BOOTSTRAP
    )
    perms = iam.get_role_policy(RoleName=ROLE, PolicyName=bootstrap.TEMPLATE_POLICY)[
        "PolicyDocument"
    ]
    stmt = (json.loads(perms) if isinstance(perms, str) else perms)["Statement"][0]
    assert sorted(stmt["Action"]) == ["ecr:CreateRepository", "ecr:TagResource"]
    assert stmt["Resource"].endswith(":repository/aegc-quay/*")


def test_template_without_custom_role_is_corrected(aws: Callable[[str], Any]) -> None:
    ecr = aws("ecr")
    # A template made before aegc used its own role: ECR's default role can't tag (spike #5).
    ecr.create_repository_creation_template(
        prefix=PREFIX, appliedFor=["PULL_THROUGH_CACHE"], imageTagMutability="MUTABLE"
    )
    template = creation_template(ecr)
    assert template is not None and not template.get("customRoleArn")
    report = outcomes(bootstrap.bootstrap(**clients(aws)))
    assert report[("ecr creation template", PREFIX)] == "updated"
    template = creation_template(ecr)
    assert template is not None
    assert template["customRoleArn"].endswith(f"/aegc/{ROLE}")


def test_refuses_untagged_template_role(aws: Callable[[str], Any]) -> None:
    aws("iam").create_role(RoleName=ROLE, Path="/aegc/", AssumeRolePolicyDocument="{}")
    with pytest.raises(OwnershipError, match=ROLE):
        bootstrap.bootstrap(**clients(aws))


def test_dry_run_creates_nothing(aws: Callable[[str], Any]) -> None:
    report = outcomes(bootstrap.bootstrap(**clients(aws), dry_run=True))
    assert set(report.values()) == {"would-create"}
    assert BUCKET not in [b["Name"] for b in aws("s3").list_buckets()["Buckets"]]
    ecr = aws("ecr")
    assert pull_through_rules(ecr) == {} and creation_template(ecr) is None


def created_buckets(s3: Any) -> list[dict[str, Any]]:
    """The parameters of each CreateBucket call on this client."""
    calls: list[dict[str, Any]] = []
    s3.meta.events.register(
        "provide-client-params.s3.CreateBucket", lambda params, **_: calls.append(dict(params))
    )
    return calls


def east_clients(substrate: Any) -> dict[str, Any]:
    del substrate  # requested for its side effects (reset + env)
    made = {
        s: boto3.client(s, region_name="us-east-1")  # type: ignore[call-overload]
        for s in ("s3", "ecr", "ssm", "iam")
    }
    return {**made, "account": ACCOUNT, "region": "us-east-1"}


def test_bucket_in_other_regions_is_created_tagged(aws: Callable[[str], Any]) -> None:
    c = clients(aws)
    calls = created_buckets(c["s3"])
    bootstrap.bootstrap(**c)
    (call,) = calls
    cfg = call["CreateBucketConfiguration"]
    assert cfg["LocationConstraint"] == REGION
    assert naming.is_owned(BUCKET, naming.tag_dict(cfg["Tags"]), naming.BOOTSTRAP)


def test_bootstrap_in_us_east_1(substrate: Any) -> None:
    # us-east-1 takes no LocationConstraint: CreateBucket goes without a configuration, and the
    # bucket is tagged straight after.
    c = east_clients(substrate)
    calls = created_buckets(c["s3"])
    first = outcomes(bootstrap.bootstrap(**c))
    bucket = naming.bucket_name(ACCOUNT, "us-east-1")
    assert bucket == f"aegc-{ACCOUNT}-us-east-1"
    assert first[("s3 bucket", bucket)] == "created"
    (call,) = calls
    assert "CreateBucketConfiguration" not in call
    tags = naming.tag_dict(c["s3"].get_bucket_tagging(Bucket=bucket)["TagSet"])
    assert naming.is_owned(bucket, tags, naming.BOOTSTRAP)
    assert first[("iam role", naming.ecr_template_role("us-east-1"))] == "created"
    second = outcomes(bootstrap.bootstrap(**c))
    assert set(second.values()) == {"exists"}
    assert len(calls) == 1


def test_us_east_1_bucket_is_removed_if_tagging_fails(substrate: Any) -> None:
    c = east_clients(substrate)

    def denied(**_: Any) -> tuple[Any, dict[str, Any]]:
        error = {"Code": "AccessDenied", "Message": "no s3:PutBucketTagging"}
        return SimpleNamespace(status_code=403, headers={}), {"Error": error}

    c["s3"].meta.events.register("before-call.s3.PutBucketTagging", denied)
    with pytest.raises(Exception, match="AccessDenied"):
        bootstrap.bootstrap(**c)
    bucket = naming.bucket_name(ACCOUNT, "us-east-1")
    # Not left behind untagged, where the next run would refuse it as not aegc's.
    assert bucket not in [b["Name"] for b in c["s3"].list_buckets()["Buckets"]]


def test_refuses_untagged_bucket_with_our_name(aws: Callable[[str], Any]) -> None:
    aws("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    with pytest.raises(OwnershipError):
        bootstrap.bootstrap(**clients(aws))


def test_refuses_foreign_pull_through_rule(aws: Callable[[str], Any]) -> None:
    aws("ecr").create_pull_through_cache_rule(
        ecrRepositoryPrefix=PREFIX, upstreamRegistryUrl="public.ecr.aws"
    )
    with pytest.raises(OwnershipError, match="refusing"):
        bootstrap.bootstrap(**clients(aws))
    assert pull_through_rules(aws("ecr")) == {PREFIX: "public.ecr.aws"}


def test_unbootstrap_refuses_while_deployments_exist(aws: Callable[[str], Any]) -> None:
    c = clients(aws)
    bootstrap.bootstrap(**c)
    aws("ssm").put_parameter(Name="/aegc/dev/manifest", Value="{}", Type="String")
    with pytest.raises(RuntimeError, match="dev"):
        bootstrap.unbootstrap(**c, dry_run=False)


def test_unbootstrap_dry_run_then_delete(aws: Callable[[str], Any]) -> None:
    c = clients(aws)
    bootstrap.bootstrap(**c)
    aws("s3").put_object(Bucket=BUCKET, Key="payloads/dev/tasks/x/1.task", Body=b"x")

    dry = outcomes(bootstrap.unbootstrap(**c))
    assert dry[("s3 bucket", BUCKET)] == "would-delete"
    assert BUCKET in [b["Name"] for b in aws("s3").list_buckets()["Buckets"]]

    done = outcomes(bootstrap.unbootstrap(**c, dry_run=False))
    assert done[("s3 bucket", BUCKET)] == "deleted"
    assert done[("ecr pull-through rule", "aegc-quay")] == "deleted"
    assert BUCKET not in [b["Name"] for b in aws("s3").list_buckets()["Buckets"]]
    assert done[("ecr creation template", PREFIX)] == "deleted"
    ecr = aws("ecr")
    assert pull_through_rules(ecr) == {} and creation_template(ecr) is None
    assert done[("iam role", ROLE)] == "deleted"
    with pytest.raises(aws("iam").exceptions.NoSuchEntityException):
        aws("iam").get_role(RoleName=ROLE)
    with pytest.raises(aws("ssm").exceptions.ParameterNotFound):
        aws("ssm").get_parameter(Name="/aegc/bootstrap")


def test_unbootstrap_keeps_foreign_cache_repositories(aws: Callable[[str], Any]) -> None:
    c = clients(aws)
    bootstrap.bootstrap(**c)
    ecr = aws("ecr")
    ecr.create_repository(
        repositoryName="aegc-quay/aarchsci/geospatial",
        tags=naming.tag_list(naming.tags(naming.BOOTSTRAP, "cache-repo")),
    )
    ecr.create_repository(repositoryName="aegc-quay/someone-elses")

    done = outcomes(bootstrap.unbootstrap(**c, dry_run=False))
    assert done[("ecr repository", "aegc-quay/aarchsci/geospatial")] == "deleted"
    assert done[("ecr repository", "aegc-quay/someone-elses")] == "kept"
    names = [r["repositoryName"] for r in ecr.describe_repositories()["repositories"]]
    assert names == ["aegc-quay/someone-elses"]
