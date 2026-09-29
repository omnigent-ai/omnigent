"""Unit coverage for shared pexpect REPL test helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pexpect
import pytest
import yaml

from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env, wait_for_ready


@pytest.mark.parametrize("keep_running", [False, True])
def test_wait_for_ready_reports_startup_failure(keep_running: bool) -> None:
    """Preserve startup errors when the process exits or waits at a crash prompt."""
    script = "print('RuntimeError: startup failed', flush=True)"
    if keep_running:
        script += "; input('Report this crash? ')"
    child = pexpect.spawn(sys.executable, ["-c", script], encoding="utf-8")
    try:
        with pytest.raises(AssertionError, match="RuntimeError: startup failed"):
            wait_for_ready(child, timeout=1.0)
    finally:
        child.close(force=True)


def test_ensure_repl_test_theme_env_seeds_isolated_home(tmp_path: Path) -> None:
    """Helper writes a persisted theme into a caller-provided fake HOME.

    :param tmp_path: Pytest temporary directory used as the fake HOME root.
    """
    home = tmp_path / "home"
    env = ensure_repl_test_theme_env({"HOME": str(home)})

    config = home / ".omnigent" / "config.yaml"
    assert env["HOME"] == str(home)
    assert "theme: light" in config.read_text(encoding="utf-8")


def test_ensure_repl_test_theme_env_uses_config_home_and_preserves_config(
    tmp_path: Path,
) -> None:
    """Configured config home wins without clobbering existing auth settings.

    :param tmp_path: Pytest temporary directory used for isolated paths.
    """
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    config_home.mkdir()
    config_path = config_home / "config.yaml"
    config_path.write_text("auth:\n  type: api_key\n", encoding="utf-8")

    env = ensure_repl_test_theme_env(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
        }
    )

    assert env["OMNIGENT_CONFIG_HOME"] == str(config_home)
    assert not (home / ".omnigent" / "config.yaml").exists()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {
        "auth": {"type": "api_key"},
        "tui": {"theme": "light"},
    }


def test_ensure_repl_test_theme_env_does_not_write_real_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inherited real HOME is replaced with a temp HOME for the subprocess.

    :param tmp_path: Pytest temporary directory used to model the real HOME.
    :param monkeypatch: Pytest monkeypatch fixture for overriding
        :meth:`Path.home`.
    """
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    databrickscfg = real_home / ".databrickscfg"
    databrickscfg.write_text(
        "[profile]\nhost = https://example.databricks.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: real_home)

    env = ensure_repl_test_theme_env({"HOME": str(real_home)})
    prepared_home = Path(env["HOME"])

    assert prepared_home != real_home
    assert not (real_home / ".omnigent" / "config.yaml").exists()
    assert (prepared_home / ".databrickscfg").samefile(databrickscfg)
    assert "theme: light" in (prepared_home / ".omnigent" / "config.yaml").read_text(
        encoding="utf-8"
    )
