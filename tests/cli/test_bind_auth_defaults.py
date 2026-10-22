"""Unit tests for ``_apply_bind_auth_defaults`` — the bind-interface → auth-mode decision.

Covers the four corners of the matrix:

- loopback + env-unset → single-user marker (no login);
- non-loopback + env-unset → accounts mode (login required) + warning;
- explicit ``OMNIGENT_AUTH_PROVIDER`` → no implicit change;
- explicit ``OMNIGENT_AUTH_ENABLED=0`` → no implicit re-enable;
- non-loopback + explicit ``OMNIGENT_LOCAL_SINGLE_USER=1`` → header mode
  kept (no accounts auto-enable) + security warning.
"""

from __future__ import annotations

import os

import pytest

from omnigent.cli import _apply_bind_auth_defaults

# The env vars the helper reads / writes. Cleared per-test so no
# cross-test leakage.
_AUTH_ENVS = (
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_LOCAL_SINGLE_USER",
    "OMNIGENT_OIDC_ISSUER",
)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all auth-related env vars before each test."""
    for _var in _AUTH_ENVS:
        monkeypatch.delenv(_var, raising=False)


def _stderr(capsys: pytest.CaptureFixture[str]) -> str:
    """Stderr captured from the last helper call."""
    return capsys.readouterr().err


# ── loopback ───────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_enables_single_user(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A loopback bind with no explicit auth gets the single-user marker.

    The default ``omnigent server`` on loopback is a local single-user
    runtime — no proxy to inject identity — so it keeps the no-login
    ``"local"`` header-mode fallback.
    """
    _apply_bind_auth_defaults(host)
    assert os.environ.get("OMNIGENT_LOCAL_SINGLE_USER") == "1"
    # Accounts mode must NOT be auto-enabled on loopback.
    assert os.environ.get("OMNIGENT_AUTH_ENABLED") is None
    # No warning on the loopback path.
    assert _stderr(capsys) == ""


def test_loopback_respects_explicit_single_user_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit OMNIGENT_LOCAL_SINGLE_USER=0 wins on loopback.

    setdefault must not clobber an operator's explicit "off".
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    _apply_bind_auth_defaults("127.0.0.1")
    assert os.environ.get("OMNIGENT_LOCAL_SINGLE_USER") == "0"
    assert _stderr(capsys) == ""


