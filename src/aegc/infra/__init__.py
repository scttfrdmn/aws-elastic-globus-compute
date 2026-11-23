"""Idempotent boto3 ``ensure_*`` / ``destroy_*`` functions for aegc's AWS resources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Outcome = Literal[
    "created",
    "exists",
    "updated",
    "deleted",
    "would-create",
    "would-update",
    "would-delete",
    "kept",
    "remaining",
    "skipped",  # left out on purpose, e.g. a subnet in a zone without the pool's instance types
]


@dataclass(frozen=True)
class Action:
    resource: str
    name: str
    outcome: Outcome
    detail: str = ""

    def __str__(self) -> str:
        tail = f" ({self.detail})" if self.detail else ""
        return f"{self.outcome:>12}  {self.resource:<26} {self.name}{tail}"


@dataclass
class Report:
    actions: list[Action] = field(default_factory=list)

    def add(self, resource: str, name: str, outcome: Outcome, detail: str = "") -> None:
        self.actions.append(Action(resource, name, outcome, detail))

    def __str__(self) -> str:
        return "\n".join(str(a) for a in self.actions)


class OwnershipError(RuntimeError):
    """A resource with aegc's name exists but doesn't carry aegc's ownership tags."""
