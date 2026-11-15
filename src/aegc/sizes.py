"""Size classes: pools whose workers fit tasks bigger than one vCPU and 2 GiB (#18).

A pool option ``size=<class>`` sets what one task gets. Auto Scaling still counts vCPUs; the class
sets each task's vCPUs, the instances' minimum memory per vCPU (so a task's vCPUs come with its
memory), and the workers' slots (``slots=auto``).

Tasks pick a class with ``resource_specification={"size": "mem"}``, or by need with
``{"cpus": 4, "memory_gib": 24}``: the cheapest class that fits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Tasks don't get all of an instance's nominal memory (the kernel, Docker and the worker keep
#: some), so a slot needs this fraction of its share.
MEMORY_SLACK = 0.9


@dataclass(frozen=True)
class SizeClass:
    name: str
    task_cpus: int  # vCPUs per task (0: the whole instance)
    task_memory_gib: float  # memory per task
    memory_gib_per_vcpu: float  # InstanceRequirements MemoryGiBPerVCpu.Min (0: not set)
    summary: str


SIZES: dict[str, SizeClass] = {
    c.name: c
    for c in (
        SizeClass("small", 1, 2, 0, "1 vCPU, 2 GiB per task (the default)"),
        SizeClass("medium", 2, 8, 4, "2 vCPUs, 8 GiB per task"),
        SizeClass("mem", 2, 16, 8, "2 vCPUs, 16 GiB per task"),
        SizeClass("large", 8, 32, 4, "8 vCPUs, 32 GiB per task"),
        SizeClass("whole", 0, 0, 0, "one task per instance"),
    )
}
DEFAULT = "small"


def get(name: str) -> SizeClass:
    try:
        return SIZES[name or DEFAULT]
    except KeyError:
        raise ValueError(
            f"unknown size class {name!r}; one of: "
            + ", ".join(f"{c.name} ({c.summary})" for c in SIZES.values())
        ) from None


def slots(task_cpus: int, task_memory_gib: float, cpus: int, memory_gib: float) -> int:
    """Tasks one worker of ``cpus`` vCPUs and ``memory_gib`` runs at once (at least 1)."""
    if task_cpus <= 0:  # whole
        return 1
    n = cpus // task_cpus
    if task_memory_gib > 0:
        n = min(n, int(memory_gib / (task_memory_gib * MEMORY_SLACK)))
    return max(1, n)


def per_task(pool: dict[str, Any]) -> tuple[float, float]:
    """(vCPUs, GiB) one task of a manifest pool is sure to get."""
    vcpus = float(pool.get("vcpu_min", 2))
    mem = float(pool.get("memory_mib_min", 4096)) / 1024
    if pool.get("size_class") == "whole":
        return vcpus, mem
    slots = str(pool.get("slots", "auto"))
    if not pool.get("size_class") and slots != "auto":  # fixed slots share the smallest worker
        n = max(1, int(slots))
        return vcpus / n, mem / n
    cpus = float(pool.get("task_cpus") or 1)
    return cpus, float(pool.get("task_memory_gib") or 2 * cpus)