# ── non-loopback ───────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5"])
def test_non_loopback_enables_accounts(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A non-loopback bind with no explicit auth enables accounts mode.

    An end user binding to a network interface has no realistic way to
    inject an identity header, so we opt them into login. A warning is
    echoed to stderr so the operator knows accounts mode was
    auto-enabled and how to create the first admin.
    """
    _apply_bind_auth_defaults(host)

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") == "1"
    # The single-user marker must NOT be set on a non-loopback bind.
    assert os.environ.get("OMNIGENT_LOCAL_SINGLE_USER") is None

    # The warning names the host and mentions accounts mode + admin setup.
    _msg = _stderr(capsys)
    assert host in _msg
    assert "accounts" in _msg.lower()
    assert "admin" in _msg.lower()


def test_non_loopback_respects_explicit_auth_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit OMNIGENT_AUTH_PROVIDER prevents the auto-enable.

    An operator who declared ``header`` (behind an identity-injecting
    proxy) or ``oidc`` chose their auth deliberately — don't override it.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    _apply_bind_auth_defaults("0.0.0.0")

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") is None
    # No auto-enable warning — the operator was explicit.
    assert _stderr(capsys) == ""


def test_non_loopback_respects_explicit_auth_enabled_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit OMNIGENT_AUTH_ENABLED=0 disables auth — no re-enable.

    The operator explicitly turned auth off; we must not flip it back on.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    _apply_bind_auth_defaults("0.0.0.0")

    # Stays "0", not overwritten by setdefault.
    assert os.environ.get("OMNIGENT_AUTH_ENABLED") == "0"
    assert _stderr(capsys) == ""


def test_non_loopback_with_existing_auth_enabled_no_double_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """OMNIGENT_AUTH_ENABLED=1 already set → no warning (it's not auto-enabled).

    The operator already opted in; the "we enabled accounts for you"
    warning would be noise.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    _apply_bind_auth_defaults("0.0.0.0")

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") == "1"
    assert _stderr(capsys) == ""


def test_non_loopback_empty_auth_provider_string_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty OMNIGENT_AUTH_PROVIDER ('') is treated as unset.

    Compose-style deploys pass ``${VAR:-}`` which expands to an empty
    string — empty and missing both mean "not explicitly pinned", so the
    non-loopback auto-enable should still fire.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "")
    _apply_bind_auth_defaults("0.0.0.0")

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") == "1"


# ── non-loopback + explicit single-user marker ──────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5"])
@pytest.mark.parametrize("marker", ["1", "true", "yes", "TRUE"])
def test_non_loopback_respects_explicit_single_user(
    host: str, marker: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truthy OMNIGENT_LOCAL_SINGLE_USER wins on a non-loopback bind.

    Auto-enabling accounts here would switch identity resolution to the
    cookie path, where neither the ``"local"`` fallback nor the identity
    header is reachable — every request 401s and the host tunnel 403s.
    The operator declared a single-operator server, so header mode stays.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", marker)
    _apply_bind_auth_defaults(host)

    # Accounts mode must NOT be auto-enabled over the explicit marker.
    assert os.environ.get("OMNIGENT_AUTH_ENABLED") is None
    assert os.environ.get("OMNIGENT_LOCAL_SINGLE_USER") == marker

    from omnigent.server.auth import resolve_auth_source

    assert resolve_auth_source() == "header"

    # The warning names the exposure, not just the mode: this posture
    # serves unauthenticated to anyone who can reach the bind address.
    _msg = _stderr(capsys)
    assert host in _msg
    assert "unauthenticated" in _msg.lower()
    assert "OMNIGENT_LOCAL_SINGLE_USER" in _msg


def test_non_loopback_single_user_zero_still_enables_accounts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """OMNIGENT_LOCAL_SINGLE_USER=0 is an opt-out — accounts still auto-enable.

    Only a *truthy* marker declares a single-user server; an explicit
    "off" must not suppress the network-exposed login default.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    _apply_bind_auth_defaults("0.0.0.0")

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") == "1"
    assert "accounts" in _stderr(capsys).lower()


def test_non_loopback_single_user_with_auth_enabled_one_keeps_accounts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit AUTH_ENABLED=1 beats the single-user marker.

    Both are explicit; the narrower "auth on" wins, and the
    unauthenticated-exposure warning must not fire for a server that
    does require login.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    _apply_bind_auth_defaults("0.0.0.0")

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") == "1"
    assert _stderr(capsys) == ""


@pytest.mark.parametrize("provider", ["accounts", "oidc"])
def test_non_loopback_single_user_with_explicit_login_provider_is_silent(
    provider: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit login provider beside the marker suppresses the warning.

    ``OMNIGENT_AUTH_PROVIDER`` wins outright in ``resolve_auth_source``, so
    identity goes through the cookie path and login *is* required. Claiming
    unauthenticated exposure here would be false.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", provider)
    _apply_bind_auth_defaults("0.0.0.0")

    from omnigent.server.auth import resolve_auth_source

    assert resolve_auth_source() == provider
    assert _stderr(capsys) == ""


def test_non_loopback_single_user_with_explicit_header_provider_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit header provider beside the marker still warns.

    Header mode is exactly where the ``"local"`` fallback is reachable, so
    pinning it deliberately is real exposure — the warning must not be
    silenced just because the provider was named explicitly.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    _apply_bind_auth_defaults("0.0.0.0")

    assert "unauthenticated" in _stderr(capsys).lower()


def test_non_loopback_single_user_with_auth_enabled_zero_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AUTH_ENABLED=0 beside the marker warns: it resolves to header mode.

    The kill-switch is "set" but falsy, so ``resolve_auth_source`` returns
    ``header`` and the unauthenticated ``"local"`` fallback is live. The
    exposure is real and must be announced.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    _apply_bind_auth_defaults("0.0.0.0")

    from omnigent.server.auth import resolve_auth_source

    assert resolve_auth_source() == "header"
    assert "unauthenticated" in _stderr(capsys).lower()


def test_loopback_single_user_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exposure warning is scoped to non-loopback binds.

    On loopback the single-user marker is the normal, safe default — it
    must stay silent.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    _apply_bind_auth_defaults("127.0.0.1")

    assert _stderr(capsys) == ""


# ── OIDC ───────────────────────────────────────────────────────────


def test_non_loopback_with_oidc_resolves_to_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-loopback + AUTH_ENABLED=1 + OIDC issuer → resolve_auth_source returns oidc.

    The helper only sets AUTH_ENABLED; the downstream accounts-ergonomics
    block gates on resolve_auth_source(), which returns ``oidc`` (not
    ``accounts``) when an OIDC issuer is present — so no accounts secrets
    are minted. This test documents that the helper's single env-var
    flip is enough: OIDC config is respected downstream.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://idp.example.com")

    _apply_bind_auth_defaults("0.0.0.0")

    from omnigent.server.auth import resolve_auth_source

    assert resolve_auth_source() == "oidc"
