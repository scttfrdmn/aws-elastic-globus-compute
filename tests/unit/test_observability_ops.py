"""v0.4 operator side: group metrics at deploy, `aegc metrics`, `aegc provenance`, status (#39,
#40, #69), and role policies that report what changed."""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import pytest

from aegc import awsutil, cli, engine, naming, observability, ops
from aegc.infra import Report, iam
from aegc.infra import deployment as dep
from aegc.infra.deployment import Clients, DeploymentSpec
from aegc.infra.iam import Grant
from tests.conftest import ACCOUNT, REGION
from tests.unit.test_deployment import POOL
from tests.unit.test_iam import decide, principal
from tests.unit.test_sizes import sized

ROLE = naming.worker_role("dev", REGION)
GRANTS = (Grant.parse("read=s3://open-data/in/"), Grant.parse("write=s3://lab/out/"))


@pytest.fixture
def env(aws: Callable[[str], Any]) -> tuple[Clients, DeploymentSpec]:
    c = Clients(
        iam=aws("iam"),
        ec2=aws("ec2"),
        autoscaling=aws("autoscaling"),
        sqs=aws("sqs"),
        logs=aws("logs"),
        ssm=aws("ssm"),
    )
    spec = DeploymentSpec(
        deployment="dev",
        account=ACCOUNT,
        region=REGION,
        bucket=naming.bucket_name(ACCOUNT, REGION),
        pools=(POOL,),
    )
    return c, spec


def outcomes(r: Report) -> dict[tuple[str, str], str]:
    return {(a.resource, a.name): a.outcome for a in r.actions}


# --- role policies (#69) -------------------------------------------------------------------------


def test_unchanged_redeploy_reports_policies_as_exists(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    with_grants = DeploymentSpec(**{**spec.__dict__, "grants": GRANTS})
    # POOL predates 0.4 (unisolated), so the worker role carries the grants too (#32).
    worker = ("iam role policy", iam.WORKER_POLICY)
    grants = ("iam role policy", f"{iam.GRANTS_POLICY} (worker role)")
    task_grants = ("iam role policy", f"{iam.GRANTS_POLICY} (task role)")

    # A dry run of a new deployment says what it would create, and creates nothing.
    r = outcomes(dep.deploy(c, with_grants, dry_run=True))
    assert (r[worker], r[grants]) == ("would-create", "would-create")
    with pytest.raises(c.iam.exceptions.NoSuchEntityException):
        c.iam.get_role(RoleName=ROLE)

    r = outcomes(dep.deploy(c, with_grants))
    assert (r[worker], r[grants]) == ("created", "created")
    r = outcomes(dep.deploy(c, with_grants))
    assert (r[worker], r[grants]) == ("exists", "exists")  # not "updated" every time
    assert r[task_grants] == "exists" and r[("iam role policy", iam.TASK_POLICY)] == "exists"

    # Dry runs of a change report it and change nothing.
    fewer = DeploymentSpec(**{**spec.__dict__, "grants": GRANTS[:1]})
    r = outcomes(dep.deploy(c, fewer, dry_run=True))
    assert (r[worker], r[grants]) == ("exists", "would-update")
    r = outcomes(dep.deploy(c, spec, dry_run=True))
    assert r[grants] == "would-delete"
    doc = c.iam.get_role_policy(RoleName=ROLE, PolicyName=iam.GRANTS_POLICY)["PolicyDocument"]
    assert dep._policy_document(doc) == with_grants.grants_policy()

    r = outcomes(dep.deploy(c, fewer))
    assert r[grants] == "updated"
    r = outcomes(dep.deploy(c, spec))
    assert r[grants] == "deleted"
    assert grants not in outcomes(dep.deploy(c, spec))


class _Iam:
    """GetRolePolicy returning a fixed document; records puts."""

    class exceptions:
        pass

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.puts: list[str] = []

    def get_role_policy(self, **_: Any) -> dict[str, Any]:
        return {"PolicyDocument": self.raw}

    def put_role_policy(self, *, PolicyDocument: str, **_: Any) -> None:
        self.puts.append(PolicyDocument)


@pytest.mark.parametrize(
    "encode",
    [
        lambda d: d,  # boto3 decodes the document
        json.dumps,
        lambda d: urllib.parse.quote(json.dumps(d)),  # as IAM sends it
        lambda d: urllib.parse.quote(json.dumps(d, indent=2)),
    ],
)
def test_policy_comparison_reads_every_encoding(encode: Callable[[Any], Any]) -> None:
    doc = iam.grants_policy(GRANTS, deployment="dev", account=ACCOUNT)
    stub = _Iam(encode(doc))
    c = Clients(iam=stub, ec2=None, autoscaling=None, sqs=None, logs=None, ssm=None)
    report = Report()
    assert dep._put_role_policy(c, ROLE, iam.GRANTS_POLICY, doc, report, dry_run=False) == "exists"
    assert stub.puts == []
    other = iam.grants_policy(GRANTS[:1], deployment="dev", account=ACCOUNT)
    assert dep._put_role_policy(c, ROLE, iam.GRANTS_POLICY, other, report, dry_run=True) == (
        "would-update"
    )
    assert stub.puts == []
    stub.raw = "%%% not json"
    assert dep._put_role_policy(c, ROLE, iam.GRANTS_POLICY, doc, report, dry_run=False) == "updated"
    assert len(stub.puts) == 1


# --- Auto Scaling group metrics (#39) ------------------------------------------------------------


def test_deploy_enables_group_metrics(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    r = outcomes(dep.deploy(c, spec))
    assert r[("auto scaling group metrics", "aegc-dev-sci-py314")] == "updated"
    (g,) = c.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=["aegc-dev-sci-py314"])[
        "AutoScalingGroups"
    ]
    assert {m["Granularity"] for m in g["EnabledMetrics"]} == {"1Minute"}


def test_group_metrics_are_best_effort(env: tuple[Clients, DeploymentSpec]) -> None:
    # A deployer policy from before 0.4 is refused EnableMetricsCollection: the deploy still
    # completes, and says the metrics aren't on.
    from botocore.exceptions import ClientError

    c, spec = env

    def denied(**_: Any) -> None:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}},
                          "EnableMetricsCollection")  # fmt: skip

    c.autoscaling.meta.events.register("before-call.auto-scaling.EnableMetricsCollection", denied)
    report = dep.deploy(c, spec)
    (a,) = [a for a in report.actions if a.resource == "auto scaling group metrics"]
    assert a.outcome == "kept" and "not enabled" in a.detail
    assert dep.read_manifest(c.ssm, "dev")["pools"][POOL.name]


