"""Tests for :mod:`omnigent.onboarding.gemini_auth`.

Covers both real ``agy`` token formats — macOS ``oauth_creds.json``
(``access_token`` / ``refresh_token``) and Linux
``antigravity-cli/antigravity-oauth-token`` (``{auth_method, token}``) — and the
dual-path default that recognizes a logged-in user on either platform. The
Linux shape was confirmed live against agy 1.0.10 on k3s.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.onboarding import gemini_auth as ga


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_macos_oauth_creds_format_detected(tmp_path: Path) -> None:
    """The macOS ``oauth_creds.json`` shape (``access_token``) is a usable login."""
    creds = _write(
        tmp_path / "oauth_creds.json",
        {"access_token": "ya29.abc", "token_type": "Bearer"},
    )
    assert ga.gemini_auth_has_credential(creds) is True


def test_linux_antigravity_token_format_detected(tmp_path: Path) -> None:
    """The Linux ``antigravity-oauth-token`` shape (``{auth_method, token}``) is a
    usable login — the real agy 1.0.10 Linux format (verified on k3s). The old
    ``access_token``/``refresh_token``-only check missed this and falsely read
    the deploy target as not-logged-in.
    """
    creds = _write(
        tmp_path / "antigravity-oauth-token",
        {
            "auth_method": "oauth",
            "token": {
                "access_token": "ya29.xyz",
                "refresh_token": "1//0gabc",
                "token_type": "Bearer",
                "expiry": "2026-06-19T12:00:00Z",
            },
        },
    )
    assert ga.gemini_auth_has_credential(creds) is True


def test_refresh_token_only_detected(tmp_path: Path) -> None:
    """A ``refresh_token`` alone (no ``access_token``) still counts as logged in."""
    creds = _write(tmp_path / "oauth_creds.json", {"refresh_token": "1//0gabc"})
    assert ga.gemini_auth_has_credential(creds) is True


def test_flat_token_survives_nondict_token_field(tmp_path: Path) -> None:
    """A valid top-level ``access_token`` is honored even when a sibling
    ``token`` field is a (non-dict) string — the nested-scan guard must not
    shadow the flat credential.
    """
    creds = _write(
        tmp_path / "oauth_creds.json",
        {"access_token": "ya29.flat", "token": "some-opaque-string"},
    )
    assert ga.gemini_auth_has_credential(creds) is True


@pytest.mark.parametrize(
    "payload",
    [
        {},  # object but no token field
        {"access_token": ""},  # empty token
        {"token": "   "},  # "token" present but a (whitespace) string, not creds
        {"token": "abc"},  # "token" is a non-empty string, not a creds object
        {"auth_method": "oauth"},  # Linux shape but token object missing
        {"token": {}},  # nested token object but empty
        {"token": {"access_token": ""}},  # nested but empty access_token
        ["not", "an", "object"],  # JSON but not an object
    ],
)
def test_tokenless_or_malformed_not_detected(tmp_path: Path, payload: object) -> None:
    """A file with no usable token field reads as not-logged-in."""
    creds = _write(tmp_path / "oauth_creds.json", payload)
    assert ga.gemini_auth_has_credential(creds) is False


def test_missing_file_not_detected(tmp_path: Path) -> None:
    """A path that does not exist reads as not-logged-in (must not raise)."""
    assert ga.gemini_auth_has_credential(tmp_path / "nope.json") is False


def test_non_json_file_not_detected(tmp_path: Path) -> None:
    """A non-JSON file reads as not-logged-in."""
    p = tmp_path / "oauth_creds.json"
    p.write_text("not json at all", encoding="utf-8")
    assert ga.gemini_auth_has_credential(p) is False


def test_default_checks_both_platform_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no explicit path, detection succeeds when EITHER platform location
    carries a token — so a logged-in Linux host (token only at
    ``antigravity-cli/antigravity-oauth-token``, no ``oauth_creds.json``) is
    recognized. This is the deploy-target case the old single-path check broke.
    """
    macos = tmp_path / "oauth_creds.json"
    linux = tmp_path / "antigravity-cli" / "antigravity-oauth-token"
    monkeypatch.setattr(ga, "GEMINI_OAUTH_CRED_PATHS", (macos, linux))

    # Neither present → not logged in.
    assert ga.gemini_auth_has_credential() is False
    assert ga.gemini_login_detected() is False

    # Only the Linux-format token present → logged in.
    _write(linux, {"auth_method": "oauth", "token": {"access_token": "ya29.linux"}})
    assert ga.gemini_auth_has_credential() is True
    assert ga.gemini_login_detected() is True

    # Symmetrically: only the macOS-format file present → logged in.
    linux.unlink()
    _write(macos, {"access_token": "ya29.macos"})
    assert ga.gemini_login_detected() is True
