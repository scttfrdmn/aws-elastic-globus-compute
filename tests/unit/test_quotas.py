from collections.abc import Callable
from typing import Any

import pytest

from aegc import quotas


def pool(name: str, *, spot: bool = True, max_workers: int = 3, vmin: int = 2, vmax: int = 16):
    return {"name": name, "spot": spot, "max_workers": max_workers, "vcpu_min": vmin,
            "vcpu_max": vmax}  # fmt: skip


def test_on_demand_quota_from_service_quotas(aws: Callable[[str], Any]) -> None:
    (c,) = quotas.check([pool("od", spot=False)], sq=aws("service-quotas"), cw=aws("cloudwatch"))
    assert (c.kind, c.quota, c.used, c.worst_case) == ("on-demand", 32.0, 0.0, 48)
    assert c.errors == []
    (w,) = c.warnings  # 3 x 16 > 32
    assert "can ask for up to 48" in w and "--quota-code L-1216C47A --desired-value 48" in w


@pytest.mark.xfail(strict=True, reason="substrate#1498: no Spot vCPU quota L-34B43A08")
def test_spot_quota_from_service_quotas(aws: Callable[[str], Any]) -> None:
    (c,) = quotas.check([pool("geo")], sq=aws("service-quotas"), cw=aws("cloudwatch"))
    assert c.quota is not None


def test_unreadable_quota_is_a_warning_not_an_error(aws: Callable[[str], Any]) -> None:
    # Today's substrate has no Spot quota (substrate#1498): the same path as AccessDenied.
    (c,) = quotas.check([pool("geo")], sq=aws("service-quotas"), cw=aws("cloudwatch"))
    if c.quota is None:
        assert c.errors == [] and "couldn't read the spot vCPU quota" in c.warnings[0]


def test_default_spot_quota_of_5() -> None:
    # A new account: 5 Spot vCPUs. One 2-vCPU worker fits; 3 x 16 doesn't; an 8-vCPU minimum never.
    ok = quotas.QuotaCheck("spot", 5.0, 0.0, 48, {"geo": 2})
    assert ok.errors == [] and "leaves 5" in ok.warnings[0]
    never = quotas.QuotaCheck("spot", 5.0, 0.0, 48, {"geo": 2, "big": 8})
    assert len(never.errors) == 1 and never.errors[0].startswith("pool big:")
    assert "--quota-code L-34B43A08" in never.errors[0]
    roomy = quotas.QuotaCheck("spot", 640.0, 6.0, 48, {"geo": 2})
    assert roomy.errors == [] and roomy.warnings == []
    assert "spot vCPU quota 640, in use 6; pools can use up to 48" in roomy.render()
