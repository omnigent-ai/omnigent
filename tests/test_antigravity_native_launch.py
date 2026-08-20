"""Tests for the Antigravity (agy) launch-config module.

No live agy calls are made — all external dependencies (shutil.which,
gemini_auth_has_credential, agy_binary_path) are monkeypatched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.antigravity_native_launch as _mod
from omnigent.antigravity_native_launch import (
    NativeAntigravityLaunch,
    agy_binary_path,
    build_agy_launch,
    resolve_native_antigravity_launch,
    should_skip_permissions,
)

_SKIP_FLAG = "--dangerously-skip-permissions"

# ---------------------------------------------------------------------------
# agy_binary_path
# ---------------------------------------------------------------------------


class TestAgyBinaryPath:
    """Tests for :func:`agy_binary_path`."""

    def test_returns_path_from_which(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Return the path found by shutil.which when it succeeds."""
        fake_path = str(tmp_path / "agy")
        monkeypatch.setattr("shutil.which", lambda _name: fake_path)
        assert agy_binary_path() == fake_path

    def test_returns_fallback_when_which_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Fall back to ~/.local/bin/agy when shutil.which returns None."""
        fake_fallback = tmp_path / "agy"
        fake_fallback.touch(mode=0o755)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(_mod, "_AGY_FALLBACK_PATH", fake_fallback)
        result = agy_binary_path()
        assert result == str(fake_fallback)

    def test_raises_when_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Raise RuntimeError with install instructions when agy is not found."""
        missing_fallback = tmp_path / "agy"  # does NOT exist
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(_mod, "_AGY_FALLBACK_PATH", missing_fallback)
        with pytest.raises(RuntimeError, match="curl"):
            agy_binary_path()

    def test_error_mentions_fallback_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """RuntimeError message names the missing fallback path."""
        missing_fallback = tmp_path / "agy"
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(_mod, "_AGY_FALLBACK_PATH", missing_fallback)
        with pytest.raises(RuntimeError, match=str(missing_fallback)):
            agy_binary_path()


# ---------------------------------------------------------------------------
# resolve_native_antigravity_launch
# ---------------------------------------------------------------------------


