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
    """
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: False)
    cmap = hr.configured_harness_map()
    assert cmap.get("pi-native") is False
    assert cmap.get("pi") is False


def test_configured_harness_map_exposes_kiro_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness map carries Kiro native keys for the web picker lookup."""
    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: False)
    cmap = hr.configured_harness_map()
    assert cmap.get("kiro-native") is False
    assert cmap.get("native-kiro") is False


def test_codex_availability_reports_provider_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The picker map surfaces ``provider-unreachable`` for a dead loopback proxy.

    Even with the codex binary present and a credential configured, the resolved
    provider's endpoint being unreachable must make the picker warn instead of
    reporting codex as available.
    """
    import omnigent.codex_native as codex_native

    monkeypatch.setattr(codex_native, "_codex_auth_unavailable_reason", lambda: None)
    monkeypatch.setattr(
        codex_native, "_codex_provider_unreachable_reason", lambda: "provider-unreachable"
    )
    cmap = hr.configured_harness_map()
    assert cmap["codex-native"] == "provider-unreachable"
    assert cmap["codex"] == "provider-unreachable"


def test_codex_availability_prefers_auth_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing credential is reported before the reachability check is consulted."""
    import omnigent.codex_native as codex_native

    monkeypatch.setattr(codex_native, "_codex_auth_unavailable_reason", lambda: "needs-auth")
    monkeypatch.setattr(
        codex_native,
        "_codex_provider_unreachable_reason",
        lambda: pytest.fail("reachability must not run when auth already failed"),
    )
    assert hr._harness_availability("codex-native") == "needs-auth"


def test_codex_launch_gate_rejects_unreachable_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch gate refuses codex when the resolved provider is unreachable."""
    import omnigent.codex_native as codex_native

    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(
        codex_native, "_codex_provider_unreachable_reason", lambda: "provider-unreachable"
    )
    assert hr.harness_is_configured("codex-native") is False


def test_codex_launch_gate_allows_reachable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch gate allows codex when the binary is present and provider reachable."""
    import omnigent.codex_native as codex_native

    monkeypatch.setattr(hr, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(codex_native, "_codex_provider_unreachable_reason", lambda: None)
    assert hr.harness_is_configured("codex-native") is True
