"""Tests for harness readiness checks (``harness_readiness.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnigent.onboarding.harness_install as hi
from omnigent.onboarding.harness_readiness import (
    configured_harness_map,
    harness_is_configured,
)


@pytest.fixture(autouse=True)
def _isolate_cursor_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate cursor + copilot credential sources so their readiness is deterministic.

    Cursor readiness keys off a configured ``CURSOR_API_KEY`` and copilot off a
    GitHub token (the ``cursor:`` / ``copilot:`` config blocks or the
    environment), so point the config home at an empty tmp dir and clear any
    ambient ``CURSOR_API_KEY`` / ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
    ``GITHUB_TOKEN`` — otherwise a developer's real key would flip their verdict
    under these tests.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _all_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear installed.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    # Follow test_harness_install.py's convention: patch the module's
    # shutil.which (reverted by monkeypatch after the test).
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")


def _no_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear missing.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)


# SDK and unknown harnesses are never gated — their credentials resolve at
# runtime from ambient/spec sources the daemon can't enumerate.
@pytest.mark.parametrize(
    "harness",
    [
        "claude-sdk",
        "claude_sdk",
        "openai-agents",
        "openai-agents-sdk",
        "agents_sdk",
        "claude",  # alias → claude-sdk
        "some-future-harness",  # unknown → fail open
    ],
)
def test_sdk_and_unknown_harnesses_are_never_gated(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """SDK / unknown harnesses are configured even with no CLI installed.

    They run in-process (or are unknown to the daemon) and resolve any
    credential at runtime, so the daemon must not block them. A ``False``
    here is a false negative that would break a launch authenticating via
    an env key, a Databricks profile, or the spec's ``executor.auth`` —
    none of which the daemon can see.
    """
    _no_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is True


# CLI-wrapping harnesses are gated on their binary being on PATH. Native Cursor
# (``omni cursor``) joins the list: it wraps the ``cursor-agent`` CLI, unlike the
# SDK ``cursor`` harness which gates on a key (covered separately below). Native
# Kiro wraps the standalone ``kiro-cli`` binary.
@pytest.mark.parametrize(
    "harness",
    [
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
        "pi",
        "cursor-native",
        "native-cursor",
        "kiro-native",
        "native-kiro",
        "goose-native",
        "native-goose",
        "hermes",
    ],
)
def test_cli_harness_configured_only_when_binary_installed(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """A CLI-wrapping harness is configured iff its binary is on PATH.

    These harnesses cannot run without their CLI; the missing binary is
    the one thing the daemon can reliably detect. Installed → True,
    absent → False. A wrong verdict here either blocks the headline
    "I never installed Claude Code/Codex" case (if it stayed True) or
    breaks every native launch (if it stayed False).
    """
    _all_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is True
    _no_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is False


def test_configured_harness_map_covers_all_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hello-frame map carries every spelling a consumer may hold.

    The server/web UI does a plain dict lookup with whatever harness
    string it has (spec executor types, canonical ids, aliases) — a
    missing key reads as "unknown" and silently disables the warning
    for that agent.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    expected_keys = {
        "claude-sdk",
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
        "openai-agents",
        "openai-agents-sdk",
        "claude_sdk",
        "agents_sdk",
        "claude",
        "pi",
        "pi-native",
        "native-pi",
        "cursor",
        # Native Cursor (``omni cursor``) — gates on the cursor-agent CLI.
        "cursor-native",
        "native-cursor",
        # Native Kiro (``omni kiro``) — gates on the kiro-cli binary.
        "kiro-native",
        "native-kiro",
        # Goose — native TUI (``omni goose``) + headless ACP harness; both gate
        # on the goose CLI.
        "goose",
        "goose-native",
        "native-goose",
        # Antigravity SDK harness + its user-facing aliases.
        "antigravity",
        "agy",
        "google-antigravity",
        # Kimi Code CLI + alias.
        "kimi",
        "kimi-code",
        # Native Kimi (``omnigent kimi``) — gates on the kimi CLI.
        "kimi-native",
        "native-kimi",
        # Native Antigravity (agy) CLI-wrapping harness, both spellings.
        "antigravity-native",
        "native-antigravity",
        # Native OpenCode harness + its user-facing aliases.
        "opencode-native",
        "native-opencode",
        "opencode",
        # Qwen harnesses — ACP (``qwen`` / ``qwen-code``) + native TUI
        # (``qwen-native`` / ``native-qwen``); all gate on the qwen CLI.
        "qwen",
        "qwen-code",
        "qwen-native",
        "native-qwen",
        # Copilot SDK harness + its user-facing alias.
        "copilot",
        "github-copilot",
        # Hermes — headless subprocess harness (``hermes``) + native TUI
        # (``hermes-native`` / ``native-hermes``); all gate on the hermes CLI.
        "hermes",
        "hermes-native",
        "native-hermes",
        # Rovo Dev ACP harness alias (canonical is rovo-cli).
        "rovo",
    }
    assert set(result) == expected_keys


def test_configured_harness_map_gates_only_cli_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no CLI installed, only CLI-wrapping spellings read False.

    SDK spellings (incl. the ``openai-agents-sdk`` workflow spelling and
    the ``claude`` alias) stay True; the native + pi spellings flip to
    False. A misclassified spelling would warn the wrong agents in the
    picker — e.g. an SDK agent authenticating via a Databricks profile
    flagged "needs setup" when it launches fine.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    # SDK / alias spellings — never gated.
    for sdk in (
        "claude-sdk",
        "claude_sdk",
        "claude",
        "openai-agents",
        "openai-agents-sdk",
        "agents_sdk",
    ):
        assert result[sdk] is True, f"{sdk} should never be gated"
    # CLI-wrapping spellings — gated, so False when the binary is absent.
    # (The SDK ``cursor`` harness is excluded: it runs via the ``cursor-sdk``
    # package and gates on a configured ``CURSOR_API_KEY``, not a binary —
    # covered separately. Native Cursor (``cursor-native`` / ``native-cursor``)
    # wraps the ``cursor-agent`` CLI, so it IS gated on the binary.)
    # antigravity-native is also gated (it wraps the ``agy`` CLI); with no
    # binary it reads False before its credential check is even reached.
    for cli in (
        "claude-native",
        "native-claude",
        "pi",
        "kimi",
        "cursor-native",
        "native-cursor",
        "kiro-native",
        "native-kiro",
        "antigravity-native",
        "native-antigravity",
        "goose-native",
        "native-goose",
        "qwen",
        "hermes",
    ):
        assert result[cli] is False, f"{cli} should be gated on its CLI binary"
    for codex in ("codex", "codex-native", "native-codex"):
        assert result[codex] == "binary-missing", f"{codex} should name the missing Codex binary"


def test_configured_harness_map_all_true_with_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every spelling reads True once the CLIs are installed and the key/token-
    gated harnesses are satisfied.

    The CLI harnesses pass their binary check, the SDK harnesses are ungated,
    cursor (key-gated) is satisfied by a ``CURSOR_API_KEY``, copilot
    (token-gated) by a ``GH_TOKEN``, and antigravity-native (binary + credential
    gated) by a detected Gemini OAuth credential — so nothing is reported
    unconfigured.
    """
    import omnigent.onboarding.gemini_auth as _ga

    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.codex_native._codex_auth_unavailable_reason",
        lambda: None,
    )
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_ready")
    # antigravity-native also needs a credential (not just the ``agy`` binary).
    monkeypatch.setattr(_ga, "gemini_login_detected", lambda: True)
    monkeypatch.setenv("GH_TOKEN", "gho_ready")
    result = configured_harness_map()
    assert all(result.values())


