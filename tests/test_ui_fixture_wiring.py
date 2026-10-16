"""Check UI fixture adapters in a separate pytest session with no live services."""

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def test_ui_fixture_wiring(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeini("[pytest]\n")
    pytester.makepyfile(
        """
from unittest.mock import Mock

import pytest

from tests.e2e_ui import conftest as fixtures
from tests.e2e import _harness_probes


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
@pytest.mark.parametrize("owned", [False, True])
def test_mock_session_uses_owned_config(monkeypatch, tmp_path, harness, owned):
    monkeypatch.setattr(_harness_probes, "cli_unavailable_reason", lambda _: None)
    monkeypatch.setattr(fixtures.shutil, "which", lambda _: "/test/tmux")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-placeholder")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("original")
    monkeypatch.setattr(
        fixtures, "_server_state", {"runner_id": "runner", "workflow_owned": owned}
    )
    monkeypatch.setattr(fixtures, "_ensure_runner_online", lambda *_: None)
    create = Mock(return_value="session")
    monkeypatch.setattr(fixtures, f"_create_native_{harness}_session", create)
    delete = Mock()
    monkeypatch.setattr(fixtures.httpx, "delete", delete)
    journey = getattr(fixtures, f"native_{harness}_mock_session").__wrapped__(
        "http://server", "http://model", None
    )
    try:
        assert next(journey) == ("http://server", "session")
        create.assert_called_once_with("http://server", "runner")
        if owned:
            assert config.read_text() == "original"
        else:
            provider = "claude" if harness == "pi" else harness
            assert f"mock-{provider}:" in config.read_text()
            assert "http://model" in config.read_text()
    finally:
        journey.close()
    assert config.read_text() == "original"
    delete.assert_called_once_with("http://server/v1/sessions/session", timeout=10.0)


@pytest.mark.parametrize("present", range(1, 7))
def test_partial_environment_fails_before_spawn(monkeypatch, tmp_path, present):
    keys = ("OMNIGENT_REPRO_SERVER_URL", "OMNIGENT_REPRO_MODEL_URL", "OMNIGENT_REPRO_RUNNER_ID")
    for index, key in enumerate(keys):
        monkeypatch.setenv(key, "configured" if present & (1 << index) else "")
    spawn = Mock(side_effect=AssertionError("must not spawn a replacement mock"))
    monkeypatch.setattr(fixtures.subprocess, "Popen", spawn)
    factory = Mock()
    factory.mktemp.return_value = tmp_path
    journey = fixtures.mock_llm_server_url.__wrapped__(factory)
    try:
        with pytest.raises(RuntimeError, match="Incomplete prepared reproduction environment"):
            next(journey)
    finally:
        journey.close()
    spawn.assert_not_called()
"""
    )
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=12)
