"""Regression checks for the UI suite's fixture configuration."""

import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from _pytest.config import Config

from tests.helpers import ui_configuration as configuration


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("owned", [True, False])
def test_mock_config_respects_workflow_ownership(monkeypatch, tmp_path, harness, owned):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("original config\n")
    with configuration.temp_omnigent_mock_config("http://model", harness, workflow_owned=owned):
        if owned:
            assert config.read_text() == "original config\n"
        else:
            assert f"mock-{harness}:" in config.read_text()
            assert "http://model" in config.read_text()
    assert config.read_text() == "original config\n"


def test_native_claude_mock_rejects_conflicting_managed_route(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("original config\n")
    managed = tmp_path / "managed-settings.json"
    managed.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://private.example/secret"}})
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed,)
    )
    with pytest.raises(RuntimeError, match="managed ANTHROPIC_BASE_URL overrides") as caught:
        configuration.validate_native_claude_mock_routing("http://model")
    assert config.read_text() == "original config\n"
    assert not (tmp_path / "config.yaml.e2e-backup").exists()
    assert "private.example" not in str(caught.value)
    assert "secret" not in str(caught.value)

    # The Anthropic config is also used by Pi; only real Claude launches need this check.
    with configuration.temp_omnigent_mock_config("http://model", "claude"):
        assert "mock-claude:" in config.read_text()


@pytest.mark.parametrize(
    "settings",
    [
        None,
        "not json",
        "[]",
        "{}",
        '{"apiKeyHelper": "credential-helper"}',
        '{"env": {"ANTHROPIC_BASE_URL": "http://model/"}}',
    ],
    ids=["missing", "malformed", "non-object", "unrelated", "auth-only", "matching"],
)
def test_native_claude_mock_allows_settings_without_a_conflicting_route(
    monkeypatch, tmp_path, settings
):
    managed = tmp_path / "managed-settings.json"
    if settings is not None:
        managed.write_text(settings)
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed,)
    )
    configuration.validate_native_claude_mock_routing("http://model")


@pytest.mark.parametrize("missing", ["SERVER_URL", "MODEL_URL", "RUNNER_ID"])
def test_prepared_environment_rejects_missing_setting(monkeypatch, missing):
    keys = ("OMNIGENT_REPRO_SERVER_URL", "OMNIGENT_REPRO_MODEL_URL", "OMNIGENT_REPRO_RUNNER_ID")
    missing_key = f"OMNIGENT_REPRO_{missing}"
    for key in keys:
        monkeypatch.setenv(key, "" if key == missing_key else "configured")
    with pytest.raises(
        RuntimeError, match=f"Incomplete prepared reproduction environment: missing {missing_key}"
    ):
        configuration.prepared_repro_environment()


@pytest.mark.parametrize("key", ["pid", "runner_pid", "database_uri", "restart_server"])
def test_workflow_owned_state_explains_unsupported_access(key):
    state = configuration.ServerState(workflow_owned=True)
    with pytest.raises(RuntimeError, match="Workflow-owned reproduction does not expose"):
        _ = state[key]


