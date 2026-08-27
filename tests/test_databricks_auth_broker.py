from __future__ import annotations

import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

from omnigent.databricks_auth_broker import (
    AuthIdentity,
    DatabricksAuthBroker,
    DatabricksCredentialError,
    DatabricksCredentialsDeadError,
    normalize_workspace_host,
)


class _Config:
    def __init__(self, token: str = "opaque-pat", error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.calls = 0

    def authenticate(self) -> dict[str, str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture(autouse=True)
def _isolated_broker_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("omnigent.databricks_auth_broker._state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(
        "omnigent.databricks_auth_broker._runtime_root", lambda: tmp_path / "runtime"
    )


def test_identity_normalizes_host_and_separates_workspaces() -> None:
    first = AuthIdentity.create("oss", "HTTPS://Example.COM/")
    equivalent = AuthIdentity.create("oss", "https://example.com")
    other = AuthIdentity.create("oss", "https://other.example.com")

    assert normalize_workspace_host("Example.COM/") == "https://example.com"
    assert first == equivalent
    assert first.key != other.key


def test_broker_publishes_private_shared_token(tmp_path: Path) -> None:
    config = _Config()
    broker = DatabricksAuthBroker(
        config,
        profile="oss",
        workspace_host="https://example.com",
    )

    assert broker.current_token() == "opaque-pat"
    assert broker.current_token() == "opaque-pat"
    assert config.calls == 1

    state_files = list((tmp_path / "state").glob("*.json"))
    assert len(state_files) == 1
    payload = json.loads(state_files[0].read_text())
    assert payload["profile"] == "oss"
    assert payload["workspace_host"] == "https://example.com"
    assert payload["token"] == "opaque-pat"
    assert payload["exp"] is None
    assert stat.S_IMODE(state_files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(state_files[0].parent.stat().st_mode) == 0o700


def test_broker_refreshes_expired_shared_jwt() -> None:
    def _jwt(expiry: float) -> str:
        import base64

        payload = (
            base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
        )
        return f"header.{payload}.signature"

    config = _Config(_jwt(time.time() - 1))
    broker = DatabricksAuthBroker(config, profile="oss", workspace_host="https://example.com")
    broker.current_token()
    config.token = _jwt(time.time() + 3600)

    assert broker.current_token() == config.token
    assert config.calls == 2


def test_broker_rejects_symlink_state_file(tmp_path: Path) -> None:
    config = _Config()
    broker = DatabricksAuthBroker(config, profile="oss", workspace_host="https://example.com")
    token_path = broker._token_path
    token_path.parent.mkdir(mode=0o700, parents=True)
    target = tmp_path / "target"
    target.write_text("{}")
    token_path.symlink_to(target)

    with pytest.raises(DatabricksCredentialError, match="unsafe credential state file"):
        broker.current_token()


def test_broker_rejects_world_readable_state_file() -> None:
    config = _Config()
    broker = DatabricksAuthBroker(config, profile="oss", workspace_host="https://example.com")
    broker._token_path.parent.mkdir(mode=0o700, parents=True)
    broker._token_path.write_text("{}")
    os.chmod(broker._token_path, 0o644)

    with pytest.raises(DatabricksCredentialError, match="unsafe ownership or mode"):
        broker.current_token()


def test_permanent_failure_is_marked_dead_and_probes_are_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr("omnigent.databricks_auth_broker.time.time", lambda: now)
    monkeypatch.setattr("omnigent.databricks_auth_broker.time.monotonic", lambda: now)
    config = _Config(error=ValueError("invalid_grant: refresh token has expired"))
    broker = DatabricksAuthBroker(config, profile="oss", workspace_host="https://example.com")

    with pytest.raises(DatabricksCredentialsDeadError, match="databricks auth login"):
        broker.current_token()
    assert config.calls == 1

    with pytest.raises(DatabricksCredentialsDeadError):
        broker.current_token()
    assert config.calls == 1

    now += 15.0
    with pytest.raises(DatabricksCredentialsDeadError):
        broker.current_token()
    assert config.calls == 2

    # The failed probe claimed the interval before running, so an immediate
    # waiter does not make another credential request.
    with pytest.raises(DatabricksCredentialsDeadError):
        broker.current_token()
    assert config.calls == 2


def test_dead_marker_clears_only_after_workspace_accepts_repaired_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr("omnigent.databricks_auth_broker.time.time", lambda: now)
    monkeypatch.setattr("omnigent.databricks_auth_broker.time.monotonic", lambda: now)
    config = _Config(error=ValueError("invalid_grant"))
    validated: list[str] = []

    def _validate(_host: str, token: str) -> None:
        validated.append(token)

    broker = DatabricksAuthBroker(
        config,
        profile="oss",
        workspace_host="https://example.com",
        token_validator=_validate,
    )
    with pytest.raises(DatabricksCredentialsDeadError):
        broker.current_token()

    config.error = None
    config.token = "repaired"
    now += 15.0
    assert broker.current_token() == "repaired"
    assert validated == ["repaired"]
    assert not broker._dead_path.exists()


def test_failed_workspace_validation_keeps_dead_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("omnigent.databricks_auth_broker.time.time", lambda: now)
    monkeypatch.setattr("omnigent.databricks_auth_broker.time.monotonic", lambda: now)
    config = _Config(error=ValueError("invalid_grant"))

    def _reject(_host: str, _token: str) -> None:
        raise RuntimeError("401")

    broker = DatabricksAuthBroker(
        config,
        profile="oss",
        workspace_host="https://example.com",
        token_validator=_reject,
    )
    with pytest.raises(DatabricksCredentialsDeadError):
        broker.current_token()
    config.error = None
    now += 15.0
    with pytest.raises(DatabricksCredentialsDeadError):
        broker.current_token()
    assert broker._dead_path.exists()


@pytest.mark.posix_only
def test_twenty_processes_publish_one_token() -> None:
    """Twenty simultaneous consumers cause exactly one authentication call."""
    context = multiprocessing.get_context("fork")
    calls = context.Value("i", 0)
    start = context.Event()
    results = context.Queue()

    class _SharedConfig:
        def authenticate(self) -> dict[str, str]:
            with calls.get_lock():
                calls.value += 1
            return {"Authorization": "Bearer shared"}

    def _consume() -> None:
        start.wait()
        broker = DatabricksAuthBroker(
            _SharedConfig(), profile="oss", workspace_host="https://concurrent.example.com"
        )
        results.put(broker.current_token())

    processes = [context.Process(target=_consume) for _ in range(20)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert [results.get(timeout=1) for _ in processes] == ["shared"] * 20
    assert calls.value == 1


@pytest.mark.posix_only
def test_lock_is_released_when_refreshing_process_crashes() -> None:
    context = multiprocessing.get_context("fork")
    locked = context.Event()
    broker = DatabricksAuthBroker(
        _Config(), profile="oss", workspace_host="https://crash.example.com"
    )

    def _crash_holding_lock() -> None:
        with broker._locked():
            locked.set()
            os._exit(17)

    process = context.Process(target=_crash_holding_lock)
    process.start()
    assert locked.wait(timeout=5)
    process.join(timeout=5)
    assert process.exitcode == 17
    assert broker.current_token() == "opaque-pat"


def test_oauth_cli_timeout_is_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    class _OAuthConfig(_Config):
        auth_type = "databricks-cli"

    def _timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired("databricks", 20)

    monkeypatch.setattr("omnigent.databricks_auth_broker.subprocess.run", _timeout)
    broker = DatabricksAuthBroker(
        _OAuthConfig(), profile="oss", workspace_host="https://timeout.example.com"
    )

    with pytest.raises(DatabricksCredentialError, match="Run: databricks auth login"):
        broker.current_token()
    assert not broker._dead_path.exists()