def test_kimi_readiness_keys_off_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi is configured iff the ``kimi`` binary is on PATH.

    Kimi authenticates against Moonshot AI's backend via ``kimi login`` (OAuth
    or a Moonshot API key), which the daemon cannot inspect — so readiness
    keys off binary presence, and the alias ``kimi-code`` resolves to the
    same verdict via canonicalization.
    """
    _no_clis_installed(monkeypatch)
    assert harness_is_configured("kimi") is False
    assert harness_is_configured("kimi-code") is False

    _all_clis_installed(monkeypatch)
    assert harness_is_configured("kimi") is True
    assert harness_is_configured("kimi-code") is True


def test_cursor_readiness_keys_off_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cursor is configured iff a ``CURSOR_API_KEY`` is resolvable — not a binary.

    The cursor harness runs via the always-present ``cursor-sdk`` package, so
    its readiness ignores the ``cursor-agent`` binary entirely: no key → not
    configured (even with every CLI installed); an env key or a stored
    ``cursor:`` block → configured (even with no CLI at all). A wrong verdict
    would either warn a key-configured cursor user "needs setup" or greenlight a
    keyless one that fails at the first turn.
    """
    # No key anywhere (autouse isolation), even with all CLIs present → False.
    _all_clis_installed(monkeypatch)
    assert harness_is_configured("cursor") is False

    # An inherited environment key satisfies it, with no CLI installed.
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_from_env")
    assert harness_is_configured("cursor") is True

    # A key stored in the ``cursor:`` config block also satisfies it.
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("MY_CURSOR_KEY", "crsr_from_config")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"cursor": {"api_key_ref": "env:MY_CURSOR_KEY"}})
    )
    assert harness_is_configured("cursor") is True


def test_native_cursor_keys_off_binary_not_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native Cursor (``omni cursor``) gates on the cursor-agent CLI, not a key.

    The mirror image of :func:`test_cursor_readiness_keys_off_api_key`: native
    Cursor boots the ``cursor-agent`` TUI, so its readiness is the binary on
    ``PATH`` — a ``CURSOR_API_KEY`` (which configures the SDK ``cursor`` harness)
    does not make it launchable. Conflating the two would tell a native-Cursor
    user with a key set "you're ready" and then die booting a CLI that isn't
    installed.
    """
    # A key set but no binary → not configured (the SDK key doesn't help here).
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_from_env")
    assert harness_is_configured("cursor-native") is False
    assert harness_is_configured("native-cursor") is False

    # Binary present → configured, even with no key.
    _all_clis_installed(monkeypatch)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert harness_is_configured("cursor-native") is True
    assert harness_is_configured("native-cursor") is True
