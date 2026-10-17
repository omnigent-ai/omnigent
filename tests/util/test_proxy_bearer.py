"""The proxy-bearer file: how managed sandbox hosts get past an authenticating front door."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
from omnigent.util.proxy_bearer import (
    PROXY_BEARER_FILE_ENV_VAR,
    proxy_bearer_configured,
    read_proxy_bearer,
)


@pytest.fixture
def bearer_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "proxy-bearer"
    path.write_text("front-door-token\n")
    monkeypatch.setenv(PROXY_BEARER_FILE_ENV_VAR, str(path))
    return path


def test_unset_means_no_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROXY_BEARER_FILE_ENV_VAR, raising=False)
    assert not proxy_bearer_configured()
    assert read_proxy_bearer() is None


def test_reads_and_strips_and_rereads_after_refresh(bearer_file: Path) -> None:
    assert proxy_bearer_configured()
    assert read_proxy_bearer() == "front-door-token"
    bearer_file.write_text("refreshed-token")
    assert read_proxy_bearer() == "refreshed-token"


@pytest.mark.parametrize("contents", [None, "", "  \n"])
def test_missing_or_empty_file_means_no_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None
) -> None:
    path = tmp_path / "proxy-bearer"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setenv(PROXY_BEARER_FILE_ENV_VAR, str(path))
    assert read_proxy_bearer() is None


def _host():
    from omnigent.host.connect import HostProcess
    from omnigent.host.identity import HostIdentity

    return HostProcess(
        identity=HostIdentity(host_id="host_test_proxy", name="managed"),
        server_url="https://omnigent.example.com",
    )


def test_managed_host_sends_launch_token_and_proxy_bearer(
    bearer_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-token")
    headers = _host()._build_connect_headers()
    assert headers[MANAGED_HOST_TOKEN_HEADER] == "launch-token"
    assert headers["Authorization"] == "Bearer front-door-token"


def test_managed_host_without_proxy_bearer_sends_only_launch_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROXY_BEARER_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-token")
    host = _host()
    headers = host._build_connect_headers()
    assert headers[MANAGED_HOST_TOKEN_HEADER] == "launch-token"
    assert "Authorization" not in headers
    assert host._current_auth_token() is None


def test_managed_host_hands_proxy_bearer_to_runners(
    bearer_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-token")
    assert _host()._current_auth_token() == "front-door-token"


def test_runner_env_allowlist_keeps_the_file_path() -> None:
    from omnigent.host.connect import _RUNNER_ENV_ALLOWLIST

    assert PROXY_BEARER_FILE_ENV_VAR in _RUNNER_ENV_ALLOWLIST


def test_runner_factory_reads_the_file_and_drops_the_seeded_token(
    bearer_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import _entry
    from omnigent.runner.identity import RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR

    monkeypatch.setattr(_entry, "_runner_auth_factory", None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://omnigent.example.com")
    monkeypatch.setenv(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR, "seeded")
    factory = _entry._make_auth_token_factory()
    assert factory is not None
    assert factory() == "front-door-token"
    bearer_file.write_text("refreshed-token")
    assert factory() == "refreshed-token"
    import os

    assert RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR not in os.environ


def test_cli_remote_headers_use_proxy_bearer_but_explicit_token_wins(
    bearer_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.chat import _REMOTE_AUTH_TOKEN_ENV, _remote_headers

    monkeypatch.delenv(_REMOTE_AUTH_TOKEN_ENV, raising=False)
    url = "https://omnigent.example.com"
    assert _remote_headers(url, host_id=None)["Authorization"] == "Bearer front-door-token"
    monkeypatch.setenv(_REMOTE_AUTH_TOKEN_ENV, "explicit")
    assert _remote_headers(url, host_id=None)["Authorization"] == "Bearer explicit"
