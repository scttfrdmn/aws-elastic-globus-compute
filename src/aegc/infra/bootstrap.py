"""Region-wide resources, created once with ``aegc bootstrap``.

- S3 bucket ``aegc-<account>-<region>`` for payload offload: Block Public Access, bucket-owner
  enforced, TLS-only policy, payloads expire after 15 days (longer than SQS's 14-day retention).
- ECR pull-through cache rule ``aegc-quay`` → quay.io, so builds and pulls of aarchsci and
  aarchbio bases stay in-region, plus a repository creation template that tags the
  repositories the cache creates.
- SSM parameter ``/aegc/bootstrap`` recording the above. Pull-through rules can't be tagged, so
  this manifest is how unbootstrap knows the rule is aegc's.
"""

from __future__ import annotations

import json
import time
from typing import Any

from aegc import __version__, naming
from aegc.awsutil import error_code, is_not_found
from aegc.infra import Outcome, OwnershipError, Report

PAYLOAD_EXPIRY_DAYS = 15
UPSTREAM = "quay.io"
TEMPLATE_POLICY = "aegc-ecr-template"
TEMPLATE_ROLE_RETRIES = 6
TEMPLATE_ROLE_RETRY_S = 5.0


def _bucket_policy(bucket: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                }
            ],
        }
    )


def _bucket_tags(s3: Any, bucket: str) -> dict[str, str]:
    try:
        return naming.tag_dict(s3.get_bucket_tagging(Bucket=bucket)["TagSet"])
    except Exception as e:
        if error_code(e) in {"NoSuchTagSet", "NoSuchTagSetError"}:
            return {}
        raise


def _bucket_exists(s3: Any, bucket: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except Exception as e:
        if error_code(e) in {"404", "NoSuchBucket", "NotFound"}:
            return False
        raise


def ensure_bucket(s3: Any, bucket: str, region: str, report: Report, *, dry_run: bool) -> None:
    tags = naming.tags(naming.BOOTSTRAP, "bucket")
    if _bucket_exists(s3, bucket):
        if not naming.is_owned(bucket, _bucket_tags(s3, bucket), naming.BOOTSTRAP):
            raise OwnershipError(
                f"bucket {bucket} exists but isn't tagged as aegc's; refusing to use it"
            )
        report.add("s3 bucket", bucket, "exists")
    elif dry_run:
        report.add("s3 bucket", bucket, "would-create")
        return
    else:
        cfg: dict[str, Any] = {"Tags": naming.tag_list(tags)}
        if region != "us-east-1":
            cfg["LocationConstraint"] = region
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration=cfg)
        report.add("s3 bucket", bucket, "created")
    if dry_run:
        return
    # Idempotent settings, re-applied every run so drift is corrected.
    if not _bucket_tags(s3, bucket):
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": naming.tag_list(tags)})
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_ownership_controls(
        Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "aegc-expire-payloads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "payloads/"},
                    "Expiration": {"Days": PAYLOAD_EXPIRY_DAYS},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                }
            ]
        },
    )
    s3.put_bucket_policy(Bucket=bucket, Policy=_bucket_policy(bucket))


def ensure_pull_through_cache(
    ecr: Any, template_role_arn: str, report: Report, *, dry_run: bool
) -> None:
    prefix = naming.pull_through_prefix()
    try:
        rules = ecr.describe_pull_through_cache_rules(ecrRepositoryPrefixes=[prefix])
        rule = rules["pullThroughCacheRules"][0]
        if rule["upstreamRegistryUrl"] != UPSTREAM:
            raise OwnershipError(
                f"pull-through cache rule {prefix} points at {rule['upstreamRegistryUrl']}, "
                f"not {UPSTREAM}; refusing to change it"
            )
        report.add("ecr pull-through rule", prefix, "exists", UPSTREAM)
    except Exception as e:
        if isinstance(e, OwnershipError):
            raise
        if not is_not_found(e):
            raise
        if dry_run:
            report.add("ecr pull-through rule", prefix, "would-create", UPSTREAM)
        else:
            ecr.create_pull_through_cache_rule(
                ecrRepositoryPrefix=prefix, upstreamRegistryUrl=UPSTREAM
            )
            report.add("ecr pull-through rule", prefix, "created", UPSTREAM)

    # Tag the repositories the cache creates, so destroy can recognise them as aegc's. A template
    # that sets tags needs a custom role: ECR's default AWSServiceRoleForECRTemplate is denied
    # ecr:TagResource, and the cache then fails with only "repository not found" (spike #5).
    try:
        template = ecr.describe_repository_creation_templates(prefixes=[prefix])[
            "repositoryCreationTemplates"
        ][0]
    except (IndexError, KeyError):
        template = None
    except Exception as e:
        if not is_not_found(e):
            raise
        template = None
    if template is None:
        _create_template(ecr, prefix, template_role_arn, report, dry_run=dry_run)
    elif template.get("customRoleArn") != template_role_arn:
        if dry_run:
            report.add("ecr creation template", prefix, "updated", "would set its custom role")
        else:
            ecr.update_repository_creation_template(prefix=prefix, customRoleArn=template_role_arn)
            report.add("ecr creation template", prefix, "updated", "custom role set")
    else:
        report.add("ecr creation template", prefix, "exists")


