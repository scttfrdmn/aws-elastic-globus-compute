"""HealthOmics as a flow step (#84): the omics grant and the task functions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from aegc import omics
from aegc.infra import iam
from tests.conftest import ACCOUNT, REGION
from tests.unit.test_iam import decide, principal

ROLE = f"arn:aws:iam::{ACCOUNT}:role/omics-runner"


def test_omics_grants_parse_and_round_trip() -> None:
    g = iam.Grant.parse(f"omics=1234567,role={ROLE}")
    assert (g.access, g.workflow, g.role) == ("omics", "1234567", ROLE)
    assert iam.Grant.from_dict(g.to_dict()) == g
    assert iam.Grant.parse(f"omics=ready2run:9876543,role={ROLE}").workflow == "ready2run:9876543"
    for bad in (
        "omics=1234567",
        f"omics=12*,role={ROLE}",
        "omics=1234567,role=arn:aws:iam::1:role/*",
    ):
        with pytest.raises(ValueError):
            iam.Grant.parse(bad)
    assert not g.covers("b", "k")  # never an S3 grant


def test_omics_grant_policy(aws: Callable[[str], Any]) -> None:
    grants = [iam.Grant.parse(f"omics=1234567,role={ROLE}"),
              iam.Grant.parse("read=s3://lab-data/in/")]  # fmt: skip
    doc = iam.grants_policy(grants, deployment="dev", account=ACCOUNT, region=REGION)
    c, arn = principal(aws, doc)
    wf = f"arn:aws:omics:{REGION}:{ACCOUNT}:workflow/1234567"
    run = f"arn:aws:omics:{REGION}:{ACCOUNT}:run/7777777"
    tagged = {"aws:RequestTag/aegc:deployment": "dev"}
    assert decide(c, arn, "omics:StartRun", wf, **tagged) == "allowed"
    assert (
        decide(c, arn, "omics:StartRun", wf, **{"aws:RequestTag/aegc:deployment": "prod"})
        != "allowed"
    )
    assert (
        decide(c, arn, "omics:StartRun", f"arn:aws:omics:{REGION}:{ACCOUNT}:workflow/1")
        != "allowed"
    )
    assert (
        decide(c, arn, "omics:GetRun", run, **{"aws:ResourceTag/aegc:deployment": "dev"})
        == "allowed"
    )
    assert (
        decide(c, arn, "omics:GetRun", run, **{"aws:ResourceTag/aegc:deployment": "x"}) != "allowed"
    )
    passed = {"iam:PassedToService": "omics.amazonaws.com"}
    assert decide(c, arn, "iam:PassRole", ROLE, **passed) == "allowed"
    assert (
        decide(c, arn, "iam:PassRole", ROLE, **{"iam:PassedToService": "ec2.amazonaws.com"})
        != "allowed"
    )
    assert decide(c, arn, "s3:GetObject", "arn:aws:s3:::lab-data/in/x") == "allowed"


class Omics:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start_run(self, **kw: Any) -> dict[str, Any]:
        self.started.append(kw)
        return {"id": "7777777", "arn": "arn:run", "status": "PENDING"}

    def get_run(self, id: str) -> dict[str, Any]:
        return {"id": id, "status": "COMPLETED", "runOutputUri": "s3://out/7777777/"}


def test_start_run_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    fake = Omics()
    monkeypatch.setattr(boto3, "client", lambda name: fake)
    monkeypatch.setenv("AEGC_DEPLOYMENT", "dev")
    r = omics.start_run("1234567", ROLE, "s3://out/", {"region": "chr22:1-10"})
    (kw,) = fake.started
    assert kw["tags"] == {"aegc:deployment": "dev"} and kw["workflowType"] == "PRIVATE"
    assert r == {
        "run_id": "7777777",
        "arn": "arn:run",
        "status": "PENDING",
        "output_uri": "s3://out/",
    }
    omics.start_run("ready2run:9876543", ROLE, "s3://out/")
    assert fake.started[1]["workflowType"] == "READY2RUN" and "storageType" not in fake.started[1]
    s = omics.run_status("7777777")
    assert s["done"] and not s["failed"] and s["output_uri"] == "s3://out/7777777/"
    monkeypatch.delenv("AEGC_DEPLOYMENT")
    with pytest.raises(ValueError, match="deployment"):
        omics.start_run("1234567", ROLE, "s3://out/")
