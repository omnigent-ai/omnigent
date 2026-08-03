"""Tests for the read-only ``omnigent diagnose`` snapshot."""

from __future__ import annotations

import pytest

from omnigent import diagnostics
from omnigent.diagnostics import collect_snapshot
from omnigent.version import VERSION


def test_snapshot_local_only_has_no_server_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    snap = collect_snapshot(server_url=None)
    assert snap["cli_version"] == VERSION
    assert snap["server_url"] is None
    assert snap["server_version"] is None  # can't know a server's version without asking
    assert snap["auth_source_origin"] == "local-env"
    assert snap["auth_source"]  # env-derived, always present
    assert snap["os"]
    assert snap["python"]


def test_snapshot_reports_only_known_keys() -> None:
    snap = collect_snapshot(server_url=None)
    assert set(snap) == {
        "cli_version",
        "server_version",
        "server_url",
        "auth_source",
        "auth_source_origin",
        "os",
        "python",
    }


def test_local_auth_source_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "OIDC")
    snap = collect_snapshot(server_url=None)
    assert snap["auth_source"] == "oidc"
    assert snap["auth_source_origin"] == "local-env"


def test_server_info_populates_version_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reachable server supplies BOTH version and the real auth mode.
    monkeypatch.setattr(
        diagnostics,
        "_fetch_server_info",
        lambda url, *, timeout: {"server_version": "9.9.9", "accounts_enabled": True},
    )
    snap = collect_snapshot(server_url="http://example:6767")
    assert snap["server_version"] == "9.9.9"
    assert snap["server_url"] == "http://example:6767"
    assert snap["auth_source"] == "accounts"
    assert snap["auth_source_origin"] == "server"


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"accounts_enabled": True}, "accounts"),
        # single_user must win over the header fallback: a local one-user server
        # and a header-auth deploy both have accounts_enabled=false / no login_url.
        ({"accounts_enabled": False, "single_user": True}, "single_user"),
        ({"accounts_enabled": False, "single_user": False, "login_url": "https://idp"}, "oidc"),
        ({"accounts_enabled": False, "single_user": False, "login_url": None}, "header"),
    ],
)
def test_auth_source_derived_from_info(info: dict, expected: str) -> None:
    assert diagnostics._auth_source_from_info(info) == expected


def test_unreachable_server_falls_back_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.setattr(diagnostics, "_fetch_server_info", lambda url, *, timeout: None)
    snap = collect_snapshot(server_url="http://127.0.0.1:6767")
    assert snap["server_version"] is None
    assert snap["auth_source"] == "header"
    assert snap["auth_source_origin"] == "local-env"


def test_fetch_server_info_degrades_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failing HTTP call returns None, never raises.
    import httpx

    def _boom(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("httpx.get", _boom)
    assert diagnostics._fetch_server_info("http://127.0.0.1:6767", timeout=1.0) is None


def test_snapshot_contains_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with a secret-looking env var set, the snapshot never carries it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-appear")
    blob = repr(collect_snapshot(server_url=None))
    assert "sk-should-not-appear" not in blob
