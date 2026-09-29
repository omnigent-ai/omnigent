"""Regression checks for the UI suite's fixture configuration."""

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from _pytest.config import Config

from tests.e2e_ui import conftest as fixtures


@pytest.fixture(autouse=True, name="_enforce_min_server_version")
def isolated_version_gate() -> None:
    """Fixture unit tests have no live server to check for compatibility."""


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("owned", [True, False])
def test_mock_fixture_ignores_credential_placeholder(monkeypatch, harness, owned):
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-placeholder")
    monkeypatch.setattr(
        fixtures, "_server_state", {"runner_id": "runner", "workflow_owned": owned}
    )
    monkeypatch.setattr(fixtures, "_ensure_runner_online", lambda *_: None)
    monkeypatch.setattr(fixtures, f"_create_native_{harness}_session", lambda *_: "session")
    monkeypatch.setattr(fixtures.httpx, "delete", Mock())
    configure = Mock(return_value=nullcontext())
    monkeypatch.setattr(fixtures, "_temp_omnigent_mock_config", configure)
    fixture = getattr(fixtures, f"native_{harness}_mock_session").__wrapped__
    journey = fixture("http://server", "http://model", None)
    assert next(journey) == ("http://server", "session")
    journey.close()
    assert configure.call_count == (0 if owned else 1)


@pytest.mark.parametrize("missing", ["SERVER_URL", "MODEL_URL", "RUNNER_ID"])
def test_partial_prepared_environment_fails_before_spawning_mock(monkeypatch, missing):
    keys = ("OMNIGENT_REPRO_SERVER_URL", "OMNIGENT_REPRO_MODEL_URL", "OMNIGENT_REPRO_RUNNER_ID")
    missing_key = f"OMNIGENT_REPRO_{missing}"
    for key in keys:
        monkeypatch.setenv(key, "" if key == missing_key else "configured")
    spawn = Mock(side_effect=AssertionError("must not spawn a different mock"))
    monkeypatch.setattr(fixtures.subprocess, "Popen", spawn)
    with pytest.raises(
        RuntimeError, match=f"Incomplete prepared reproduction environment: missing {missing_key}"
    ):
        next(fixtures.mock_llm_server_url.__wrapped__(None))
    spawn.assert_not_called()


@pytest.mark.parametrize("key", ["pid", "runner_pid", "database_uri", "restart_server"])
def test_workflow_owned_state_explains_unsupported_access(key):
    state = fixtures._ServerState(workflow_owned=True)
    with pytest.raises(RuntimeError, match="Workflow-owned reproduction does not expose"):
        _ = state[key]


def test_mock_config_backup_survives_and_blocks_overwrite(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    backup = tmp_path / "config.yaml.e2e-backup"
    path.write_bytes(b"original config\n")
    with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
        assert "12345" in path.read_text()
        assert backup.read_bytes() == b"original config\n"
        assert backup.stat().st_mode & 0o077 == 0
    assert path.read_bytes() == b"original config\n"
    assert not backup.exists()
    backup.write_bytes(b"interrupted run original\n")
    with pytest.raises(RuntimeError, match="recover the original config"):
        with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
            pytest.fail("must not overwrite a recovery backup")
    assert backup.read_bytes() == b"interrupted run original\n"
    assert path.read_bytes() == b"original config\n"


@pytest.mark.parametrize("raise_in_test", [False, True])
def test_mock_config_restores_symlink_target(monkeypatch, tmp_path, raise_in_test):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "original.yaml"
    target.write_text("original provider config\n")
    config = tmp_path / "config.yaml"
    config.symlink_to(target.name)
    link_inode = config.lstat().st_ino
    try:
        with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
            assert "12345" in target.read_text()
            if raise_in_test:
                raise ValueError("test assertion failed")
    except ValueError:
        assert raise_in_test
    assert config.is_symlink()
    assert config.lstat().st_ino == link_inode
    assert config.readlink() == Path(target.name)
    assert target.read_text() == "original provider config\n"


@dataclass
class _ConfigStub:
    options: dict[str, Any]

    def getoption(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)


def test_pytest_configure_rejects_headed_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")

    with pytest.raises(pytest.UsageError, match="must run headless in CI"):
        fixtures.pytest_configure(
            cast(Config, _ConfigStub({"--ui-base-url": None, "--headed": True}))
        )


def test_pytest_configure_rejects_dev_ui_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_E2E_ALLOW_DEV_BASE_URL", raising=False)
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(pytest.UsageError, match="Refusing --ui-base-url"):
        fixtures.pytest_configure(
            cast(
                Config,
                _ConfigStub({"--ui-base-url": "http://127.0.0.1:5173", "--headed": False}),
            )
        )
