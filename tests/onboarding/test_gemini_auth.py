"""Tests for :mod:`omnigent.onboarding.gemini_auth`.

Covers both real ``agy`` token formats — macOS ``oauth_creds.json``
(``access_token`` / ``refresh_token``) and Linux
``antigravity-cli/antigravity-oauth-token`` (``{auth_method, token}``) — the
dual-path default that recognizes a logged-in user on either platform, and the
macOS-only ``agy models`` fallback that covers agy 1.1.7+, which keeps OAuth in
the Keychain and writes no token file. The Linux shape was confirmed live
against agy 1.0.10 on k3s.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent.onboarding import gemini_auth as ga
from omnigent.onboarding import harness_install


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate_detection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep detection off the developer's real machine.

    Points the default credential paths at an empty tmp home and neutralizes the
    macOS CLI fallback, so no test reads a real ``~/.gemini`` or shells out to a
    real ``agy models`` — which on a signed-in Mac would flip the not-logged-in
    assertions. Tests opt back in to exactly the signal they exercise.

    :param monkeypatch: pytest's env/attr patching fixture.
    :param tmp_path: pytest's per-test temporary directory fixture.
    :returns: None.
    """
    home = tmp_path / "gemini-home"
    monkeypatch.setattr(
        ga,
        "GEMINI_OAUTH_CRED_PATHS",
        (home / "oauth_creds.json", home / "antigravity-cli" / "antigravity-oauth-token"),
    )
    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda key: False)


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


# ---------------------------------------------------------------------------
# macOS Keychain fallback (agy 1.1.7+)
# ---------------------------------------------------------------------------


def test_macos_keychain_login_detected_via_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS with no token file, a signed-in CLI counts as logged in.

    agy 1.1.7+ puts the OAuth credential in the Keychain and writes neither
    ``oauth_creds.json`` nor ``antigravity-oauth-token``, so a file-only check
    reported ``antigravity-native`` as unconfigured and refused to launch it for
    a user who was in fact signed in. ``agy models`` reads the Keychain, so it
    sees the login the files cannot.
    """
    monkeypatch.setattr(ga.sys, "platform", "darwin")
    seen_keys: list[str] = []

    def _fake_logged_in(key: str) -> bool:
        seen_keys.append(key)
        return True

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _fake_logged_in)
    assert ga.gemini_login_detected() is True
    # The fallback asked about the gemini family specifically.
    assert seen_keys == ["gemini"]


def test_omnigent_written_settings_json_is_not_a_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A settings.json omnigent wrote itself must not read as a login.

    ``ensure_agy_feedback_survey_disabled`` creates
    ``~/.gemini/antigravity-cli/settings.json`` under the real home before agy
    starts, so its existence proves only that omnigent ran. Treating it as a
    credential would leave the launch gate permanently satisfied for a user who
    never signed in. Only the CLI verdict may decide.
    """
    monkeypatch.setattr(ga.sys, "platform", "darwin")
    _write(
        tmp_path / "gemini-home" / "antigravity-cli" / "settings.json",
        {"showFeedbackSurvey": False},
    )
    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda key: False)
    assert ga.gemini_login_detected() is False


def test_non_darwin_never_runs_cli_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off macOS the fallback never runs — detection stays file-only.

    Linux writes a real token file, so its absence is a true negative. Running
    the fallback there would only add a subprocess and weaken a signal that
    already works. The stub raises if called, so any non-darwin invocation fails.
    """
    monkeypatch.setattr(ga.sys, "platform", "linux")

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"CLI fallback must not run off macOS (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    assert ga.gemini_login_detected() is False


def test_token_file_short_circuits_cli_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A usable token file wins on macOS without paying for a subprocess."""
    monkeypatch.setattr(ga.sys, "platform", "darwin")
    macos = _write(tmp_path / "oauth_creds.json", {"access_token": "ya29.macos"})
    monkeypatch.setattr(ga, "GEMINI_OAUTH_CRED_PATHS", (macos,))

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"file-present path must not invoke the CLI (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    assert ga.gemini_login_detected() is True


def test_explicit_creds_path_skips_cli_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit *creds_path* checks only that file — the fallback stays off.

    The caller named the signal it wants, so a Keychain login must not rescue a
    file it explicitly asked about. Pinned because the carve-out is load-bearing
    for the documented semantics and for test isolation.
    """
    monkeypatch.setattr(ga.sys, "platform", "darwin")

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"explicit creds_path must not invoke the CLI (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    assert ga.gemini_auth_has_credential(tmp_path / "nope.json") is False


# ---------------------------------------------------------------------------
# Readiness must never raise
# ---------------------------------------------------------------------------


def test_unreadable_home_reads_as_no_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An inaccessible ~/.gemini reads as not-logged-in rather than raising.

    This verdict feeds the daemon's hello frame and readiness refresh loop, both
    of which call it unguarded, so one odd-permission home must not abort them.
    """
    home = tmp_path / "locked"
    (home / "antigravity-cli").mkdir(parents=True)
    macos = home / "oauth_creds.json"
    linux = home / "antigravity-cli" / "antigravity-oauth-token"
    macos.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ga, "GEMINI_OAUTH_CRED_PATHS", (macos, linux))
    home.chmod(0o000)
    try:
        assert ga.gemini_login_detected() is False
    finally:
        # Restore before tmp_path cleanup, which cannot remove a mode-000 dir.
        home.chmod(0o700)


@pytest.mark.parametrize(
    "error",
    [
        OSError("spawn failed"),
        subprocess.TimeoutExpired(cmd="agy models", timeout=30),
        subprocess.SubprocessError("boom"),
        ValueError("bad args"),
    ],
)
def test_cli_fallback_failure_reads_as_no_credential(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A fallback that cannot run reads as not-logged-in rather than raising.

    ``harness_cli_logged_in`` already absorbs a missing binary, a non-zero exit,
    and a timeout; this pins the outer contract so a future change there cannot
    turn a probe failure into a crashed readiness poll.
    """
    monkeypatch.setattr(ga.sys, "platform", "darwin")

    def _raise(key: str) -> bool:
        raise error

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _raise)
    assert ga.gemini_login_detected() is False
