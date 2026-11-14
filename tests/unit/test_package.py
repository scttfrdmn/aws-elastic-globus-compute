import pytest

import aegc
from aegc import cli


def test_version_is_0x() -> None:
    major, minor, _ = aegc.__version__.split(".")
    assert major == "0", "stay on 0.x until the owner says otherwise"
    assert minor.isdigit()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert aegc.__version__ in capsys.readouterr().out
