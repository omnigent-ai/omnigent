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


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("owned", [False, True])
def test_mock_session_uses_owned_config(monkeypatch, tmp_path, harness, owned):
    monkeypatch.setattr("omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
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
        "http://server", "http://model", None,
        **({"claude_managed_settings": None} if harness == "claude" else {})
    )
    try:
        assert next(journey) == ("http://server", "session")
        create.assert_called_once_with("http://server", "runner")
        if owned:
            assert config.read_text() == "original"
        else:
            assert f"mock-{harness}:" in config.read_text()
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
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=10)


def test_real_claude_fixture_sees_managed_routing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeini("[pytest]\n")
    pytester.makeconftest(
        """
from unittest.mock import Mock

import pytest

from tests.conftest import _isolate_claude_managed_settings, claude_managed_settings
from tests.e2e_ui import conftest as fixtures

native_claude_mock_session = fixtures.native_claude_mock_session
native_codex_mock_session = fixtures.native_codex_mock_session


@pytest.fixture(scope="session", autouse=True)
def managed_policy(tmp_path_factory):
    settings = tmp_path_factory.mktemp("machine") / "managed-settings.json"
    settings.write_text('{"env": {"ANTHROPIC_BASE_URL": "https://private.example/secret"}}')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (settings,)
        )
        yield


@pytest.fixture
def live_server():
    return "http://server"


@pytest.fixture
def mock_llm_server_url():
    return "http://model"


@pytest.fixture(params=[False, True], autouse=True)
def services(request, monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("original")
    monkeypatch.setattr(
        fixtures, "_server_state", {"runner_id": "runner", "workflow_owned": request.param}
    )
    runner = Mock(return_value=None)
    claude = Mock(side_effect=AssertionError("must not create a Claude session"))
    monkeypatch.setattr(fixtures, "_ensure_runner_online", runner)
    monkeypatch.setattr(fixtures, "_create_native_claude_session", claude)
    monkeypatch.setattr(fixtures, "_create_native_codex_session", Mock(return_value="session"))
    monkeypatch.setattr(fixtures.httpx, "delete", Mock())
    yield
    claude.assert_not_called()
    if "native_claude_mock_session" in request.fixturenames:
        runner.assert_not_called()
    assert config.read_text() == "original"
    assert not (tmp_path / "config.yaml.e2e-backup").exists()
"""
    )
    pytester.makepyfile(
        """
def test_claude(native_claude_mock_session):
    raise AssertionError("setup must reject conflicting managed routing")


def test_codex(native_codex_mock_session):
    assert native_codex_mock_session == ("http://server", "session")
"""
    )
    result = pytester.runpytest_subprocess("-q", "--tb=short")
    result.assert_outcomes(passed=2, errors=2)
    result.stdout.fnmatch_lines(["*managed ANTHROPIC_BASE_URL overrides the mock provider*"])
    assert "private.example" not in result.stdout.str()
    assert "secret" not in result.stdout.str()
