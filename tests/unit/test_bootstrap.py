import json
from collections.abc import Callable
from typing import Any

import pytest

from aegc import naming
from aegc.infra import OwnershipError, bootstrap
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import FakeEcrPullThrough, s3_with_ownership_controls

BUCKET = naming.bucket_name(ACCOUNT, REGION)
ROLE = naming.ecr_template_role(REGION)


def clients(aws: Callable[[str], Any], fake_ecr: FakeEcrPullThrough) -> dict[str, Any]:
    return {
        "s3": s3_with_ownership_controls(aws("s3")),
        "ecr": fake_ecr.wrap(aws("ecr")),
        "ssm": aws("ssm"),
        "iam": aws("iam"),
        "account": ACCOUNT,
        "region": REGION,
    }


def outcomes(report: bootstrap.Report) -> dict[tuple[str, str], str]:
    return {(a.resource, a.name): a.outcome for a in report.actions}


def test_bootstrap_creates_then_is_idempotent(aws: Callable[[str], Any]) -> None:
    fake = FakeEcrPullThrough()
    c = clients(aws, fake)

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
    assert rules[0]["Expiration"]["Days"] == bootstrap.PAYLOAD_EXPIRY_DAYS
    assert s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"][
        "RestrictPublicBuckets"
    ]
    assert "aws:SecureTransport" in s3.get_bucket_policy(Bucket=BUCKET)["Policy"]
    assert fake.rules == {"aegc-quay": "quay.io"}
    template_tags = naming.tag_dict(fake.templates["aegc-quay"]["resourceTags"])
    assert naming.is_owned("aegc-quay", template_tags, naming.BOOTSTRAP)

    # The template uses aegc's own role; ECR's default role can't tag (spike #5).
    iam = aws("iam")
    role = iam.get_role(RoleName=ROLE)["Role"]
    assert fake.templates["aegc-quay"]["customRoleArn"] == role["Arn"]
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
    fake = FakeEcrPullThrough()
    c = clients(aws, fake)
    bootstrap.bootstrap(**c)
    del fake.templates["aegc-quay"]["customRoleArn"]
    report = outcomes(bootstrap.bootstrap(**c))
    assert report[("ecr creation template", "aegc-quay")] == "updated"
    assert fake.templates["aegc-quay"]["customRoleArn"].endswith(f"/aegc/{ROLE}")


def test_refuses_untagged_template_role(aws: Callable[[str], Any]) -> None:
    aws("iam").create_role(RoleName=ROLE, Path="/aegc/", AssumeRolePolicyDocument="{}")
    with pytest.raises(OwnershipError, match=ROLE):
        bootstrap.bootstrap(**clients(aws, FakeEcrPullThrough()))


def test_dry_run_creates_nothing(aws: Callable[[str], Any]) -> None:
    fake = FakeEcrPullThrough()
    report = outcomes(bootstrap.bootstrap(**clients(aws, fake), dry_run=True))
    assert set(report.values()) == {"would-create"}
    assert BUCKET not in [b["Name"] for b in aws("s3").list_buckets()["Buckets"]]
    assert fake.rules == {} and fake.templates == {}


def test_refuses_untagged_bucket_with_our_name(aws: Callable[[str], Any]) -> None:
    aws("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    with pytest.raises(OwnershipError):
        bootstrap.bootstrap(**clients(aws, FakeEcrPullThrough()))


def test_refuses_foreign_pull_through_rule(aws: Callable[[str], Any]) -> None:
    fake = FakeEcrPullThrough()
    fake.rules["aegc-quay"] = "public.ecr.aws"
    with pytest.raises(OwnershipError, match="refusing"):
        bootstrap.bootstrap(**clients(aws, fake))


def test_unbootstrap_refuses_while_deployments_exist(aws: Callable[[str], Any]) -> None:
    c = clients(aws, FakeEcrPullThrough())
    bootstrap.bootstrap(**c)
    aws("ssm").put_parameter(Name="/aegc/dev/manifest", Value="{}", Type="String")
    with pytest.raises(RuntimeError, match="dev"):
        bootstrap.unbootstrap(**c, dry_run=False)


def test_unbootstrap_dry_run_then_delete(aws: Callable[[str], Any]) -> None:
    fake = FakeEcrPullThrough()
    c = clients(aws, fake)
    bootstrap.bootstrap(**c)
    aws("s3").put_object(Bucket=BUCKET, Key="payloads/dev/tasks/x/1.task", Body=b"x")

    dry = outcomes(bootstrap.unbootstrap(**c))
    assert dry[("s3 bucket", BUCKET)] == "would-delete"
    assert BUCKET in [b["Name"] for b in aws("s3").list_buckets()["Buckets"]]

    done = outcomes(bootstrap.unbootstrap(**c, dry_run=False))
    assert done[("s3 bucket", BUCKET)] == "deleted"
    assert done[("ecr pull-through rule", "aegc-quay")] == "deleted"
    assert BUCKET not in [b["Name"] for b in aws("s3").list_buckets()["Buckets"]]
    assert fake.rules == {} and fake.templates == {}
    assert done[("iam role", ROLE)] == "deleted"
    with pytest.raises(aws("iam").exceptions.NoSuchEntityException):
        aws("iam").get_role(RoleName=ROLE)
    with pytest.raises(aws("ssm").exceptions.ParameterNotFound):
        aws("ssm").get_parameter(Name="/aegc/bootstrap")


@pytest.mark.xfail(
    strict=True,
    reason="substrate#1465: ECR ListTagsForResource can't resolve namespaced repository ARNs",
)
def test_unbootstrap_keeps_foreign_cache_repositories(aws: Callable[[str], Any]) -> None:
    c = clients(aws, FakeEcrPullThrough())
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
