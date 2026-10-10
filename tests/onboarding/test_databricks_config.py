"""Unit tests for omnigent.onboarding.databricks_config."""

from __future__ import annotations

import configparser
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.onboarding import databricks_config as db_cfg_mod
from omnigent.onboarding.databricks_config import (
    _databricks_cli_version,
    _oauth_callback_port_holder,
    _OAuthPortHolder,
    databricks_login_port_conflict,
    databricks_sdk_installed,
    get_workspace_url_for_profile,
    normalize_workspace_url,
)

_WORKSPACE_URL = "https://example.databricks.com"


def test_get_workspace_url_for_profile_reads_databrickscfg(tmp_path: Path) -> None:
    """Resolves a profile name to its host from ~/.databrickscfg."""
    cfg = configparser.ConfigParser()
    cfg["test-profile"] = {"host": _WORKSPACE_URL, "token": "tok"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("test-profile")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_strips_trailing_slash(tmp_path: Path) -> None:
    """Host values with a trailing slash are normalized."""
    cfg = configparser.ConfigParser()
    cfg["test-profile"] = {"host": _WORKSPACE_URL + "/", "token": "tok"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("test-profile")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_returns_none_when_file_absent(
    tmp_path: Path,
) -> None:
    """Returns None when ~/.databrickscfg does not exist."""
    with patch(
        "omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH",
        tmp_path / "nonexistent",
    ):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_returns_none_for_missing_profile(
    tmp_path: Path,
) -> None:
    """Returns None when the named profile is not in ~/.databrickscfg."""
    cfg = configparser.ConfigParser()
    cfg["other"] = {"host": "https://example-other.cloud.databricks.com"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_does_not_use_default_for_missing_profile(
    tmp_path: Path,
) -> None:
    """A typo'd profile must not silently resolve to the DEFAULT workspace."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg["other"] = {"host": "https://example-other.cloud.databricks.com"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_reads_explicit_default_profile(
    tmp_path: Path,
) -> None:
    """The DEFAULT section is only used when the caller asks for DEFAULT."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("DEFAULT")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_reads_lowercase_default_profile(
    tmp_path: Path,
) -> None:
    """The Databricks SDK treats ``default`` as the DEFAULT profile name."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("default")

    assert url == _WORKSPACE_URL


def test_databricks_sdk_installed_true_in_dev_env() -> None:
    """``databricks_sdk_installed`` finds the SDK in the dev environment.

    The dev/CI install carries ``databricks-sdk`` (via the ``all`` extra),
    so the helper must report it present. A failure means the helper probes
    the wrong module path (e.g. a typo'd ``find_spec`` target), which would
    make the add-provider menu and ``setup --internal-beta`` claim the
    Databricks extra is missing even on installs that have it.
    """
    assert databricks_sdk_installed() is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The reported foot-gun: the URL copied from a browser address bar.
        (
            "https://my-ws.cloud.databricks.com/browse?o=1234567890",
            "https://my-ws.cloud.databricks.com",
        ),
        # Path with no query.
        (
            "https://my-ws.cloud.databricks.com/explore/data",
            "https://my-ws.cloud.databricks.com",
        ),
        # Fragment is dropped too.
        (
            "https://my-ws.cloud.databricks.com/#/setting/account",
            "https://my-ws.cloud.databricks.com",
        ),
        # Surrounding whitespace is trimmed before parsing.
        (
            "  https://my-ws.cloud.databricks.com/browse  ",
            "https://my-ws.cloud.databricks.com",
        ),
        # Pre-existing trailing-slash case still collapses.
        ("https://my-ws.cloud.databricks.com/", "https://my-ws.cloud.databricks.com"),
        # Already an origin — returned unchanged.
        ("https://my-ws.cloud.databricks.com", "https://my-ws.cloud.databricks.com"),
    ],
)
def test_normalize_workspace_url_reduces_to_origin(raw: str, expected: str) -> None:
    """A pasted workspace URL is reduced to its bare ``scheme://host`` origin."""
    assert normalize_workspace_url(raw) == expected


def test_normalize_workspace_url_scheme_less_input_only_strips_trailing_slash() -> None:
    """Without a scheme there is no netloc to isolate, so the result matches the
    prior ``rstrip("/")`` behavior — the wizard pre-adds ``https://`` before
    calling, so a scheme is present in practice."""
    assert normalize_workspace_url("my-ws.cloud.databricks.com/") == "my-ws.cloud.databricks.com"


# ── OAuth callback port preflight ────────────────────────────────────────────


@pytest.fixture
def busy_port(monkeypatch: pytest.MonkeyPatch):
    """Point the callback port at a loopback port that is currently listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        monkeypatch.setattr(db_cfg_mod, "_DATABRICKS_OAUTH_CALLBACK_PORT", port)
        yield port


def _set_cli_version(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int] | None
) -> None:
    monkeypatch.setattr(db_cfg_mod, "_databricks_cli_version", lambda _bin: version)


def test_port_conflict_none_when_port_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """A free callback port is never a conflict, whatever the CLI version."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setattr(db_cfg_mod, "_DATABRICKS_OAUTH_CALLBACK_PORT", free_port)
    _set_cli_version(monkeypatch, (0, 244, 0))
    assert databricks_login_port_conflict("databricks") is None


def test_port_conflict_message_for_old_cli(
    monkeypatch: pytest.MonkeyPatch, busy_port: int
) -> None:
    """A busy port with a non-falling-back CLI yields an upgrade message."""
    _set_cli_version(monkeypatch, (0, 244, 0))
    monkeypatch.setattr(
        db_cfg_mod, "_oauth_callback_port_holder", lambda: _OAuthPortHolder(None, None)
    )
    msg = databricks_login_port_conflict("databricks")
    assert msg is not None
    assert str(busy_port) in msg
    assert "v0.244.0" in msg
    assert "v0.265.0" in msg
    assert "kill" not in msg
    assert " by " not in msg


def test_port_conflict_names_holder(monkeypatch: pytest.MonkeyPatch, busy_port: int) -> None:
    """The holder's pid and process name appear in the message when known."""
    _set_cli_version(monkeypatch, (0, 244, 0))
    monkeypatch.setattr(
        db_cfg_mod, "_oauth_callback_port_holder", lambda: _OAuthPortHolder(9999, "arcaterm")
    )
    msg = databricks_login_port_conflict("databricks")
    assert msg is not None
    assert "by pid 9999 (arcaterm)" in msg


def test_port_conflict_none_for_fallback_capable_cli(
    monkeypatch: pytest.MonkeyPatch, busy_port: int
) -> None:
    """CLI v0.265.0+ picks another port itself, so a busy 8020 is fine."""
    _set_cli_version(monkeypatch, (1, 17, 0))
    assert databricks_login_port_conflict("databricks") is None
    _set_cli_version(monkeypatch, (0, 265, 0))
    assert databricks_login_port_conflict("databricks") is None


def test_port_conflict_none_for_unknown_version(
    monkeypatch: pytest.MonkeyPatch, busy_port: int
) -> None:
    """An unreadable CLI version never blocks login."""
    _set_cli_version(monkeypatch, None)
    assert databricks_login_port_conflict("databricks") is None


def _fake_cli(tmp_path: Path, body: str) -> str:
    script = tmp_path / "databricks"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_databricks_cli_version_parses_output(tmp_path: Path) -> None:
    cli = _fake_cli(tmp_path, "echo 'Databricks CLI v0.244.0'")
    assert _databricks_cli_version(cli) == (0, 244, 0)


@pytest.mark.parametrize(
    "body",
    ["echo garbage", "echo 'Databricks CLI v0.244.0'; exit 1", "echo 'Databricks CLI v0.0.0-dev'"],
)
def test_databricks_cli_version_none_for_unusable_output(tmp_path: Path, body: str) -> None:
    assert _databricks_cli_version(_fake_cli(tmp_path, body)) is None


def test_databricks_cli_version_none_for_missing_binary(tmp_path: Path) -> None:
    assert _databricks_cli_version(str(tmp_path / "nope")) is None


def test_oauth_callback_port_holder_finds_own_process(busy_port: int) -> None:
    """psutil identifies the listener (this test process)."""
    import psutil

    try:
        psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        pytest.skip("psutil.net_connections requires root on this platform")
    assert _oauth_callback_port_holder().pid is not None


def test_oauth_callback_port_holder_tolerates_psutil_access_denied(
    monkeypatch: pytest.MonkeyPatch, busy_port: int
) -> None:
    """psutil AccessDenied still yields a bare holder instead of raising."""
    import psutil

    def _denied(**kwargs: object) -> list[object]:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", _denied)
    assert _oauth_callback_port_holder() == _OAuthPortHolder(pid=None, name=None)
