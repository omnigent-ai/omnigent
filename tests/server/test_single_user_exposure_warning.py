"""Unit tests for the single-user exposure warning helpers.

``warn_if_single_user_exposed`` is the rule every spawn path shares — the
CLI prints it to stderr, the container entrypoints log it. It fires only
where the exposure is real: a reachable bind, a truthy single-user
marker, and a resolved source of ``header`` (the one mode where the
unauthenticated ``"local"`` fallback is live).
"""

from __future__ import annotations

import pytest

from omnigent.server.auth import (
    bind_host_is_loopback,
    warn_if_single_user_exposed,
)

_AUTH_ENVS = (
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_LOCAL_SINGLE_USER",
    "OMNIGENT_OIDC_ISSUER",
)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear auth env vars so each case starts from a known posture."""
    for _var in _AUTH_ENVS:
        monkeypatch.delenv(_var, raising=False)


# ── bind_host_is_loopback ───────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_hosts_recognized(host: str) -> None:
    """Literals and the wider 127.0.0.0/8 range count as loopback."""
    assert bind_host_is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5"])
def test_non_loopback_hosts_recognized(host: str) -> None:
    """Wildcards are NOT loopback — they accept traffic from every interface."""
    assert bind_host_is_loopback(host) is False


def test_unresolvable_host_treated_as_reachable() -> None:
    """An unparseable host errs toward "reachable" so the warning still fires."""
    assert bind_host_is_loopback("some-k8s-service.internal") is False


# ── warn_if_single_user_exposed ─────────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
@pytest.mark.parametrize("marker", ["1", "true", "yes", "TRUE"])
def test_warns_on_reachable_bind_with_truthy_marker(
    host: str, marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable bind + truthy marker + header mode → warning names the exposure."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", marker)

    msg = warn_if_single_user_exposed(host)

    assert msg is not None
    assert host in msg
    assert "unauthenticated" in msg.lower()
    assert "OMNIGENT_LOCAL_SINGLE_USER" in msg


def test_silent_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """On loopback the marker is the normal, safe local posture."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")

    assert warn_if_single_user_exposed("127.0.0.1") is None


def test_silent_without_marker() -> None:
    """No marker means no ``"local"`` fallback, so nothing is exposed."""
    assert warn_if_single_user_exposed("0.0.0.0") is None


def test_silent_with_falsy_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``=0`` is an explicit opt-out, not a declaration."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")

    assert warn_if_single_user_exposed("0.0.0.0") is None


@pytest.mark.parametrize("provider", ["accounts", "oidc"])
def test_silent_under_explicit_login_provider(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit login provider routes identity through the cookie path.

    Login is required there, so claiming exposure would be false.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", provider)
    if provider == "oidc":
        monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://idp.example.com")

    assert warn_if_single_user_exposed("0.0.0.0") is None


def test_warns_under_explicit_header_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning header mode deliberately is still exposure, so still warn."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")

    assert warn_if_single_user_exposed("0.0.0.0") is not None


def test_warns_under_auth_enabled_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AUTH_ENABLED=0`` is set-but-falsy, resolving to header mode.

    The Docker kill-switch posture, which also sets the marker.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")

    assert warn_if_single_user_exposed("0.0.0.0") is not None


def test_silent_under_auth_enabled_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truthy enable switch resolves to accounts — login required."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")

    assert warn_if_single_user_exposed("0.0.0.0") is None
