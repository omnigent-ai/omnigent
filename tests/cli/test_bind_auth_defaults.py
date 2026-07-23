"""Unit tests for ``_apply_bind_auth_defaults`` — the bind-interface → auth-mode decision.

Covers the four corners of the matrix:

- loopback + env-unset → single-user marker (no login);
- non-loopback + env-unset → accounts mode (login required) + warning;
- explicit ``OMNIGENT_AUTH_PROVIDER`` → no implicit change;
- explicit ``OMNIGENT_AUTH_ENABLED=0`` → no implicit re-enable.
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
    "OMNIGENT_ACCOUNTS_ENABLED",
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


def test_non_loopback_respects_deprecated_accounts_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deprecated OMNIGENT_ACCOUNTS_ENABLED alias also counts as explicit.

    An operator who set the old var name should not be auto-overridden.
    """
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_ENABLED", "0")
    _apply_bind_auth_defaults("0.0.0.0")

    assert os.environ.get("OMNIGENT_AUTH_ENABLED") is None
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
