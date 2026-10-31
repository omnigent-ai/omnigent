"""UI test processes keep provider config and Playwright artifacts to themselves."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
_ROOT = Path(__file__).resolve().parents[2]
_USER_CONFIG = "user: real\n"
_USER_SECRETS = '{"provider-key": "real"}\n'

# Each worker installs its own mock provider, then waits until the other worker
# has too, so a shared config file would be overwritten or refuse the backup.
_OVERLAPPING_MOCK_CONFIG = """
import json, os, time
from pathlib import Path

from omnigent.config import global_config_path
from tests.helpers.ui_configuration import temp_omnigent_mock_config


def test_{name}():
    worker = os.environ["PYTEST_XDIST_WORKER"]
    records = Path(os.environ["ISOLATION_RECORDS"])
    seeded = global_config_path().read_text()
    with temp_omnigent_mock_config(f"http://mock-{{worker}}", "claude"):
        (records / f"{{worker}}.arrived").touch()
        deadline = time.monotonic() + 30
        while len(list(records.glob("*.arrived"))) < 2:
            assert time.monotonic() < deadline, "the two workers never overlapped"
            time.sleep(0.05)
        config = global_config_path().read_text()
    secrets = (global_config_path().parent / "secrets.json").read_text()
    (records / f"{{worker}}.json").write_text(json.dumps({{
        "path": str(global_config_path()), "seeded": seeded, "config": config, "secrets": secrets
    }}))
"""


def _configure(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run nested pytest with a synthetic user HOME and the UI config hooks."""
    home = pytester.path / "home"
    (home / ".omnigent").mkdir(parents=True)
    (home / ".omnigent" / "config.yaml").write_text(_USER_CONFIG)
    (home / ".omnigent" / "secrets.json").write_text(_USER_SECRETS)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(_ROOT), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("ISOLATION_RECORDS", str(pytester.path))
    for name in (
        "CI",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_UI_TESTS_CONFIG_HOME",
        "OMNIGENT_REPRO_SERVER_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    pytester.makeini("[pytest]\n")
    pytester.makeconftest("""
from tests.helpers import ui_timings
from tests.helpers.ui_configuration import pytest_configure, pytest_unconfigure


def pytest_addoption(parser):
    ui_timings.pytest_addoption(parser)
    parser.addoption("--ui-base-url")
""")
    return home


def _assert_user_files_untouched(home: Path) -> None:
    user_dir = home / ".omnigent"
    assert sorted(path.name for path in user_dir.iterdir()) == ["config.yaml", "secrets.json"]
    assert (user_dir / "config.yaml").read_text() == _USER_CONFIG
    assert (user_dir / "secrets.json").read_text() == _USER_SECRETS


def test_parallel_workers_get_separate_config_homes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure(pytester, monkeypatch)
    pytester.makepyfile(
        test_first=_OVERLAPPING_MOCK_CONFIG.format(name="first"),
        test_second=_OVERLAPPING_MOCK_CONFIG.format(name="second"),
    )
    result = pytester.runpytest_subprocess("-q", "-p", "xdist.plugin", "-n2", "--dist=loadfile")
    result.assert_outcomes(passed=2)
    records = {
        worker: json.loads((pytester.path / f"{worker}.json").read_text())
        for worker in ("gw0", "gw1")
    }
    for worker, record in records.items():
        assert record["seeded"] == _USER_CONFIG
        assert record["secrets"] == _USER_SECRETS
        assert f"http://mock-{worker}" in record["config"]
        assert not Path(record["path"]).is_relative_to(home)
        assert not Path(record["path"]).parent.exists()
    assert records["gw0"]["path"] != records["gw1"]["path"]
    _assert_user_files_untouched(home)


@pytest.mark.parametrize("mode", ["default", "empty", "caller", "external", "repro"])
def test_serial_run_isolates_only_its_own_stack(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    home = _configure(pytester, monkeypatch)
    caller_home = pytester.path / "caller"
    args = ["-q"]
    if mode == "empty":
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "")
    elif mode == "caller":
        caller_home.mkdir()
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(caller_home))
    elif mode == "external":
        monkeypatch.setenv("OMNIGENT_E2E_ALLOW_DEV_BASE_URL", "1")
        args.append("--ui-base-url=http://127.0.0.1:59999")
    elif mode == "repro":
        monkeypatch.setenv("OMNIGENT_REPRO_SERVER_URL", "http://127.0.0.1:59998")
    pytester.makepyfile("""
import json, os
from pathlib import Path

def test_record():
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    copied = {}
    if config_home and config_home != os.environ["HOME"]:
        for name in ("config.yaml", "secrets.json"):
            path = Path(config_home, name)
            if path.exists():
                copied[name] = [path.read_text(), oct(path.stat().st_mode & 0o777)]
    Path(os.environ["ISOLATION_RECORDS"], "serial.json").write_text(
        json.dumps({"home": config_home, "copied": copied})
    )
""")
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(passed=1)
    record = json.loads((pytester.path / "serial.json").read_text())
    if mode in ("default", "empty"):
        assert not Path(record["home"]).is_relative_to(home)
        assert not Path(record["home"]).exists()
        assert record["copied"] == {
            "config.yaml": [_USER_CONFIG, "0o600"],
            "secrets.json": [_USER_SECRETS, "0o600"],
        }
    elif mode == "caller":
        assert record["home"] == str(caller_home)
    else:
        assert record["home"] is None
    _assert_user_files_untouched(home)


def test_parallel_workers_keep_separate_playwright_output(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(pytester, monkeypatch)
    output = pytester.path / "test-results"
    output.mkdir()
    (output / "stale.txt").write_text("from an earlier run")
    artifact = """
import os
from pathlib import Path

def test_{name}(pytestconfig):
    worker = os.environ["PYTEST_XDIST_WORKER"]
    output = Path(pytestconfig.getoption("--output"))
    assert output.name == worker
    output.mkdir(parents=True, exist_ok=True)
    (output / "{name}.txt").write_text(worker)
"""
    pytester.makepyfile(
        test_first=artifact.format(name="first"), test_second=artifact.format(name="second")
    )
    result = pytester.runpytest_subprocess(
        "-q",
        "-p",
        "pytest_playwright.pytest_playwright",
        "-p",
        "xdist.plugin",
        "-n2",
        "--dist=loadfile",
        f"--output={output}",
    )
    result.assert_outcomes(passed=2)
    written = {path.name: path.parent.name for path in output.glob("gw*/*.txt")}
    assert sorted(written) == ["first.txt", "second.txt"]
    assert sorted(written.values()) == ["gw0", "gw1"]
    assert not (output / "stale.txt").exists()


def test_private_home_outlives_other_unconfigure_hooks(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root conftest reaps leaked processes in pytest_unconfigure; keep their home."""
    _configure(pytester, monkeypatch)
    pytester.makepyfile(
        reaper_probe="""
import json, os
from pathlib import Path

def pytest_unconfigure(config):
    home = os.environ.get("OMNIGENT_CONFIG_HOME")
    Path(os.environ["ISOLATION_RECORDS"], "reaper.json").write_text(
        json.dumps({"home": home, "exists": bool(home) and Path(home).is_dir()})
    )
""",
        test_pass="def test_pass(): pass\n",
    )
    result = pytester.runpytest_subprocess("-q", "-p", "reaper_probe")
    result.assert_outcomes(passed=1)
    record = json.loads((pytester.path / "reaper.json").read_text())
    assert record["exists"], record
    assert not Path(record["home"]).exists()
