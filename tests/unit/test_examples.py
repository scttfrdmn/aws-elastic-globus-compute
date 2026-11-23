"""The tutorials' example code (examples/), where it can be checked without AWS or Globus."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aegc import naming

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fis() -> Any:
    return _load(EXAMPLES / "spot-sweep" / "fis.py")


def test_fis_targets_aegc_worker_tags(fis: Any) -> None:
    # The template must select workers by the tags aegc really puts on them.
    assert fis.AEGC_DEPLOYMENT == naming.TAG_DEPLOYMENT
    assert fis.AEGC_POOL == naming.TAG_POOL
    req = fis.template_request(
        deployment="sweep", pool="plain-py314", role_arn="arn:aws:iam::1:role/r", client_token="t"
    )
    target = req["targets"]["SpotWorkers"]
    assert target == {
        "resourceType": "aws:ec2:spot-instance",
        "resourceTags": {"aegc:deployment": "sweep", "aegc:pool": "plain-py314"},
        "filters": [{"path": "State.Name", "values": ["running"]}],
        "selectionMode": "PERCENT(50)",
    }
    action = req["actions"]["interrupt"]
    assert action["actionId"] == "aws:ec2:send-spot-instance-interruptions"
    assert action["parameters"] == {"durationBeforeInterruption": "PT2M"}
    assert action["targets"] == {"SpotInstances": "SpotWorkers"}
    assert req["stopConditions"] == [{"source": "none"}]
    assert req["roleArn"] == "arn:aws:iam::1:role/r"
    assert req["clientToken"] == "t"
    assert fis.owned(req["tags"], "sweep")
    assert req["tags"]["pool"] == "plain-py314"


def test_fis_template_options(fis: Any) -> None:
    req = fis.template_request(deployment="d", pool="p", role_arn="a", percent=25, before="PT5M")
    assert req["targets"]["SpotWorkers"]["selectionMode"] == "PERCENT(25)"
    assert req["actions"]["interrupt"]["parameters"]["durationBeforeInterruption"] == "PT5M"
    assert req["clientToken"]  # a fresh one when not given
    for bad in (0, 101):
        with pytest.raises(ValueError, match="percent"):
            fis.template_request(deployment="d", pool="p", role_arn="a", percent=bad)


def test_fis_trust_policy_is_scoped_to_the_account(fis: Any) -> None:
    doc = fis.trust_policy("123456789012")
    (stmt,) = doc["Statement"]
    assert stmt["Principal"] == {"Service": "fis.amazonaws.com"}
    assert stmt["Action"] == "sts:AssumeRole"
    assert stmt["Condition"] == {"StringEquals": {"aws:SourceAccount": "123456789012"}}
    json.dumps(doc)  # a valid policy document


def test_fis_ownership(fis: Any) -> None:
    assert fis.owned(fis.our_tags("sweep"), "sweep")
    assert not fis.owned(fis.our_tags("sweep"), "other")
    assert not fis.owned({"deployment": "sweep"}, "sweep")
    assert not fis.owned({}, "sweep")
    # IAM role names are at most 64 characters, even for the longest deployment name.
    assert len(fis.role_name("x" * 20, "ap-southeast-2")) <= 64


def test_sweep_task_is_deterministic() -> None:
    tasks = _load(EXAMPLES / "spot-sweep" / "sweep_tasks.py")
    r = tasks.collatz_chunk(0, 10)
    assert {k: r[k] for k in ("lo", "hi", "best_n", "best_steps")} == {
        "lo": 1,
        "hi": 10,
        "best_n": 9,
        "best_steps": 19,
    }
    assert r["total_steps"] == sum([0, 1, 7, 2, 5, 8, 16, 3, 19, 6])
    again = tasks.collatz_chunk(3, 1000)
    assert {k: v for k, v in again.items() if k != "seconds"} == {
        k: v for k, v in tasks.collatz_chunk(3, 1000).items() if k != "seconds"
    }


def test_flow_definitions_are_json() -> None:
    for path in (
        EXAMPLES / "sentinel2-ndvi" / "nightly.json",
        EXAMPLES / "two-endpoints" / "two_endpoints.json",
        EXAMPLES / "two-endpoints" / "input.json",
    ):
        doc = json.loads(path.read_text())
        for state in doc.get("States", {}).values():
            assert state["ActionUrl"] == "https://compute.actions.globus.org/v3"
            assert "resource_specification" not in state["Parameters"]


def _functions(path: Path) -> dict[str, str]:
    """Top-level function sources in an example module, by name."""
    import ast

    text = path.read_text()
    tree = ast.parse(text)
    return {
        n.name: ast.get_source_segment(text, n) or ""
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }


def test_tutorial_code_matches_the_examples() -> None:
    # Each function shown in a tutorial page is the example's own code, so they can't drift.
    import re

    funcs: dict[str, str] = {}
    for path in EXAMPLES.glob("*/*.py"):
        funcs.update(_functions(path))
    shown = 0
    for page in (EXAMPLES.parent / "docs" / "tutorials").glob("*.md"):
        for block in re.findall(r"```python\n(.*?)```", page.read_text(), flags=re.S):
            m = re.match(r"def (\w+)\(", block)
            if m:
                assert block.strip() == funcs[m.group(1)].strip(), f"{page.name}: {m.group(1)}"
                shown += 1
    assert shown >= 5
