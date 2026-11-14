"""Test configuration: AWS calls go to the substrate emulator (https://github.com/scttfrdmn/substrate).

`make tools` installs the pinned substrate binary into .tools/. The `substrate` fixture from
pytest-substrate resets emulator state before each test and points boto3 at it via AWS_ENDPOINT_URL.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import pytest

REGION = "us-west-2"
ACCOUNT = "123456789012"  # substrate's default account

_TOOLS = Path(__file__).resolve().parent.parent / ".tools" / "substrate"
if "SUBSTRATE_BINARY" not in os.environ and _TOOLS.exists():
    os.environ["SUBSTRATE_BINARY"] = str(_TOOLS)


@pytest.fixture
def aws(substrate: Any) -> Callable[[str], Any]:
    """A factory for boto3 clients bound to the per-test substrate state, in REGION."""
    del substrate  # requested for its side effects (reset + env)

    def make(service: str) -> Any:
        return boto3.client(service, region_name=REGION)  # type: ignore[call-overload]

    return make
