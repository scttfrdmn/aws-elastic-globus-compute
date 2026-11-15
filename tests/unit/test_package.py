import pytest

import aegc
from aegc import cli


def test_version_is_0x() -> None:
    major, minor, _ = aegc.__version__.split(".", 2)  # e.g. 0.2.0 or 0.2.0.dev0
    assert major == "0", "stay on 0.x until the owner says otherwise"
    assert minor.isdigit()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert aegc.__version__ in capsys.readouterr().out


def test_cli_iam_policy_offline(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    rc = cli.main(
        ["--region", "us-west-2", "iam-policy", "worker", "--deployment", "dev", "--account", "1"]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert {s["Sid"] for s in doc["Statement"]} >= {"ConsumeTasks", "SelfDrain"}
