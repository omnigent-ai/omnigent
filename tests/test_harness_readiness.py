"""Tests for omnigent.onboarding.harness_readiness gating."""

from __future__ import annotations

import pytest

from omnigent.onboarding import harness_readiness as hr


@pytest.mark.parametrize("harness", ["pi", "pi-native", "native-pi"])
def test_pi_harnesses_gate_on_pi_cli(harness: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``pi`` and ``pi-native`` are both gated on the ``pi`` CLI being installed.

    Regression guard: ``pi-native`` has no ``_HARNESS_FAMILY`` entry (pi uses
    the ``PI_SURFACE`` sentinel), so it used to hit the unknown-harness
    fail-open branch and report configured even when ``pi`` was missing — the
    host pre-spawn check then let a doomed launch through. Both spellings must
    track ``harness_cli_installed``.
    """
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: False)
    assert hr.harness_is_configured(harness) is False

    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: True)
    assert hr.harness_is_configured(harness) is True


@pytest.mark.parametrize("harness", ["kiro-native", "native-kiro"])
def test_kiro_native_harnesses_gate_on_kiro_cli(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native Kiro is gated on the ``kiro-cli`` binary being installed."""
    calls: list[str] = []

    def _installed(key: str) -> bool:
        calls.append(key)
        return False

    monkeypatch.setattr(hr, "harness_cli_installed", _installed)
    assert hr.harness_is_configured(harness) is False
    assert calls[-1] == hr.KIRO_KEY

    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: True)
    assert hr.harness_is_configured(harness) is True


def test_sdk_and_unknown_harnesses_still_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK and unknown harnesses are never gated, even with no CLI installed.

    Pins that the pi-native fix narrowed only the pi surface — SDK harnesses
    (runtime/ambient credentials) and unknown harnesses must keep failing open
    so a working launch is never blocked.
    """
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: False)
    assert hr.harness_is_configured("claude-sdk") is True
    assert hr.harness_is_configured("openai-agents") is True
    assert hr.harness_is_configured("totally-unknown-harness") is True


def test_configured_harness_map_exposes_pi_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness map carries a ``pi-native`` key for the web picker lookup.

    The agent picker warns "needs setup" by looking up the agent's harness
    (``pi-native``) in this map; without the key the Pi row could never warn.
    A missing binary now reports the richer ``"binary-missing"`` reason (Pi
    gained the credential axis) rather than a bare ``False``.
    """
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: False)
    cmap = hr.configured_harness_map()
    assert cmap.get("pi-native") == "binary-missing"
    assert cmap.get("pi") == "binary-missing"


def test_configured_harness_map_pi_installed_no_provider_needs_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi installed but with no configured provider reports ``"needs-auth"``.

    Pi has no CLI login — its only credential is an omnigent-managed provider —
    so an installed binary with no provider is the yellow "installed but not
    configured" state the setup dialog offers an "Add key" action for.
    """
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(hr, "_family_provider_configured", lambda _h: False)
    cmap = hr.configured_harness_map()
    assert cmap.get("pi") == "needs-auth"
    assert cmap.get("pi-native") == "needs-auth"


def test_configured_harness_map_pi_installed_with_provider_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi installed AND a provider configured reports ready (``True``)."""
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(hr, "_family_provider_configured", lambda _h: True)
    cmap = hr.configured_harness_map()
    assert cmap.get("pi") is True
    assert cmap.get("pi-native") is True


def test_configured_harness_map_exposes_kiro_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness map carries Kiro native keys for the web picker lookup."""
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: False)
    cmap = hr.configured_harness_map()
    assert cmap.get("kiro-native") is False
    assert cmap.get("native-kiro") is False