class TestResolveNativeAntigravityLaunch:
    """Tests for :func:`resolve_native_antigravity_launch`."""

    def test_returns_subscription_when_credential_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return subscription mode when Gemini credential is detected."""
        monkeypatch.setattr(
            "omnigent.antigravity_native_launch.gemini_auth_has_credential",
            lambda: True,
        )
        result = resolve_native_antigravity_launch()
        assert isinstance(result, NativeAntigravityLaunch)
        assert result.auth_mode == "subscription"

    def test_returns_subscription_when_credential_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return subscription mode even when no Gemini credential is found."""
        monkeypatch.setattr(
            "omnigent.antigravity_native_launch.gemini_auth_has_credential",
            lambda: False,
        )
        result = resolve_native_antigravity_launch()
        assert isinstance(result, NativeAntigravityLaunch)
        assert result.auth_mode == "subscription"

    def test_passes_model_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model kwarg is forwarded to NativeAntigravityLaunch."""
        monkeypatch.setattr(
            "omnigent.antigravity_native_launch.gemini_auth_has_credential",
            lambda: True,
        )
        result = resolve_native_antigravity_launch(model="gemini-2.5-pro")
        assert result.model == "gemini-2.5-pro"

    def test_model_none_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model defaults to None when not provided."""
        monkeypatch.setattr(
            "omnigent.antigravity_native_launch.gemini_auth_has_credential",
            lambda: True,
        )
        result = resolve_native_antigravity_launch()
        assert result.model is None

    def test_warns_when_no_credential(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning naming both token paths is logged when no agy credential is found."""
        monkeypatch.setattr(
            "omnigent.antigravity_native_launch.gemini_auth_has_credential",
            lambda: False,
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="omnigent.antigravity_native_launch"):
            resolve_native_antigravity_launch()
        records = [r.message for r in caplog.records]
        assert any("agy OAuth credential" in m for m in records)
        # Names both checked locations so the hint is correct on macOS AND Linux
        # (the Linux path is where the deploy target actually stores the token).
        assert any("antigravity-cli/antigravity-oauth-token" in m for m in records)


# ---------------------------------------------------------------------------
# build_agy_launch
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_agy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Monkeypatch agy_binary_path to a deterministic fake path."""
    fake = str(tmp_path / "agy")
    monkeypatch.setattr(_mod, "agy_binary_path", lambda: fake)
    return fake


class TestBuildAgyLaunch:
    """Tests for :func:`build_agy_launch`."""

    def test_argv_starts_with_binary(self, fake_agy: str) -> None:
        """argv[0] is the agy binary path."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert argv[0] == fake_agy

    # ------------------------------------------------------------------
    # Fresh-session path (resume=False)
    # ------------------------------------------------------------------

    def test_fresh_env_is_empty(self, fake_agy: str) -> None:
        """Fresh session emits no env overrides (agy ignores all knobs)."""
        _, env = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert env == {}

    def test_fresh_env_has_no_conversation_id(self, fake_agy: str) -> None:
        """Fresh session does NOT set ANTIGRAVITY_CONVERSATION_ID (agy ignores it)."""
        _, env = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert "ANTIGRAVITY_CONVERSATION_ID" not in env

    def test_fresh_argv_no_conversation_flag(self, fake_agy: str) -> None:
        """Fresh session does NOT include --conversation in argv."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert "--conversation" not in argv

    # ------------------------------------------------------------------
    # Resume path (resume=True)
    # ------------------------------------------------------------------

    def test_resume_argv_has_conversation_flag(self, fake_agy: str) -> None:
        """Resume session includes --conversation <id> in argv."""
        argv, _ = build_agy_launch(
            conversation_id="68caaeac-real-uuid",
            model=None,
            resume=True,
        )
        assert "--conversation" in argv
        idx = argv.index("--conversation")
        assert argv[idx + 1] == "68caaeac-real-uuid"

    def test_resume_env_no_conversation_id(self, fake_agy: str) -> None:
        """Resume session does NOT set ANTIGRAVITY_CONVERSATION_ID in env."""
        _, env = build_agy_launch(
            conversation_id="68caaeac-real-uuid",
            model=None,
            resume=True,
        )
        assert "ANTIGRAVITY_CONVERSATION_ID" not in env

    def test_resume_without_conversation_id_raises(self, fake_agy: str) -> None:
        """Resuming with no conversation id raises (agy needs a real id)."""
        with pytest.raises(ValueError, match="requires a conversation id"):
            build_agy_launch(conversation_id=None, model=None, resume=True)

    # ------------------------------------------------------------------
    # Model flag
    # ------------------------------------------------------------------

    def test_model_flag_present_when_given(self, fake_agy: str) -> None:
        """--model <label> is appended when model is provided."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model="gemini-2.5-pro",
            resume=False,
        )
        assert "--model" in argv
        idx = argv.index("--model")
        assert argv[idx + 1] == "gemini-2.5-pro"

    def test_model_flag_absent_when_none(self, fake_agy: str) -> None:
        """--model is NOT appended when model is None."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert "--model" not in argv

    # ------------------------------------------------------------------
    # No env knobs (agy ignores them all)
    # ------------------------------------------------------------------

    def test_no_sidecar_port_env(self, fake_agy: str) -> None:
        """ANTIGRAVITY_SIDECAR_WEB_PORT is never emitted (agy ignores it)."""
        for resume in (False, True):
            _, env = build_agy_launch(
                conversation_id="68caaeac-real-uuid" if resume else None,
                model=None,
                resume=resume,
            )
            assert "ANTIGRAVITY_SIDECAR_WEB_PORT" not in env

    def test_no_data_dir_env(self, fake_agy: str) -> None:
        """ANTIGRAVITY_EXECUTABLE_DATA_DIR is never emitted (agy ignores it)."""
        _, env = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert "ANTIGRAVITY_EXECUTABLE_DATA_DIR" not in env

    # ------------------------------------------------------------------
    # extra_args
    # ------------------------------------------------------------------

    def test_extra_args_appended(self, fake_agy: str) -> None:
        """Extra args are appended after generated flags."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
            extra_args=("--print-timeout", "30"),
        )
        assert argv[-2:] == ["--print-timeout", "30"]

    # ------------------------------------------------------------------
    # Permission-mode → --dangerously-skip-permissions (phase 4 task 1)
    # ------------------------------------------------------------------

    def test_bypass_mode_appends_skip_flag(self, fake_agy: str) -> None:
        """bypassPermissions → the skip flag is present in argv."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
            permission_mode="bypassPermissions",
            headless=False,
        )
        assert _SKIP_FLAG in argv

    def test_non_bypass_interactive_omits_skip_flag(self, fake_agy: str) -> None:
        """Non-bypass mode + interactive (attended) → the skip flag is absent."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
            permission_mode="default",
            headless=False,
        )
        assert _SKIP_FLAG not in argv

    def test_non_bypass_headless_appends_skip_flag(self, fake_agy: str) -> None:
        """Non-bypass mode + headless → the skip flag is auto-added (no hang)."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
            permission_mode="default",
            headless=True,
        )
        assert _SKIP_FLAG in argv

    def test_skip_flag_defaults_absent(self, fake_agy: str) -> None:
        """Defaults (no permission_mode, not headless) omit the skip flag."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
        )
        assert _SKIP_FLAG not in argv

    def test_skip_flag_not_duplicated_from_extra_args(self, fake_agy: str) -> None:
        """A user-passed skip flag in extra_args is not duplicated when headless."""
        argv, _ = build_agy_launch(
            conversation_id=None,
            model=None,
            resume=False,
            headless=True,
            extra_args=(_SKIP_FLAG,),
        )
        assert argv.count(_SKIP_FLAG) == 1


# ---------------------------------------------------------------------------
# should_skip_permissions
# ---------------------------------------------------------------------------


class TestShouldSkipPermissions:
    """Tests for :func:`should_skip_permissions` (phase 4 task 1)."""

    def test_bypass_mode_true(self) -> None:
        """bypassPermissions always skips, even when interactive."""
        assert should_skip_permissions(permission_mode="bypassPermissions", headless=False) is True

    def test_non_bypass_interactive_false(self) -> None:
        """Non-bypass + interactive does not skip (agy prompts the attended user)."""
        assert should_skip_permissions(permission_mode="default", headless=False) is False

    def test_non_bypass_headless_true(self) -> None:
        """Non-bypass + headless skips so an unattended turn does not hang."""
        assert should_skip_permissions(permission_mode="default", headless=True) is True

    def test_none_mode_interactive_false(self) -> None:
        """``None`` mode is treated as non-bypass; interactive does not skip."""
        assert should_skip_permissions(permission_mode=None, headless=False) is False

    def test_none_mode_headless_true(self) -> None:
        """``None`` mode + headless skips (headless wins regardless of mode)."""
        assert should_skip_permissions(permission_mode=None, headless=True) is True
