"""The Claude login probe asks the OMNIGENT_CLAUDE_PATH launcher when one is set."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from omnigent.onboarding import harness_install as hi


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    hi._LOGIN_PROBE_CACHE.clear()
    yield
    hi._LOGIN_PROBE_CACHE.clear()


def test_launcher_with_banner_reports_logged_in(tmp_path, monkeypatch):
    plain = _script(tmp_path, "claude", "echo '{\"loggedIn\": false}'\n")
    launcher = _script(
        tmp_path,
        "claude-launcher",
        "echo \"Launcher banner\"\necho '{'\necho '  \"loggedIn\": true'\necho '}'\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(launcher))
    assert hi.harness_cli_logged_in(hi.ANTHROPIC_FAMILY) is True
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH")
    hi._LOGIN_PROBE_CACHE.clear()
    assert shutil.which("claude") == str(plain)
    assert hi.harness_cli_logged_in(hi.ANTHROPIC_FAMILY) is False


def test_missing_launcher_falls_back_to_path(tmp_path, monkeypatch):
    _script(tmp_path, "claude", "echo '{\"loggedIn\": true}'\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(tmp_path / "does-not-exist"))
    assert hi.harness_cli_logged_in(hi.ANTHROPIC_FAMILY) is True


def test_launcher_ignored_for_other_families(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(_script(tmp_path, "x", "exit 0\n")))
    assert hi._claude_launcher_override("openai") is None


def test_status_json_skips_banner():
    assert hi._status_json('banner\n{"loggedIn": true}\n') == {"loggedIn": True}
    assert hi._status_json("no json here") is None


def test_launcher_probe_gets_the_longer_timeout(tmp_path, monkeypatch):
    launcher = _script(tmp_path, "claude-launcher", "echo '{\"loggedIn\": true}'\n")
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(launcher))
    seen = {}
    real = hi.run_isolated

    def spy(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real(cmd, **kwargs)

    monkeypatch.setattr(hi, "run_isolated", spy)
    assert hi.harness_cli_logged_in(hi.ANTHROPIC_FAMILY, timeout=10.0) is True
    assert seen["timeout"] == hi.LAUNCHER_PROBE_TIMEOUT_S