def _create_template(
    ecr: Any, prefix: str, role_arn: str, report: Report, *, dry_run: bool
) -> None:
    if dry_run:
        report.add("ecr creation template", prefix, "would-create")
        return
    kwargs: dict[str, Any] = dict(
        prefix=prefix,
        description="aegc: tags repositories created by the aegc-quay pull-through cache",
        appliedFor=["PULL_THROUGH_CACHE"],
        imageTagMutability="MUTABLE",
        resourceTags=naming.tag_list(naming.tags(naming.BOOTSTRAP, "cache-repo")),
        customRoleArn=role_arn,
    )
    # A just-created IAM role can take a few seconds to become usable.
    for attempt in range(TEMPLATE_ROLE_RETRIES):
        try:
            ecr.create_repository_creation_template(**kwargs)
            break
        except Exception as e:
            if error_code(e) != "InvalidParameterException" or attempt == TEMPLATE_ROLE_RETRIES - 1:
                raise
            time.sleep(TEMPLATE_ROLE_RETRY_S)
    report.add("ecr creation template", prefix, "created")


def template_role_policies(account: str, region: str) -> tuple[str, str]:
    """(trust policy, permissions policy) for the creation template's custom role."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecr.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    perms = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["ecr:CreateRepository", "ecr:TagResource"],
                "Resource": (
                    f"arn:aws:ecr:{region}:{account}:repository/{naming.pull_through_prefix()}/*"
                ),
            }
        ],
    }
    return json.dumps(trust), json.dumps(perms)


def ensure_template_role(
    iam: Any, account: str, region: str, report: Report, *, dry_run: bool
) -> str:
    """The IAM role ECR assumes to create and tag pull-through cache repositories."""
    name = naming.ecr_template_role(region)
    arn = f"arn:aws:iam::{account}:role{naming.IAM_PATH}{name}"
    trust, perms = template_role_policies(account, region)
    try:
        role = iam.get_role(RoleName=name)["Role"]
        tags = naming.tag_dict(iam.list_role_tags(RoleName=name)["Tags"])
        if not naming.is_owned(name, tags, naming.BOOTSTRAP):
            raise OwnershipError(f"IAM role {name} exists but isn't tagged as aegc's")
        arn = role["Arn"]
        outcome: Outcome = "exists"
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if dry_run:
            report.add("iam role", name, "would-create", "ecr creation template")
            return arn
        arn = iam.create_role(
            Path=naming.IAM_PATH,
            RoleName=name,
            AssumeRolePolicyDocument=trust,
            Description="aegc: lets ECR create and tag aegc-quay pull-through cache repositories",
            Tags=naming.tag_list(naming.tags(naming.BOOTSTRAP, "ecr-template-role")),
        )["Role"]["Arn"]
        outcome = "created"
    if not dry_run:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
        iam.put_role_policy(RoleName=name, PolicyName=TEMPLATE_POLICY, PolicyDocument=perms)
    report.add("iam role", name, outcome, "ecr creation template")
    return str(arn)


def ensure_manifest(ssm: Any, bucket: str, report: Report, *, dry_run: bool) -> None:
    name = naming.bootstrap_parameter()
    value = json.dumps(
        {
            "version": __version__,
            "bucket": bucket,
            "pull_through_prefix": naming.pull_through_prefix(),
            "upstream": UPSTREAM,
            "updated_at": int(time.time()),
        },
        sort_keys=True,
    )
    try:
        current = ssm.get_parameter(Name=name)["Parameter"]
        tags = naming.tag_dict(
            ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)["TagList"]
        )
        if not naming.is_owned("aegc-bootstrap", tags, naming.BOOTSTRAP):
            raise OwnershipError(f"SSM parameter {name} exists but isn't tagged as aegc's")
        same = {k: v for k, v in json.loads(current["Value"]).items() if k != "updated_at"} == {
            k: v for k, v in json.loads(value).items() if k != "updated_at"
        }
        if same:
            report.add("ssm parameter", name, "exists")
        elif dry_run:
            report.add("ssm parameter", name, "updated", "would update")
        else:
            ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
            report.add("ssm parameter", name, "updated")
    except Exception as e:
        if isinstance(e, OwnershipError) or not is_not_found(e):
            raise
        if dry_run:
            report.add("ssm parameter", name, "would-create")
        else:
            ssm.put_parameter(
                Name=name,
                Value=value,
                Type="String",
                Tags=naming.tag_list(naming.tags(naming.BOOTSTRAP, "manifest")),
            )
            report.add("ssm parameter", name, "created")


def bootstrap(
    *, s3: Any, ecr: Any, ssm: Any, iam: Any, account: str, region: str, dry_run: bool = False
) -> Report:
    """Create or reconcile the region-wide resources. Safe to run repeatedly."""
    report = Report()
    bucket = naming.bucket_name(account, region)
    ensure_bucket(s3, bucket, region, report, dry_run=dry_run)
    role_arn = ensure_template_role(iam, account, region, report, dry_run=dry_run)
    ensure_pull_through_cache(ecr, role_arn, report, dry_run=dry_run)
    ensure_manifest(ssm, bucket, report, dry_run=dry_run)
    return report


def deployments(ssm: Any) -> list[str]:
    """Deployments with a manifest in this region (``/aegc/<d>/manifest``)."""
    found: list[str] = []
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path="/aegc/", Recursive=True):
        for p in page["Parameters"]:
            parts = p["Name"].strip("/").split("/")
            if len(parts) == 3 and parts[2] == "manifest":
                found.append(parts[1])
    return sorted(found)


def unbootstrap(
    *, s3: Any, ecr: Any, ssm: Any, iam: Any, account: str, region: str, dry_run: bool = True
) -> Report:
    """Remove the region-wide resources; refuses while deployments remain. Dry run by default."""
    remaining = deployments(ssm)
    if remaining:
        raise RuntimeError(f"deployments still exist: {', '.join(remaining)}; destroy them first")
    report = Report()
    bucket = naming.bucket_name(account, region)
    verb: Outcome = "would-delete" if dry_run else "deleted"

    try:
        manifest = json.loads(
            ssm.get_parameter(Name=naming.bootstrap_parameter())["Parameter"]["Value"]
        )
    except Exception as e:
        if not is_not_found(e):
            raise
        manifest = {}

    if _bucket_exists(s3, bucket):
        if naming.is_owned(bucket, _bucket_tags(s3, bucket), naming.BOOTSTRAP):
            if not dry_run:
                _empty_bucket(s3, bucket)
                s3.delete_bucket(Bucket=bucket)
            report.add("s3 bucket", bucket, verb)
        else:
            report.add("s3 bucket", bucket, "kept", "not tagged as aegc's")

    prefix = manifest.get("pull_through_prefix")
    if prefix == naming.pull_through_prefix():
        deletes: list[tuple[str, str, dict[str, str]]] = [
            ("ecr creation template", "delete_repository_creation_template", {"prefix": prefix}),
            (
                "ecr pull-through rule",
                "delete_pull_through_cache_rule",
                {"ecrRepositoryPrefix": prefix},
            ),
        ]
        for kind, method, kwargs in deletes:
            try:
                if not dry_run:
                    getattr(ecr, method)(**kwargs)
                report.add(kind, prefix, verb)
            except Exception as e:
                if not is_not_found(e):
                    raise
        report.actions.extend(_cache_repositories(ecr, prefix, dry_run=dry_run).actions)

    _delete_template_role(iam, region, report, dry_run=dry_run)

    if manifest:
        if not dry_run:
            ssm.delete_parameter(Name=naming.bootstrap_parameter())
        report.add("ssm parameter", naming.bootstrap_parameter(), verb)
    return report


def _delete_template_role(iam: Any, region: str, report: Report, *, dry_run: bool) -> None:
    name = naming.ecr_template_role(region)
    try:
        tags = naming.tag_dict(iam.list_role_tags(RoleName=name)["Tags"])
    except Exception as e:
        if is_not_found(e):
            return
        raise
    if not naming.is_owned(name, tags, naming.BOOTSTRAP):
        report.add("iam role", name, "kept", "not tagged as aegc's")
        return
    if not dry_run:
        for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        iam.delete_role(RoleName=name)
    report.add("iam role", name, "would-delete" if dry_run else "deleted")


def _cache_repositories(ecr: Any, prefix: str, *, dry_run: bool) -> Report:
    """Repositories the pull-through cache created: delete only those carrying aegc's tags."""
    report = Report()
    paginator = ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for repo in page["repositories"]:
            name = repo["repositoryName"]
            if not name.startswith(f"{prefix}/"):
                continue
            tags = naming.tag_dict(
                ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"])["tags"]
            )
            if naming.is_owned(prefix, tags, naming.BOOTSTRAP):
                if not dry_run:
                    ecr.delete_repository(repositoryName=name, force=True)
                report.add("ecr repository", name, "would-delete" if dry_run else "deleted")
            else:
                report.add("ecr repository", name, "kept", "not tagged as aegc's")
    return report


def _empty_bucket(s3: Any, bucket: str) -> None:
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objs = [
            {"Key": o["Key"], "VersionId": o["VersionId"]}
            for o in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i : i + 1000], "Quiet": True})
