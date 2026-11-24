"""Lambda pools, the deploy side (#21, #76): PoolSpec, --pool, deploy, prune, destroy, IAM."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from aegc import images, lambda_pools, naming, state
from aegc.infra import OwnershipError, Report, deployment, iam
from aegc.infra.deployment import Clients, DeploymentSpec, PoolSpec
from tests.conftest import ACCOUNT, REGION
from tests.substrate_gaps import lambda_echoes
from tests.unit.test_deployment import outcomes
from tests.unit.test_iam import decide, principal
from tests.unit.test_images import push_fake

NAME = "geospatial-py314-lambda"
FUNCTION = f"aegc-dev-{NAME}"
ROLE = f"aegc-dev-{NAME}-{REGION}"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/aegc/geospatial@sha256:" + "b" * 64


def lam_pool(**kw: Any) -> PoolSpec:
    base: dict[str, Any] = dict(
        name=NAME, env="geospatial", python_minor="3.14", image=IMAGE, kind="lambda"
    )
    return PoolSpec(**{**base, **kw})


@pytest.fixture
def env(aws: Callable[[str], Any]) -> tuple[Clients, DeploymentSpec, dict[str, list[Any]]]:
    lam = aws("lambda")
    sent = lambda_echoes(lam)  # substrate#1659: echoes what was sent; records the requests
    c = Clients(
        iam=aws("iam"), ec2=aws("ec2"), autoscaling=aws("autoscaling"),
        sqs=aws("sqs"), logs=aws("logs"), ssm=aws("ssm"), ecr=aws("ecr"), lambda_=lam,
    )  # fmt: skip
    images.ensure_repository(c.ecr, "geospatial", Report())
    image = push_fake(c.ecr, "geospatial", "build-1", 1)
    spec = DeploymentSpec(
        deployment="dev",
        account=ACCOUNT,
        region=REGION,
        bucket=naming.bucket_name(ACCOUNT, REGION),
        pools=(lam_pool(image=image, memory_mib=4096, max_concurrency=20),),
    )
    return c, spec, sent


def function(c: Clients) -> dict[str, Any]:
    return dict(c.lambda_.get_function(FunctionName=FUNCTION))


def mappings(c: Clients) -> list[dict[str, Any]]:
    return list(c.lambda_.list_event_source_mappings(FunctionName=FUNCTION)["EventSourceMappings"])


# --- PoolSpec ----------------------------------------------------------------------------------


def test_lambda_pool_defaults_and_walltime() -> None:
    p = lam_pool()
    assert (p.memory_mib, p.timeout_s, p.max_concurrency, p.ephemeral_mib) == (2048, 900, 10, 0)
    assert (
        p.max_walltime_s
        == 900 - lambda_pools.MARGIN_S
        == lambda_pools.max_walltime_s({"timeout_s": p.timeout_s})
    )
    assert p.walltime_s == p.max_walltime_s
    p = lam_pool(timeout_s=300, walltime_s=60)
    assert (p.max_walltime_s, p.walltime_s) == (280, 60)
    # A manifest entry comes back as the same pool (what a redeploy merges over).
    entry = {**p.__dict__, "function_name": "x", "event_source_mapping": "u"}
    assert PoolSpec.from_manifest(NAME, entry) == p


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"memory_mib": 64}, "memory="),
        ({"memory_mib": 10_241}, "memory="),
        ({"timeout_s": 30}, "timeout="),
        ({"timeout_s": 1801}, "timeout="),
        ({"max_concurrency": 1}, "concurrency="),
        ({"max_concurrency": 1001}, "concurrency="),
        ({"ephemeral_mib": 256}, "tmp="),
        ({"ephemeral_mib": 20_000}, "tmp="),
        ({"timeout_s": 300, "max_walltime_s": 290}, "at most 280"),
        ({"timeout_s": 300, "walltime_s": 290}, "exceeds"),
        ({"isolate_tasks": True}, "can't isolate"),
        ({"max_vcpus": 32}, "EC2 pools only"),
        ({"spot": False}, "EC2 pools only"),
        ({"size_class": "large"}, "EC2 pools only"),
        ({"root_volume_gib": 100}, "EC2 pools only"),
        ({"max_lifetime_s": 3600}, "EC2 pools only"),
        ({"kind": "fargate"}, "pool kind"),
    ],
)
def test_lambda_pool_refuses(kw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        lam_pool(**kw)


def test_ec2_pools_refuse_lambda_settings() -> None:
    with pytest.raises(ValueError, match="Lambda pools only"):
        PoolSpec(name="sci-py314", env="sci", python_minor="3.14", image=IMAGE, memory_mib=4096)
    # EC2 pools are as before: the Lambda fields stay 0, and the defaults validate.
    p = PoolSpec(name="sci-py314", env="sci", python_minor="3.14", image=IMAGE)
    assert (p.kind, p.memory_mib, p.timeout_s, p.walltime_s) == ("ec2", 0, 0, 0)


def test_long_names_are_refused_before_anything_changes() -> None:
    long = lam_pool(name="e" * 26 + "-py314-lambda")  # a valid pool name (39 chars)
    with pytest.raises(ValueError, match="longer than 64"):
        DeploymentSpec(deployment="d" * 20, account=ACCOUNT, region=REGION, bucket="b",
                       pools=(long,))  # fmt: skip


# --- --pool ------------------------------------------------------------------------------------


def test_parse_pool_lambda() -> None:
    from aegc.cli import parse_pool

    got = parse_pool(
        "geospatial:lambda,memory=4096,timeout=600,concurrency=20,tmp=2048,walltime=60"
    )
    assert got == {
        "env": "geospatial", "kind": "lambda", "memory_mib": 4096, "timeout_s": 600,
        "max_concurrency": 20, "ephemeral_mib": 2048, "walltime_s": 60,
    }  # fmt: skip
    assert parse_pool("geospatial:lambda,python=3.13") == {
        "env": "geospatial", "kind": "lambda", "python": "3.13"
    }  # fmt: skip
    for bad, match in (
        ("geospatial:lambda,max_vcpus=8", "for EC2 pools"),
        ("geospatial:lambda,on-demand,disk=50", "are for EC2 pools"),
        ("geospatial:memory=4096", "need a Lambda pool"),
        ("geospatial:lambda=yes", "takes no value"),
    ):
        with pytest.raises(SystemExit, match=match):
            parse_pool(bad)


# --- deploy ------------------------------------------------------------------------------------


def test_deploy_creates_role_function_and_mapping(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, sent = env
    pool = spec.pools[0]
    r = outcomes(deployment.deploy(c, spec))
    assert r[("iam role", ROLE)] == "created"
    assert r[("lambda function", FUNCTION)] == "created"
    assert r[("ecr repository policy", "aegc/geospatial")] == "created"
    assert ("auto scaling group", FUNCTION) not in r  # no group, no launch template
    assert not c.autoscaling.describe_auto_scaling_groups()["AutoScalingGroups"]

    # The function role: under /aegc/, trusted by Lambda, tagged, with the pool's policy.
    role = c.iam.get_role(RoleName=ROLE)["Role"]
    assert role["Path"] == "/aegc/"
    trust = deployment._policy_document(role["AssumeRolePolicyDocument"])
    assert trust["Statement"][0]["Principal"] == {"Service": "lambda.amazonaws.com"}
    tags = naming.tag_dict(c.iam.list_role_tags(RoleName=ROLE)["Tags"])
    assert naming.is_owned(ROLE, tags, "dev") and tags[naming.TAG_ROLE] == "function-role"
    assert c.iam.list_role_policies(RoleName=ROLE)["PolicyNames"] == [iam.FUNCTION_POLICY]

    # CreateFunction: the image, arm64, the settings, the worker config, ownership tags.
    (create,) = sent["CreateFunction"]
    assert create["PackageType"] == "Image" and create["Code"] == {"ImageUri": pool.image}
    assert create["Architectures"] == ["arm64"]
    assert (create["MemorySize"], create["Timeout"]) == (4096, 900)
    assert create["EphemeralStorage"] == {"Size": 512}
    assert create["ImageConfig"] == {"Command": ["aegc-worker", "lambda"]}
    assert create["LoggingConfig"] == {"LogFormat": "Text", "LogGroup": "/aegc/dev"}
    assert create["Role"] == f"arn:aws:iam::{ACCOUNT}:role/aegc/{ROLE}"
    assert naming.is_owned(FUNCTION, create["Tags"], "dev")
    assert create["Tags"][naming.TAG_ROLE] == "function"
    variables = create["Environment"]["Variables"]
    assert variables["AEGC_IMAGE"] == pool.image
    cfg = json.loads(base64.b64decode(variables["AEGC_WORKER_CONFIG_B64"]))
    url = c.sqs.get_queue_url(QueueName=f"aegc-dev-{NAME}-tasks")["QueueUrl"]
    assert (cfg["pool"], cfg["task_queue_url"], cfg["state_parameter"]) == (
        NAME, url, "/aegc/dev/state"
    )  # fmt: skip
    got = function(c)
    assert got["Code"]["ImageUri"] == pool.image
    assert got["Configuration"]["State"] == "Active"

    # The mapping: from the task queue, one message per batch, partial failures, the cap.
    (esm,) = sent["CreateEventSourceMapping"]
    qarn = c.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]
    assert esm["EventSourceArn"] == qarn["QueueArn"] and esm["FunctionName"] == FUNCTION
    assert esm["BatchSize"] == 1
    assert esm["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert esm["ScalingConfig"] == {"MaximumConcurrency": 20}
    assert esm["Enabled"] is True
    assert naming.is_owned(FUNCTION, esm["Tags"], "dev")
    (m,) = mappings(c)
    assert m["State"] == "Enabled"

    # The queue: visibility past the function's timeout (an SQS mapping needs at least that).
    a = c.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    assert int(a["VisibilityTimeout"]) == 900 * deployment.LAMBDA_VISIBILITY_FACTOR
    assert json.loads(a["RedrivePolicy"])["maxReceiveCount"] in (6, "6")

    # Lambda pulls the image as the service, for aegc-* functions only.
    pol = json.loads(c.ecr.get_repository_policy(repositoryName="aegc/geospatial")["policyText"])
    (st,) = [s for s in pol["Statement"] if s["Sid"] == images.LAMBDA_PULL_SID]
    assert st["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert st["Condition"]["ArnLike"]["aws:sourceArn"].endswith(":function:aegc-*")

    # The manifest entry (aegc.lambda_pools): no EC2 keys.
    entry = deployment.read_manifest(c.ssm, "dev")["pools"][NAME]
    assert lambda_pools.is_lambda(entry)
    assert entry["function_name"] == FUNCTION == lambda_pools.function_name("dev", NAME)
    assert entry["function_arn"] == got["Configuration"]["FunctionArn"]
    assert entry["event_source_mapping"] == m["UUID"]
    assert entry["function_role_arn"] == role["Arn"]
    assert entry["function_role_id"] == role["RoleId"]  # the sender the engine accepts (#80)
    assert entry["task_queue_url"] == url and entry["dlq_url"].endswith("-dlq")
    assert (entry["memory_mib"], entry["timeout_s"], entry["max_concurrency"]) == (4096, 900, 20)
    for k in ("auto_scaling_group", "launch_template", "capacity_type"):
        assert k not in entry


def test_redeploy_updates_in_place(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, sent = env
    deployment.deploy(c, spec)
    arn = function(c)["Configuration"]["FunctionArn"]
    uuid = mappings(c)[0]["UUID"]
    again = outcomes(deployment.deploy(c, spec))
    assert again[("lambda function", FUNCTION)] == "exists"
    assert again[("iam role", ROLE)] == "exists"
    assert again[("ecr repository policy", "aegc/geospatial")] == "exists"
    label = f"{FUNCTION} <- aegc-dev-{NAME}-tasks"
    assert again[("lambda event source mapping", label)] == "exists"
    assert "UpdateFunctionCode" not in sent and "UpdateFunctionConfiguration" not in sent

    newer = push_fake(c.ecr, "geospatial", "build-2", 2)
    pool = replace(spec.pools[0], image=newer, memory_mib=8192, max_concurrency=50)
    spec2 = replace(spec, pools=(pool,))
    assert deployment.deploy(c, spec2, dry_run=True)
    assert function(c)["Code"]["ImageUri"] == spec.pools[0].image  # a dry run changes nothing
    r = deployment.deploy(c, spec2)
    a = {x.name: x for x in r.actions}
    assert a[FUNCTION].outcome == "updated" and "image" in a[FUNCTION].detail
    assert a[label].outcome == "updated"
    assert sent["UpdateFunctionCode"] == [
        {"FunctionName": FUNCTION, "ImageUri": newer, "Architectures": ["arm64"]}
    ]
    assert sent["UpdateFunctionConfiguration"][-1]["MemorySize"] == 8192
    assert sent["UpdateEventSourceMapping"][-1]["ScalingConfig"] == {"MaximumConcurrency": 50}
    assert "Enabled" not in sent["UpdateEventSourceMapping"][-1]  # never enabled by a redeploy
    got = function(c)
    assert got["Configuration"]["FunctionArn"] == arn and got["Code"]["ImageUri"] == newer
    assert got["Configuration"]["MemorySize"] == 8192
    assert [m["UUID"] for m in mappings(c)] == [uuid]
    assert len(sent["CreateFunction"]) == 1 and "DeleteFunction" not in sent
    entry = deployment.read_manifest(c.ssm, "dev")["pools"][NAME]
    assert (entry["memory_mib"], entry["max_concurrency"], entry["image"]) == (8192, 50, newer)


def test_halted_deployment_keeps_its_mapping_disabled(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, sent = env
    state.write_state(c.ssm, "dev", state.State(mode="stopped", reason="test", by="aegc stop"))
    deployment.deploy(c, spec)
    assert sent["CreateEventSourceMapping"][0]["Enabled"] is False
    assert mappings(c)[0]["State"] == "Disabled"
    kept = {(a.resource, a.name) for a in deployment.deploy(c, spec).actions if a.outcome == "kept"}
    assert ("ssm parameter", "/aegc/dev/state") in kept
    # A redeploy on the halted deployment changes the concurrency, and doesn't enable it.
    spec2 = replace(spec, pools=(replace(spec.pools[0], max_concurrency=30),))
    deployment.deploy(c, spec2)
    assert sent["UpdateEventSourceMapping"][-1].get("Enabled") is not True
    assert mappings(c)[0]["State"] == "Disabled"
    # An enabled mapping on a halted deployment (a freeze that didn't reach it) is disabled.
    uuid = mappings(c)[0]["UUID"]
    c.lambda_.update_event_source_mapping(UUID=uuid, Enabled=True)
    deployment.deploy(c, spec2)
    assert sent["UpdateEventSourceMapping"][-1]["Enabled"] is False
    assert mappings(c)[0]["State"] == "Disabled"
    # Running again (resume): a redeploy reports the disabled mapping, and leaves it to resume.
    state.write_state(c.ssm, "dev", state.State(reason="resumed", by="aegc resume"))
    r = deployment.deploy(c, spec2)
    (act,) = [a for a in r.actions if a.resource == "lambda event source mapping"]
    assert act.outcome == "kept" and "enable it while the deployment runs" in act.detail


def test_dry_run_creates_nothing(env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]]) -> None:
    c, spec, sent = env
    r = outcomes(deployment.deploy(c, spec, dry_run=True))
    assert r[("lambda function", FUNCTION)] == "would-create"
    assert r[("iam role", ROLE)] == "would-create"
    assert r[("ecr repository policy", "aegc/geospatial")] == "would-create"
    assert "CreateFunction" not in sent and "CreateEventSourceMapping" not in sent
    assert not c.lambda_.list_functions()["Functions"]
    with pytest.raises(ClientError):
        c.iam.get_role(RoleName=ROLE)


def test_foreign_function_role_and_mapping_are_refused(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, _ = env
    trust = json.dumps(iam.lambda_trust_policy())
    # An untagged role with the function role's name.
    c.iam.create_role(Path="/aegc/", RoleName=ROLE, AssumeRolePolicyDocument=trust)
    with pytest.raises(OwnershipError, match=ROLE):
        deployment.deploy(c, spec)
    c.iam.delete_role(RoleName=ROLE)
    # An untagged function with the pool's function name.
    other = c.iam.create_role(RoleName="someone", AssumeRolePolicyDocument=trust)["Role"]["Arn"]
    c.lambda_.create_function(FunctionName=FUNCTION, PackageType="Image", Role=other,
                              Code={"ImageUri": spec.pools[0].image})  # fmt: skip
    with pytest.raises(OwnershipError, match=FUNCTION):
        deployment.deploy(c, spec)
    assert function(c)["Configuration"]["Role"] == other  # untouched
    # destroy leaves it alone too.
    done = deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=5,
                              poll_s=0.1)  # fmt: skip
    assert ("lambda function", FUNCTION) not in outcomes(done)
    assert function(c)["Configuration"]["Role"] == other


def test_foreign_mapping_is_refused(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, _ = env
    deployment.deploy(c, spec)
    (m,) = mappings(c)
    c.lambda_.delete_event_source_mapping(UUID=m["UUID"])
    c.lambda_.create_event_source_mapping(  # untagged
        EventSourceArn=m["EventSourceArn"], FunctionName=FUNCTION, BatchSize=1
    )
    with pytest.raises(OwnershipError, match="event source mapping"):
        deployment.deploy(c, spec)


def test_oversized_image_and_environment_are_refused_first(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, spec, sent = env
    monkeypatch.setattr(lambda_pools, "MAX_IMAGE_BYTES", 100)  # push_fake's image is 274 bytes
    with pytest.raises(ValueError, match="compressed"):
        deployment.deploy(c, spec)
    monkeypatch.undo()
    grants = tuple(
        iam.Grant("read", "my-data-bucket", f"projects/{'x' * 60}/{i}/") for i in range(30)
    )
    with pytest.raises(ValueError, match="environment"):
        deployment.deploy(c, replace(spec, grants=grants))
    assert not c.sqs.list_queues().get("QueueUrls")  # nothing changed
    assert "CreateFunction" not in sent


def test_grants_go_on_the_function_role_not_the_worker_role(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, _ = env
    spec = replace(spec, grants=(iam.Grant("read", "my-data-bucket", "in/"),))
    assert spec.unisolated_pools == ()  # a Lambda pool isn't an unisolated EC2 pool
    r = outcomes(deployment.deploy(c, spec))
    assert r[("iam role policy", f"{iam.GRANTS_POLICY} ({NAME} function role)")] == "created"
    assert sorted(c.iam.list_role_policies(RoleName=ROLE)["PolicyNames"]) == sorted(
        [iam.FUNCTION_POLICY, iam.GRANTS_POLICY]
    )
    worker = naming.worker_role("dev", REGION)
    assert iam.GRANTS_POLICY not in c.iam.list_role_policies(RoleName=worker)["PolicyNames"]
    cfg = json.loads(base64.b64decode(
        function(c)["Configuration"]["Environment"]["Variables"]["AEGC_WORKER_CONFIG_B64"]
    ))  # fmt: skip
    assert cfg["grants"] == [{"access": "read", "bucket": "my-data-bucket", "prefix": "in/"}]


def test_mixed_deployment_passes_groups_and_mappings_to_the_lease(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegc import lease

    c, spec, _ = env
    ec2_pool = PoolSpec(name="geospatial-py314", env="geospatial", python_minor="3.14",
                        image=spec.pools[0].image)  # fmt: skip
    seen: list[Any] = []
    monkeypatch.setattr(lease, "ensure_lease", lambda **kw: seen.append(kw["spec"]))
    deployment.deploy(
        replace(c, scheduler=object()), replace(spec, pools=(ec2_pool, spec.pools[0]))
    )
    (ls,) = seen
    assert ls.groups == {"geospatial-py314": "aegc-dev-geospatial-py314"}
    assert ls.mappings == {NAME: mappings(c)[0]["UUID"]}
    pools = deployment.read_manifest(c.ssm, "dev")["pools"]
    assert not lambda_pools.is_lambda(pools["geospatial-py314"])
    assert pools["geospatial-py314"]["auto_scaling_group"] == "aegc-dev-geospatial-py314"


# --- prune and destroy -------------------------------------------------------------------------


def test_prune_removes_a_lambda_pool(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, _ = env
    deployment.deploy(c, spec)
    entry = deployment.read_manifest(c.ssm, "dev")["pools"][NAME]
    dry = Report()
    deployment.prune_pool(c, "dev", NAME, entry, dry, dry_run=True)
    assert outcomes(dry)[("lambda function", FUNCTION)] == "would-delete"
    assert function(c) and mappings(c)
    r = Report()
    deployment.prune_pool(c, "dev", NAME, entry, r, dry_run=False)
    got = outcomes(r)
    assert got[("lambda function", FUNCTION)] == "deleted"
    assert got[("iam role", ROLE)] == "deleted"
    assert got[("sqs queue", f"aegc-dev-{NAME}-tasks")] == "deleted"
    assert not c.lambda_.list_functions()["Functions"]
    assert not c.lambda_.list_event_source_mappings()["EventSourceMappings"]
    with pytest.raises(ClientError):
        c.iam.get_role(RoleName=ROLE)


def test_destroy_deletes_lambda_pools_and_verifies(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    c, spec, _ = env
    deployment.deploy(c, spec)
    left = {a.resource for a in deployment.verify_destroyed(c, deployment="dev", region=REGION,
                                                            wait_s=0).actions}  # fmt: skip
    assert {"lambda function", "lambda event source mapping", "iam role"} <= left
    # Another deployment's function, and a look-alike: both kept.
    trust = json.dumps(iam.lambda_trust_policy())
    other = c.iam.create_role(RoleName="someone", AssumeRolePolicyDocument=trust)["Role"]["Arn"]
    for name, tags in (
        ("aegc-dev-stray-py314-lambda", {}),
        ("aegc-prod-x-py314-lambda", naming.tags("prod", "function")),
    ):
        c.lambda_.create_function(FunctionName=name, PackageType="Image", Role=other, Tags=tags,
                                  Code={"ImageUri": spec.pools[0].image})  # fmt: skip

    dry = outcomes(deployment.destroy(c, deployment="dev", region=REGION))
    assert dry[("lambda function", FUNCTION)] == "would-delete"
    assert dry[("iam role", ROLE)] == "would-delete"
    assert function(c) and mappings(c)
    done = deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=5,
                              poll_s=0.1)  # fmt: skip
    r = outcomes(done)
    assert r[("lambda function", FUNCTION)] == "deleted"
    assert r[("iam role", ROLE)] == "deleted"
    assert deployment.remaining(done) == []
    assert deployment.verify_destroyed(c, deployment="dev", region=REGION, wait_s=0).actions == []
    names = sorted(f["FunctionName"] for f in c.lambda_.list_functions()["Functions"])
    assert names == ["aegc-dev-stray-py314-lambda", "aegc-prod-x-py314-lambda"]


def test_destroy_finds_a_function_role_left_behind(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    # An interrupted destroy: the function went, its role stayed. Only a listing finds it.
    c, spec, _ = env
    deployment.deploy(c, spec)
    c.lambda_.delete_event_source_mapping(UUID=mappings(c)[0]["UUID"])
    c.lambda_.delete_function(FunctionName=FUNCTION)
    left = deployment.verify_destroyed(c, deployment="dev", region=REGION, wait_s=0)
    assert ("iam role", ROLE) in {(a.resource, a.name) for a in left.actions}
    done = deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=5,
                              poll_s=0.1)  # fmt: skip
    assert outcomes(done)[("iam role", ROLE)] == "deleted"
    assert deployment.remaining(done) == []


def test_destroy_survives_a_deployer_without_the_lambda_policy(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    class Denied:
        def __getattr__(self, name: str) -> Any:
            def call(*a: Any, **kw: Any) -> Any:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                                  name)  # fmt: skip

            return call

    c, spec, _ = env
    ec2_pool = PoolSpec(
        name="geospatial-py314", env="geospatial", python_minor="3.14", image=spec.pools[0].image
    )
    ec2_only = replace(spec, pools=(ec2_pool,))
    deployment.deploy(c, ec2_only)
    c = replace(c, lambda_=Denied())
    r = deployment.destroy(c, deployment="dev", region=REGION, dry_run=False, wait_s=30,
                           poll_s=0.1)  # fmt: skip
    assert ("lambda pools", "dev", "kept") in {(a.resource, a.name, a.outcome) for a in r.actions}
    v = deployment.verify_destroyed(c, deployment="dev", region=REGION, wait_s=0)
    assert deployment.remaining(v) == []  # unchecked is said, but isn't a failure...
    assert [a.detail for a in v.actions] == [deployment.LAMBDA_DENIED]


def test_destroy_without_lambda_deployer_fails_when_there_are_lambda_pools(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    # Docs review: it said "kept" and verification passed, with the functions still there.
    from botocore.exceptions import ClientError

    class Denied:
        def __getattr__(self, name: str) -> Any:
            def call(*a: Any, **kw: Any) -> Any:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                                  name)  # fmt: skip

            return call

    c, spec, _ = env
    deployment.deploy(c, spec)  # with its Lambda pool
    r = deployment.destroy(replace(c, lambda_=Denied()), deployment="dev", region=REGION,
                           dry_run=False, wait_s=30, poll_s=0.1)  # fmt: skip
    assert deployment.remaining(r) and "lambda-deployer" in deployment.remaining(r)[0]


def test_footprint_lists_functions_and_mappings(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]],
) -> None:
    from aegc import ops

    c, spec, _ = env
    deployment.deploy(c, spec)
    trust = json.dumps(iam.lambda_trust_policy())
    other = c.iam.create_role(RoleName="someone", AssumeRolePolicyDocument=trust)["Role"]["Arn"]
    c.lambda_.create_function(FunctionName="aegc-dev-stray-py314-lambda", PackageType="Image",
                              Role=other, Code={"ImageUri": spec.pools[0].image})  # fmt: skip
    fp = ops.Footprint(deployment="dev", region=REGION)
    deployment.lambda_footprint(c.lambda_, fp, "dev")
    got = {(i.kind, i.name): i.owned for i in fp.items.values()}
    uuid = mappings(c)[0]["UUID"]
    assert got[("lambda function", FUNCTION)] is True
    assert got[("lambda event-source-mapping", uuid)] is True
    assert got[("lambda function", "aegc-dev-stray-py314-lambda")] is False


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_deploys_and_redeploys_a_lambda_pool(
    env: tuple[Clients, DeploymentSpec, dict[str, list[Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from aegc import cli

    c, spec, _ = env
    image = spec.pools[0].image

    def record(version: str) -> None:
        images.record_image(c.ssm, images.ImageRecord(
            env="geospatial", python_minor="3.14", image=image, tag="t", base="b",
            gce_version="4.21.0", aegc_version=version, built_at=1))  # fmt: skip

    checked: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(cli, "_clients", lambda sess: c)
    monkeypatch.setattr(cli, "_quota_preflight", lambda sess, pools: checked.append(pools) or [])
    argv = ["--region", REGION, "deploy", "dev", "--max-usd-day", "5"]
    for old in ("0.6.0", "0.7.0"):  # no `aegc-worker lambda`; results at the old keys (#80)
        record(old)
        with pytest.raises(SystemExit, match=r"aegc 0\.8"):
            cli.main([*argv, "--pool", "geospatial:lambda"])
    record("0.8.0")
    assert cli.main([*argv, "--pool", "geospatial:lambda,memory=4096,timeout=600",
                     "--pool", "geospatial"]) == 0  # fmt: skip
    pools = deployment.read_manifest(c.ssm, "dev")["pools"]
    assert sorted(pools) == ["geospatial-py314", NAME]
    lp = pools[NAME]
    assert (lp["kind"], lp["memory_mib"], lp["timeout_s"], lp["max_walltime_s"]) == (
        "lambda", 4096, 600, 580
    )  # fmt: skip
    assert lp["isolate_tasks"] is False
    assert [p["name"] for p in checked[-1]] == ["geospatial-py314"]  # vCPU quotas: EC2 only
    # A redeploy keeps the options it isn't given; a new timeout moves the derived walltimes.
    assert cli.main([*argv, "--pool", "geospatial:lambda,timeout=300", "--pool",
                     "geospatial"]) == 0  # fmt: skip
    lp = deployment.read_manifest(c.ssm, "dev")["pools"][NAME]
    assert (lp["memory_mib"], lp["timeout_s"], lp["max_walltime_s"], lp["walltime_s"]) == (
        4096, 300, 280, 280
    )  # fmt: skip
    assert function(c)["Configuration"]["Timeout"] == 300
    with pytest.raises(SystemExit, match="concurrency="):
        cli.main([*argv, "--pool", "geospatial:lambda,concurrency=1"])


# --- IAM ---------------------------------------------------------------------------------------

SQS = f"arn:aws:sqs:{REGION}:{ACCOUNT}"
BUCKET = naming.bucket_name(ACCOUNT, REGION)
FN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function"
ESM = f"arn:aws:lambda:{REGION}:{ACCOUNT}:event-source-mapping:1234"


def test_function_role_is_scoped_to_its_pool(aws: Callable[[str], Any]) -> None:
    doc = iam.function_policy(deployment="dev", pool=NAME, env="geospatial", region=REGION,
                              account=ACCOUNT, bucket=BUCKET)  # fmt: skip
    c, arn = principal(aws, doc)
    ecr = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository"
    for action, res in [
        ("sqs:ReceiveMessage", f"{SQS}:aegc-dev-{NAME}-tasks"),
        ("sqs:DeleteMessage", f"{SQS}:aegc-dev-{NAME}-tasks"),
        ("sqs:ChangeMessageVisibility", f"{SQS}:aegc-dev-{NAME}-tasks"),
        ("sqs:GetQueueAttributes", f"{SQS}:aegc-dev-{NAME}-tasks"),
        ("sqs:SendMessage", f"{SQS}:aegc-dev-res-1234567890ab"),
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/{NAME}/tasks/u/1.task"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/{NAME}/results/u/u:1.result"),
        ("ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/state"),
        ("logs:PutLogEvents", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aegc/dev:log-stream:x"),
        ("ecr:BatchGetImage", f"{ecr}/aegc/geospatial"),
    ]:
        assert decide(c, arn, action, res) == "allowed", (action, res)
    for action, res in [
        ("sqs:ReceiveMessage", f"{SQS}:aegc-dev-sci-py314-tasks"),  # another pool's queue
        # Review: tasks run with this role; Lambda spend comes from Lambda's metrics instead.
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/ledger/dev/2026-11-23/lambda-1.json"),
        ("sqs:ReceiveMessage", f"{SQS}:aegc-prod-{NAME}-tasks"),
        ("sqs:SendMessage", f"{SQS}:aegc-dev-{NAME}-tasks"),  # no submitting
        ("sqs:DeleteQueue", f"{SQS}:aegc-dev-{NAME}-tasks"),
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/prod/tasks/u/1.task"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/tasks/u/1.task"),
        # #80: not another pool's payloads (nor a pool whose name starts with this one's), nor
        # the layout before 0.8.
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/sci-py314/tasks/u/1.task"),
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/{NAME}-x/tasks/u/1.task"),
        ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/tasks/u/1.task"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/payloads/dev/sci-py314/results/u/u:1.result"),
        ("ssm:PutParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/aegc/dev/state"),
        ("autoscaling:SetDesiredCapacity", "*"),
        ("sts:AssumeRole", f"arn:aws:iam::{ACCOUNT}:role/aegc/aegc-dev-task-{REGION}"),
        ("ecr:BatchGetImage", f"{ecr}/aegc/other"),
    ]:
        assert decide(c, arn, action, res) != "allowed", (action, res)


def test_lambda_deployer_policy(aws: Callable[[str], Any]) -> None:
    c, arn = principal(aws, iam.lambda_deployer_policy(region=REGION, account=ACCOUNT))
    managed = {"aws:RequestTag/aegc:managed": "true"}
    owned = {"aws:ResourceTag/aegc:managed": "true"}
    of_aegc = {"lambda:FunctionArn": f"{FN}:aegc-dev-x"}
    assert decide(c, arn, "lambda:CreateFunction", f"{FN}:aegc-dev-x", **managed) == "allowed"
    assert decide(c, arn, "lambda:CreateFunction", f"{FN}:aegc-dev-x") != "allowed"  # untagged
    assert decide(c, arn, "lambda:CreateFunction", f"{FN}:other", **managed) != "allowed"
    assert decide(c, arn, "lambda:DeleteFunction", f"{FN}:aegc-dev-x", **owned) == "allowed"
    assert decide(c, arn, "lambda:DeleteFunction", f"{FN}:aegc-dev-x") != "allowed"
    assert decide(c, arn, "lambda:UpdateFunctionCode", f"{FN}:aegc-dev-x", **owned) == "allowed"
    assert decide(c, arn, "lambda:GetFunction", f"{FN}:aegc-dev-x") == "allowed"
    assert decide(c, arn, "lambda:GetFunction", f"{FN}:other") != "allowed"
    assert decide(c, arn, "lambda:CreateEventSourceMapping", "*", **of_aegc, **managed) == (
        "allowed"
    )
    assert decide(c, arn, "lambda:CreateEventSourceMapping", "*",
                  **{"lambda:FunctionArn": f"{FN}:other"}, **managed) != "allowed"  # fmt: skip
    assert decide(c, arn, "lambda:DeleteEventSourceMapping", ESM, **of_aegc) == "allowed"
    assert decide(c, arn, "lambda:DeleteEventSourceMapping", ESM,
                  **{"lambda:FunctionArn": f"{FN}:other"}) != "allowed"  # fmt: skip
    role = f"arn:aws:iam::{ACCOUNT}:role/aegc/{ROLE}"
    lam = {"iam:PassedToService": "lambda.amazonaws.com"}
    assert decide(c, arn, "iam:PassRole", role, **lam) == "allowed"
    assert decide(c, arn, "iam:PassRole", role, **{"iam:PassedToService": "ec2.amazonaws.com"}) != (
        "allowed"
    )
    assert decide(c, arn, "iam:PassRole", f"arn:aws:iam::{ACCOUNT}:role/admin", **lam) != "allowed"
    repo = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository"
    assert decide(c, arn, "ecr:SetRepositoryPolicy", f"{repo}/aegc/geospatial") == "allowed"
    assert decide(c, arn, "ecr:SetRepositoryPolicy", f"{repo}/other") != "allowed"


def test_operator_can_freeze_only_its_own_lambda_pools(aws: Callable[[str], Any]) -> None:
    c, arn = principal(
        aws, iam.operator_policy(deployment="dev", region=REGION, account=ACCOUNT, bucket=BUCKET)
    )
    ours = {"lambda:FunctionArn": f"{FN}:{FUNCTION}", "aws:ResourceTag/aegc:deployment": "dev"}
    # Reads aren't conditioned (the Lambda guide says GetEventSourceMapping takes none).
    assert decide(c, arn, "lambda:GetEventSourceMapping", ESM) == "allowed"
    for action in ("lambda:UpdateEventSourceMapping",):
        assert decide(c, arn, action, ESM, **ours) == "allowed"
        for other in ({"lambda:FunctionArn": f"{FN}:aegc-prod-x"},
                      {"aws:ResourceTag/aegc:deployment": "prod"}):  # fmt: skip
            assert decide(c, arn, action, ESM, **{**ours, **other}) != "allowed", other
        assert decide(
            c, arn, action, ESM, **{"lambda:FunctionArn": ours["lambda:FunctionArn"]}
        ) != ("allowed")  # untagged
    # The cloud endpoint host (the evaluator) has the operator policy's statements.
    host = iam.endpoint_role_policy(deployment="dev", region=REGION, account=ACCOUNT, bucket=BUCKET)
    c2, arn2 = principal(aws, host, "aegc-ep")
    assert decide(c2, arn2, "lambda:UpdateEventSourceMapping", ESM, **ours) == "allowed"
    assert decide(c, arn, "lambda:ListEventSourceMappings", "*") == "allowed"
    assert decide(c, arn, "lambda:GetFunctionConfiguration", f"{FN}:{FUNCTION}") == "allowed"
    assert decide(c, arn, "lambda:GetFunctionConfiguration", f"{FN}:aegc-prod-x") != "allowed"
    assert decide(c, arn, "lambda:DeleteEventSourceMapping", ESM) != "allowed"
    assert decide(c, arn, "lambda:UpdateFunctionConfiguration", f"{FN}:{FUNCTION}") != "allowed"


def test_function_policy_fits_next_to_the_grants() -> None:
    # The longest names: a 20-character deployment, the longest pool that fits 64-char names.
    d, region = "d" * 20, "ap-southeast-4"
    pool = "e" * (64 - len(f"aegc-{d}--{region}") - len("-py314-lambda")) + "-py314-lambda"
    bucket = naming.bucket_name(ACCOUNT, region)
    doc = iam.function_policy(deployment=d, pool=pool, env="e" * 30, region=region,
                              account=ACCOUNT, bucket=bucket)  # fmt: skip
    assert iam.policy_size(doc) < iam.policy_size(
        iam.worker_policy(deployment=d, region=region, account=ACCOUNT, bucket="b" * 30)
    )
    for kind in ("deployer", "lambda-deployer", "operator", "endpoint"):
        doc = iam.policy(kind, deployment=d, region=region, account=ACCOUNT)
        assert iam.policy_size(doc) < 6_144, kind


def test_lambda_preflight_checks_account_concurrency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #81: a throttled pool runs nothing; say so at deploy.
    from types import SimpleNamespace

    from aegc import awsutil, cli

    limit = {"ConcurrentExecutions": 100, "UnreservedConcurrentExecutions": 40}

    class Lam:
        def get_account_settings(self) -> dict[str, Any]:
            return {"AccountLimit": limit}

    monkeypatch.setattr(awsutil, "client", lambda sess, name: Lam())
    pools = [SimpleNamespace(max_concurrency=30), SimpleNamespace(max_concurrency=20)]
    assert cli._lambda_preflight(None, pools) == []
    assert "50 concurrent executions; 40 of the account's are unreserved" in capsys.readouterr().err
    pools.append(SimpleNamespace(max_concurrency=60))
    (err,) = cli._lambda_preflight(None, pools)
    assert "the account allows 100" in err
