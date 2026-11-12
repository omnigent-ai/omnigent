"""Tests for public Databricks profile helpers in :mod:`omnigent.onboarding.setup`."""

from __future__ import annotations

import configparser
import io
import subprocess
from pathlib import Path

import pytest
from click import ClickException
from rich.console import Console

from omnigent.onboarding import setup as setup_mod
from omnigent.onboarding.setup import (
    ProfileSpec,
    _databrickscfg_path,
    _host_matches,
    _login_profile,
    _remove_profile_section,
)

_PROFILE = ProfileSpec("gateway", "https://gateway.example.com", "Gateway")
_CLI = "/usr/bin/databricks"


@pytest.fixture()
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "databrickscfg"
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(path))
    return path


def _write_cfg(path: Path, sections: dict[str, dict[str, str]]) -> None:
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    with path.open("w") as file:
        cfg.write(file)


def _read_cfg(path: Path) -> dict[str, dict[str, str]]:
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return {name: dict(cfg[name]) for name in cfg.sections()}


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=200, no_color=True), buffer


def test_host_matches_ignores_trailing_slashes() -> None:
    assert _host_matches("https://a.example.com/", "https://a.example.com")
    assert not _host_matches("https://a.example.com", "https://b.example.com")


def test_databrickscfg_path_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "~/custom.cfg")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert _databrickscfg_path() == tmp_path / "custom.cfg"


def test_databrickscfg_path_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _databrickscfg_path() == tmp_path / ".databrickscfg"


def test_remove_profile_section_reports_whether_it_removed(cfg_path: Path) -> None:
    assert _remove_profile_section("gateway") is False

    _write_cfg(cfg_path, {"gateway": {"host": "https://gateway"}, "keep": {"a": "1"}})
    assert _remove_profile_section("missing") is False
    assert _remove_profile_section("gateway") is True
    assert _read_cfg(cfg_path) == {"keep": {"a": "1"}}


def _run_returning(returncode: int, calls: list[list[str]]):
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode)

    return fake_run


def test_login_profile_success_logs_in_then_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(subprocess, "run", _run_returning(0, calls))
    monkeypatch.setattr(setup_mod.time, "sleep", sleeps.append)
    console, output = _console()

    assert _login_profile(_CLI, _PROFILE, console) is True
    assert calls == [[_CLI, "auth", "login", "--host", _PROFILE.host, "--profile", _PROFILE.name]]
    assert sleeps == [setup_mod._OAUTH_BETWEEN_LOGIN_SLEEP_SECONDS]
    assert "✓ gateway" in output.getvalue()


def test_login_profile_reports_failure_without_pausing(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(subprocess, "run", _run_returning(2, []))
    monkeypatch.setattr(setup_mod.time, "sleep", sleeps.append)
    console, output = _console()

    assert _login_profile(_CLI, _PROFILE, console) is False
    assert "failed (exit 2)" in output.getvalue()
    assert sleeps == []


def test_login_profile_handles_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(argv: list[str], **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", interrupted)
    console, output = _console()

    assert _login_profile(_CLI, _PROFILE, console) is False
    assert "cancelled gateway" in output.getvalue()


def _cli_returns(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> None:
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout=stdout),
    )


@pytest.mark.parametrize(
    "exc", [OSError("exec failed"), subprocess.TimeoutExpired("databricks", 10)]
)
def test_existing_profile_hosts_is_empty_when_cli_cannot_run(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)

    def failing(argv: list[str], **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", failing)
    assert setup_mod._existing_profile_hosts() == {}


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, '{"profiles": [{"name": "a", "host": "h"}]}'),
        (0, "not json"),
        (0, "[]"),
        (0, '{"profiles": "nope"}'),
    ],
)
def test_existing_profile_hosts_is_empty_for_unusable_cli_output(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    _cli_returns(monkeypatch, returncode=returncode, stdout=stdout)
    assert setup_mod._existing_profile_hosts() == {}


def test_existing_profile_hosts_skips_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        '{"profiles": [{"name": "ok", "host": "https://ok"}, "junk",'
        ' {"name": "no-host"}, {"name": 3, "host": "https://x"}]}'
    )
    _cli_returns(monkeypatch, stdout=payload)

    assert setup_mod._existing_profile_hosts() == {"ok": "https://ok"}


def test_login_databricks_workspace_raises_when_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", dict)
    monkeypatch.setattr(setup_mod, "_login_profile", lambda cli, spec, console: False)

    with pytest.raises(
        ClickException, match=r"`databricks auth login` failed for https://ws\.example\.com"
    ):
        setup_mod.login_databricks_workspace("https://ws.example.com/browse?o=1")
