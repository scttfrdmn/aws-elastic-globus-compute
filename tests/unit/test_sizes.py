"""Size classes (#18): pool options, instance requirements, worker slots and request routing."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from aegc import engine, sizes
from aegc.infra import deployment
from aegc.infra.deployment import PoolSpec
from aegc.worker.config import WorkerConfig
from tests.unit.test_deployment import POOL
from tests.unit.test_engine import Env, submit, triple, unpack_result
from tests.unit.test_engine import env as env


def sized(name: str, cls: str, **kw: Any) -> PoolSpec:
    c = sizes.get(cls)
    base = {
        **asdict(POOL),
        "name": name,
        "size_class": cls,
        "task_cpus": c.task_cpus,
        "task_memory_gib": c.task_memory_gib,
        "memory_gib_per_vcpu_min": c.memory_gib_per_vcpu,
    }
    return PoolSpec(**{**base, **kw})  # fmt: skip


def test_slots_follow_cpus_and_memory() -> None:
    assert sizes.slots(1, 2, 2, 3.7) == 2  # c8g.large: 4 GiB nominal, ~3.7 seen
    assert sizes.slots(2, 16, 2, 15.4) == 1  # r8g.large
    assert sizes.slots(2, 16, 4, 31.3) == 2  # r8g.xlarge
    assert sizes.slots(2, 16, 8, 15.4) == 1  # memory-bound on a compute instance
    assert sizes.slots(8, 32, 16, 62.0) == 2
    assert sizes.slots(0, 0, 64, 512) == 1  # whole
    assert sizes.slots(8, 32, 2, 4) == 1  # never 0


def test_worker_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegc.worker import config

    monkeypatch.setattr(config.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(config, "host_memory_gib", lambda: 31.3)
    base = WorkerConfig(deployment="d", pool="p", region="r", task_queue_url="", bucket="b")
    assert base.slot_count == 4  # no class: one per vCPU, as before
    mem = WorkerConfig(**{**base.__dict__, "size_class": "mem", "task_cpus": 2,
                          "task_memory_gib": 16})  # fmt: skip
    assert mem.slot_count == 2
    fixed = WorkerConfig(**{**mem.__dict__, "slots": "3"})
    assert fixed.slot_count == 3


def test_pool_validation() -> None:
    with pytest.raises(ValueError, match="unknown size class"):
        sized("p-x", "huge")
    with pytest.raises(ValueError, match="8 vCPUs"):
        sized("p-large", "large")  # workers of 2-8 vCPUs can't run an 8-vCPU task
    assert sized("p-large", "large", vcpu_min=8, vcpu_max=16, max_vcpus=32).task_cpus == 8
    assert sized("p-whole", "whole", vcpu_min=8).task_cpus == 0
    with pytest.raises(ValueError, match="one worker size"):
        sized("p-whole", "whole")  # vcpu=2-8: Auto Scaling could use four 2-vCPU instances
    with pytest.raises(ValueError, match="drop slots=8"):
        sized("p-mem", "mem", slots="8")


def test_requests_and_templates_only_change_for_sized_pools() -> None:
    spec = deployment.DeploymentSpec(deployment="dev", account="1", region="us-west-2",
                                     bucket="b", pools=(POOL,))  # fmt: skip
    req = deployment._asg_request(spec, POOL, lt_name="lt", subnets=["s"])
    reqs = req["MixedInstancesPolicy"]["LaunchTemplate"]["Overrides"][0]["InstanceRequirements"]
    assert "MemoryGiBPerVCpu" not in reqs
    assert "size_class" not in deployment.worker_config(spec, POOL, "q")
    mem = sized("p-mem", "mem")
    req = deployment._asg_request(spec, mem, lt_name="lt", subnets=["s"])
    reqs = req["MixedInstancesPolicy"]["LaunchTemplate"]["Overrides"][0]["InstanceRequirements"]
    assert reqs["MemoryGiBPerVCpu"] == {"Min": 8}
    cfg = deployment.worker_config(spec, mem, "q")
    assert (cfg["size_class"], cfg["task_cpus"], cfg["task_memory_gib"]) == ("mem", 2, 16)


def manifest_pools() -> dict[str, dict[str, Any]]:
    out = {}
    large = sized("g-large", "large", vcpu_min=8, vcpu_max=16, max_vcpus=32)
    for p in (POOL, sized("g-mem", "mem"), large, sized("g-whole", "whole", vcpu_min=8)):
        out[p.name] = {**asdict(p), "capacity_type": "vcpu"}
    return out


def test_pick_by_size_or_need() -> None:
    pools = manifest_pools()
    pick = engine._pick_size
    assert pick({}, pools, "geospatial") == (POOL.name, "")  # no request: the default pool
    assert pick({"size": "mem"}, pools, "geospatial") == ("g-mem", "")
    assert pick({"size": "small"}, pools, "geospatial") == (POOL.name, "")
    assert pick({"memory_gib": 12}, pools, "geospatial") == ("g-mem", "")
    assert pick({"cpus": 4}, pools, "geospatial") == ("g-large", "")
    assert pick({"cpus": 2, "memory_gib": 4}, pools, "geospatial") == ("g-mem", "")
    # By vCPUs held: a 4-vCPU whole instance beats an 8-vCPU large slot; on a tie, the sized pool.
    w4 = {**asdict(sized("g-w4", "whole", vcpu_min=4, vcpu_max=4)), "capacity_type": "vcpu"}
    assert pick({"cpus": 2}, {"g-w4": w4, "g-large": pools["g-large"]}, "x")[0] == "g-w4"
    assert pick({"cpus": 2}, {n: pools[n] for n in ("g-whole", "g-large")}, "x")[0] == "g-large"
    name, problem = pick({"cpus": 4, "memory_gib": 64}, pools, "geospatial")
    assert name is None and "g-mem (mem: 2 vCPU, 16 GiB per task)" in problem
    name, problem = pick({"size": "medium"}, pools, "geospatial")
    assert name is None and "size='medium'" in problem


def test_capacity_per_class() -> None:
    pools = manifest_pools()
    assert engine._capacity_for(pools[POOL.name], 3) == 3
    assert engine._capacity_for(pools["g-mem"], 3) == 6
    assert engine._capacity_for(pools["g-large"], 3) == 24
    assert engine._capacity_for(pools["g-whole"], 3) == 3 * 8  # one 8-vCPU worker each


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("spec", "expect"),
    [
        ({"vcpus": 8}, "did you mean vcpus -> cpus"),
        ({"cpus": "lots"}, "cpus must be a positive number"),
        ({"memory_gib": 0}, "memory_gib must be a positive number"),
        ({"size": "mem"}, "no size='mem' pool"),
    ],
)
def test_bad_size_requests(env: Env, spec: dict[str, Any], expect: str) -> None:
    e = env.engine()
    try:
        f = submit(e, triple, 1, spec=spec)
        is_err, text = unpack_result(f.result(timeout=5))
        assert is_err and expect in text
    finally:
        e.shutdown()