def test_mock_config_backup_survives_and_blocks_overwrite(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    backup = tmp_path / "config.yaml.e2e-backup"
    path.write_bytes(b"original config\n")
    with configuration.temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
        assert "12345" in path.read_text()
        assert backup.read_bytes() == b"original config\n"
        assert backup.stat().st_mode & 0o077 == 0
    assert path.read_bytes() == b"original config\n"
    assert not backup.exists()
    backup.write_bytes(b"interrupted run original\n")
    with pytest.raises(RuntimeError, match="recover the original config"):
        with configuration.temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
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
    expected = (
        pytest.raises(ValueError, match="test assertion failed")
        if raise_in_test
        else nullcontext()
    )
    with expected:
        with configuration.temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
            assert "12345" in target.read_text()
            if raise_in_test:
                raise ValueError("test assertion failed")
    assert config.is_symlink()
    assert config.lstat().st_ino == link_inode
    assert config.readlink() == Path(target.name)
    assert target.read_text() == "original provider config\n"


@dataclass
class _ConfigStub:
    options: dict[str, Any] = field(default_factory=dict)
    stash: pytest.Stash = field(default_factory=pytest.Stash)
    # Set only on xdist workers, e.g. ``{"workerid": "gw0"}``.
    workerinput: dict[str, Any] | None = None

    def getoption(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)


def test_pytest_configure_rejects_headed_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")

    with pytest.raises(pytest.UsageError, match="must run headless in CI"):
        configuration.pytest_configure(
            cast(Config, _ConfigStub({"--ui-base-url": None, "--headed": True}))
        )


def test_pytest_configure_rejects_dev_ui_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_E2E_ALLOW_DEV_BASE_URL", raising=False)
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(pytest.UsageError, match="Refusing --ui-base-url"):
        configuration.pytest_configure(
            cast(
                Config,
                _ConfigStub({"--ui-base-url": "http://127.0.0.1:5173", "--headed": False}),
            )
        )


def test_worker_replaces_and_restores_an_inherited_suite_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inherited = tmp_path / "controller"
    inherited.mkdir()
    (inherited / "config.yaml").write_text("controller copy\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(inherited))
    monkeypatch.setenv("OMNIGENT_UI_TESTS_CONFIG_HOME", str(inherited))
    monkeypatch.delenv("OMNIGENT_REPRO_SERVER_URL", raising=False)
    config = cast(Config, _ConfigStub({"numprocesses": 2}, workerinput={"workerid": "gw0"}))
    configuration._isolate_config_home(config)
    worker_home = Path(os.environ["OMNIGENT_CONFIG_HOME"])
    assert worker_home != inherited
    assert os.environ["OMNIGENT_UI_TESTS_CONFIG_HOME"] == str(worker_home)
    assert (worker_home / "config.yaml").read_text() == "controller copy\n"
    assert oct((worker_home / "config.yaml").stat().st_mode & 0o777) == "0o600"
    configuration.pytest_unconfigure(config)
    assert os.environ["OMNIGENT_CONFIG_HOME"] == str(inherited)
    assert os.environ["OMNIGENT_UI_TESTS_CONFIG_HOME"] == str(inherited)
    assert not worker_home.exists()
    assert (inherited / "config.yaml").read_text() == "controller copy\n"


def test_xdist_controller_leaves_config_home_to_its_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OMNIGENT_CONFIG_HOME", "OMNIGENT_REPRO_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    # A controller launched from inside another xdist worker inherits its id.
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    config = cast(Config, _ConfigStub({"numprocesses": 2}))
    configuration._isolate_config_home(config)
    assert "OMNIGENT_CONFIG_HOME" not in os.environ
    configuration.pytest_unconfigure(config)
    assert "OMNIGENT_CONFIG_HOME" not in os.environ


@pytest.mark.parametrize(("config_home", "marker"), [(None, "stale-marker"), ("", None)])
def test_unconfigure_restores_both_variables_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_home: str | None, marker: str | None
) -> None:
    monkeypatch.setattr("omnigent.config._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    for name, value in (
        ("OMNIGENT_CONFIG_HOME", config_home),
        ("OMNIGENT_UI_TESTS_CONFIG_HOME", marker),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("OMNIGENT_REPRO_SERVER_URL", raising=False)
    config = cast(Config, _ConfigStub())
    configuration._isolate_config_home(config)
    private_home = Path(os.environ["OMNIGENT_CONFIG_HOME"])
    assert private_home.is_dir()
    configuration.pytest_unconfigure(config)
    assert os.environ.get("OMNIGENT_CONFIG_HOME") == config_home
    assert os.environ.get("OMNIGENT_UI_TESTS_CONFIG_HOME") == marker
    assert not private_home.exists()
