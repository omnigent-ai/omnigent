"""Tests for harness readiness checks (``harness_readiness.py``)."""

from __future__ import annotations

import pytest

import omnigent.onboarding.harness_install as hi
from omnigent.onboarding.harness_readiness import (
    configured_harness_map,
    harness_is_configured,
)


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


# CLI-wrapping harnesses are gated on their binary being on PATH.
@pytest.mark.parametrize(
    "harness",
    [
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
        "pi",
        "cursor",
        "mimo",
        "gemini",
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
        "cursor",
        "mimo",
        "gemini",
    }
    assert set(result) == expected_keys


def test_configured_harness_map_gates_only_cli_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no CLI installed, only CLI-wrapping spellings read False.

    SDK spellings (incl. the ``openai-agents-sdk`` workflow spelling and
    the ``claude`` alias) stay True; the native + pi + mimo spellings flip to
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
    for cli in (
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
        "pi",
        "cursor",
        "mimo",
        "gemini",
    ):
        assert result[cli] is False, f"{cli} should be gated on its CLI binary"


def test_configured_harness_map_all_true_with_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every spelling reads True once the CLIs are installed.

    The CLI harnesses pass their binary check and the SDK harnesses are
    ungated, so nothing is reported unconfigured.
    """
    _all_clis_installed(monkeypatch)
    result = configured_harness_map()
    assert all(result.values())