def test_deployer_may_enable_group_metrics_on_aegc_groups(aws: Callable[[str], Any]) -> None:
    c, arn = principal(aws, iam.deployer_policy(region=REGION, account=ACCOUNT))
    action = "autoscaling:EnableMetricsCollection"
    managed = {"autoscaling:ResourceTag/aegc:managed": "true"}
    assert decide(c, arn, action, "*", **managed) == "allowed"
    assert decide(c, arn, action, "*") != "allowed"


# --- aegc metrics --------------------------------------------------------------------------------


class FakeCloudWatch:
    """GetMetricData over a fixed table, two pages per call (substrate returns no data for
    custom metrics; see tests/substrate_gaps.py)."""

    def __init__(self, table: dict[tuple[str, str, str], list[float]]) -> None:
        self.table = table  # (metric, stat, outcome or pool-level "") -> values
        self.calls: list[dict[str, Any]] = []

    def get_metric_data(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        queries = kw["MetricDataQueries"]
        half = len(queries) // 2
        page = queries[half:] if kw.get("NextToken") else queries[:half]
        results = []
        for q in page:
            m = q["MetricStat"]["Metric"]
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            key = (m["MetricName"], q["MetricStat"]["Stat"], dims.get("Outcome", ""))
            results.append({"Id": q["Id"], "Values": self.table.get(key, [])})
        return {"MetricDataResults": results, **({} if kw.get("NextToken") else {"NextToken": "2"})}


TABLE = {
    ("TasksCompleted", "Sum", ""): [3, 2],
    ("TasksCompleted", "Sum", "ok"): [3, 1],
    ("TasksCompleted", "Sum", "task_error"): [1],
    ("TasksFailed", "Sum", ""): [1],
    ("TasksFailed", "Sum", "walltime"): [1],
    ("ExecSeconds", "Sum", ""): [30, 30],
    ("ExecSeconds", "SampleCount", ""): [4, 2],
    ("ExecSeconds", "Maximum", ""): [12, 25],
    ("QueueWaitSeconds", "Sum", ""): [60],
    ("QueueWaitSeconds", "SampleCount", ""): [6],
    ("QueueWaitSeconds", "Maximum", ""): [40],
    ("BootToFirstClaimSeconds", "Sum", ""): [90],
    ("BootToFirstClaimSeconds", "SampleCount", ""): [1],
    ("Interruptions", "Sum", ""): [1],
    ("Releases", "Sum", ""): [2],
    ("WorkerExit", "Sum", ""): [1],
    ("GroupInServiceCapacity", "Maximum", ""): [2, 4],
    ("GroupDesiredCapacity", "Maximum", ""): [4],
}


def test_metrics_summary_queries_and_totals() -> None:
    cw = FakeCloudWatch(TABLE)
    manifest = {"pools": {POOL.name: {**asdict(POOL), "capacity_type": "vcpu",
                                      "auto_scaling_group": "aegc-dev-sci-py314"}}}  # fmt: skip
    s = observability.metrics(
        cw=cw, manifest=manifest, deployment="dev", region=REGION, start=0, end=3600
    )
    assert len(cw.calls) == 2 and cw.calls[1]["NextToken"] == "2"  # follows pages
    queries = cw.calls[0]["MetricDataQueries"]
    assert {q["MetricStat"]["Period"] for q in queries} == {60}
    for q in queries:
        m = q["MetricStat"]["Metric"]
        names = {d["Name"] for d in m["Dimensions"]}
        if m["Namespace"] == "aegc":
            assert names in ({"Deployment", "Pool"}, {"Deployment", "Pool", "Outcome"})
        else:
            assert (m["Namespace"], names) == ("AWS/AutoScaling", {"AutoScalingGroupName"})
    (p,) = s.pools
    assert (p.completed, p.failed) == (5, 1)
    assert p.by_outcome == {"ok": 4, "task_error": 1, "walltime": 1}
    assert (p.exec_avg_s, p.exec_max_s) == (10, 25)
    assert (p.queue_wait_avg_s, p.queue_wait_max_s, p.boot_to_first_claim_avg_s) == (10, 40, 90)
    assert (p.interruptions, p.releases, p.poison, p.worker_exits) == (1, 2, 0, 1)
    assert (p.in_service_max, p.desired_max, p.unit) == (4, 4, "vCPUs")
    text = s.render()
    assert "pool sci-py314: 5 task(s) completed (4 ok, 1 task_error), 1 failed (1 walltime)" in text
    assert "exec avg 10.0 s, max 25.0 s" in text and "vCPUs in service max 4" in text
    assert "total: 5 completed, 1 failed" in text and "no data" not in text
    assert json.loads(json.dumps(s.to_dict()))["pools"][0]["completed"] == 5

    empty = observability.metrics(
        cw=FakeCloudWatch({}), manifest=manifest, deployment="dev", region=REGION,
        start=0, end=7 * 86400,
    )  # fmt: skip
    assert empty.period_s == 3600 and "no data in this window" in empty.render()


def test_metrics_cli(
    env: tuple[Clients, DeploymentSpec],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    c, spec = env
    dep.deploy(c, spec)
    cw = FakeCloudWatch(TABLE)
    real = awsutil.client
    monkeypatch.setattr(
        awsutil, "client", lambda sess, svc: cw if svc == "cloudwatch" else real(sess, svc)
    )
    assert cli.main(["--region", REGION, "metrics", "dev", "--since", "1d"]) == 0
    out = capsys.readouterr().out
    assert "metrics for dev (us-west-2)" in out and "pool sci-py314: 5 task(s)" in out
    assert cw.calls[0]["EndTime"] - cw.calls[0]["StartTime"] == pytest.approx(86400, abs=5)
    assert cli.main(["--region", REGION, "metrics", "dev", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pools"][0]["failed"] == 1
    with pytest.raises(SystemExit):
        cli.main(["--region", REGION, "metrics", "dev", "--since", "soon"])


def test_operator_may_read_metrics_and_its_own_logs(aws: Callable[[str], Any]) -> None:
    doc = iam.operator_policy(
        deployment="dev", region=REGION, account=ACCOUNT, bucket=naming.bucket_name(ACCOUNT, REGION)
    )
    c, arn = principal(aws, doc)
    logs = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:"
    assert decide(c, arn, "cloudwatch:GetMetricData", "*") == "allowed"
    assert decide(c, arn, "logs:FilterLogEvents", f"{logs}/aegc/dev:*") == "allowed"
    assert decide(c, arn, "logs:FilterLogEvents", f"{logs}/aegc/dev") == "allowed"
    assert decide(c, arn, "logs:FilterLogEvents", f"{logs}/aegc/prod:*") != "allowed"
    assert decide(c, arn, "logs:FilterLogEvents", f"{logs}/aegc/dev-other:*") != "allowed"
    assert decide(c, arn, "logs:GetLogEvents", f"{logs}/aegc/dev:*") != "allowed"


# --- aegc provenance -----------------------------------------------------------------------------


def finished(tid: str, attempt: int, outcome: str, **kw: Any) -> str:
    return json.dumps(
        {
            "ts": f"2026-11-16T10:0{attempt}:00.000Z", "level": "INFO", "logger": "aegc.worker",
            "msg": f"task finished: {tid}:{attempt} {outcome} in 1.0 s", "event": "task_finished",
            "task_uuid": tid, "attempt": attempt, "etid": f"{tid}:{attempt}",
            "receive_count": attempt,
            "outcome": outcome, "posted": outcome != "released", "exec_s": 1.0, "queue_wait_s": 2.0,
            "provenance": {
                "image": "r/aegc/geospatial@sha256:" + "a" * 64, "instance_type": "c8g.large",
                "availability_zone": "us-west-2a", "lifecycle": "spot", "instance_id": "i-1",
                "vcpus": 2, "slots": 2, "pool": "p", "est_cost_usd": 0.000012,
                "est_cost_source": "static", "timings": {"exec_s": 1.0, "queue_wait_s": 2.0},
            },
            **kw,
        }
    )  # fmt: skip


def test_provenance_from_the_logs(
    aws: Callable[[str], Any], capsys: pytest.CaptureFixture[str]
) -> None:
    logs = aws("logs")
    logs.create_log_group(logGroupName="/aegc/dev")
    logs.create_log_stream(logGroupName="/aegc/dev", logStreamName="p/i-1")
    tid, other = str(uuid.uuid4()), str(uuid.uuid4())
    now = int(time.time() * 1000)
    lines = [
        finished(tid, 2, "ok"),
        f"task output that mentions {tid}",  # a task's stdout: not JSON
        json.dumps({"_aws": {}, "Deployment": "dev", "note": tid}),  # not a task_finished line
        finished(tid, 1, "released"),
        finished(other, 1, "ok"),
    ]
    logs.put_log_events(
        logGroupName="/aegc/dev",
        logStreamName="p/i-1",
        logEvents=[{"timestamp": now + i, "message": m} for i, m in enumerate(lines)],
    )
    recs = observability.provenance(logs=logs, deployment="dev", task_uuid=tid, start=0)
    assert [(r["attempt"], r["outcome"]) for r in recs] == [(1, "released"), (2, "ok")]
    assert recs[1]["log_stream"] == "p/i-1"
    assert recs[1]["provenance"]["image"].endswith("a" * 64)

    argv = ["--region", REGION, "provenance", "dev"]
    assert cli.main([*argv, tid]) == 0
    out = capsys.readouterr().out
    assert f"task {tid} in dev: 2 attempt(s) finished" in out
    assert "attempt 1 (delivery 1): released in 1.0 s, queued 2.0 s (released: not reported)" in out
    assert "c8g.large spot us-west-2a i-1 | pool p, 2 slot(s), 2 vCPUs" in out
    assert "estimated cost $0.000012 (static)" in out
    assert cli.main([*argv, tid, "--json"]) == 0
    assert [r["attempt"] for r in json.loads(capsys.readouterr().out)] == [1, 2]
    assert cli.main([*argv, str(uuid.uuid4())]) == 1
    assert "no task_finished line" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main([*argv, '" OR "x'])  # only a UUID goes into the filter pattern


# --- aegc status (#69) ---------------------------------------------------------------------------


def test_status_shows_grants_and_size_classes(env: tuple[Clients, DeploymentSpec]) -> None:
    c, spec = env
    mem = sized("geo-mem", "mem")
    dep.deploy(c, DeploymentSpec(**{**spec.__dict__, "pools": (POOL, mem), "grants": GRANTS}))
    st = ops.status(
        ssm=c.ssm, sqs=c.sqs, autoscaling=c.autoscaling, deployment="dev", region=REGION
    )
    text = st.render()
    assert "grants: read s3://open-data/in/, write s3://lab/out/" in text
    assert "size mem: 2 vCPU(s), 16 GiB per task" in text
    assert "size per-vCPU: 1 vCPU(s), 2 GiB per task" in text  # a pool deployed without size=
    d = json.loads(json.dumps(st.to_dict()))
    assert d["grants"] == [g.to_dict() for g in GRANTS]
    pools = {p["name"]: p for p in d["pools"]}
    assert (pools["geo-mem"]["size_class"], pools["geo-mem"]["task_vcpus"]) == ("mem", 2)
    assert pools["geo-mem"]["task_memory_gib"] == 16

    dep.deploy(c, spec)  # --grant none
    st = ops.status(ssm=c.ssm, sqs=c.sqs, autoscaling=c.autoscaling, deployment="dev",
                    region=REGION)  # fmt: skip
    assert "grants: none" in st.render() and st.grants == []


def test_whole_instance_size_text() -> None:
    p = ops.PoolStatus(name="w", env="e", python_minor="3.14", size_class="whole",
                       task_vcpus=8, task_memory_gib=16)  # fmt: skip
    assert p.size_text == "size whole: one task per instance, at least 8 vCPUs, 16 GiB"


# --- engine texts (#69) --------------------------------------------------------------------------


def test_engine_problems_name_task_options_not_resource_specification() -> None:
    _, problem = engine._strip_mpi_keys({"num_nodes": "many"})
    assert problem == "num_nodes, ranks_per_node and num_ranks must be numbers"
    assert "resource_specification" not in problem
