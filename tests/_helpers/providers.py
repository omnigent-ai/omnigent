"""
Hermetic provider resolution for tests that assert resolved models or sources.

A developer machine can change provider resolution through three independent
channels: the user config under ``OMNIGENT_CONFIG_HOME``, a saved inference
overlay named by ``OMNIGENT_INFERENCE_CONFIG``, and ambient detection (vendor
API keys, CLI logins, a local Ollama). CI sets none of them, so a test that
forgets one passes in CI and fails locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def isolate_provider_resolution(monkeypatch: pytest.MonkeyPatch, config_home: Path) -> Path:
    """
    Resolve providers only from *config_home*, ignoring machine-global state.

    Points ``OMNIGENT_CONFIG_HOME`` at *config_home*, disables the keyring,
    drops any inherited inference overlay, and reports no ambient providers.
    Callers write their own ``config.yaml`` (if any) into the returned directory.

    :param monkeypatch: The test's monkeypatch fixture; it undoes every change.
    :param config_home: Directory to use as the user config home, e.g.
        ``tmp_path``. Created if missing.
    :returns: *config_home*, so callers can write ``config_home / "config.yaml"``.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("OMNIGENT_INFERENCE_CONFIG", raising=False)
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    return config_home
