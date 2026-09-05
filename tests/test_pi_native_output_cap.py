"""Endpoint output-token cap discovery and clamping for Pi-native config."""

from __future__ import annotations

import httpx
import pytest

from omnigent import model_catalog
from omnigent import pi_native_credentials as pnc


def _transport(status: int, text: str) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=text))


def test_probe_parses_cannot_exceed_format() -> None:
    t = _transport(400, '{"message":"max_tokens (1048576) cannot exceed 65536."}')
    cap = model_catalog.probe_output_token_cap(
        "https://ws/ai-gateway/mlflow/v1", "tok", "system.ai.kimi-k3", transport=t
    )
    assert cap == 65536


def test_probe_parses_max_output_tokens_format() -> None:
    t = _transport(
        400, "max_new_tokens 99999999 cannot be greater than max_output_tokens 25000.\n"
    )
    cap = model_catalog.probe_output_token_cap(
        "https://ws/ai-gateway/mlflow/v1", "tok", "system.ai.gpt-oss-120b", transport=t
    )
    assert cap == 25000


def test_probe_returns_none_when_accepted() -> None:
    t = _transport(200, '{"choices":[]}')
    assert model_catalog.probe_output_token_cap("https://ws/x", "tok", "m", transport=t) is None


def test_probe_returns_none_on_unparseable_error() -> None:
    t = _transport(400, '{"message":"some unrelated validation error"}')
    assert model_catalog.probe_output_token_cap("https://ws/x", "tok", "m", transport=t) is None


def test_clamp_lowers_oversized_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog, "probe_output_token_cap", lambda *a, **k: 65536)
    entries = [{"id": "system.ai.kimi-k3", "maxTokens": 1048576}]
    pnc._clamp_entries_to_output_caps("https://ws", "tok", entries)
    assert entries[0]["maxTokens"] == 65536


def test_clamp_keeps_smaller_catalog_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog, "probe_output_token_cap", lambda *a, **k: 131072)
    entries = [{"id": "m", "maxTokens": 65536}]
    pnc._clamp_entries_to_output_caps("https://ws", "tok", entries)
    assert entries[0]["maxTokens"] == 65536


def test_clamp_skips_small_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[str] = []
    monkeypatch.setattr(
        model_catalog, "probe_output_token_cap", lambda *a, **k: probed.append(a) or 100
    )
    entries = [{"id": "m", "maxTokens": 8000}]
    pnc._clamp_entries_to_output_caps("https://ws", "tok", entries)
    assert entries[0]["maxTokens"] == 8000
    assert probed == []


def test_clamp_keeps_value_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog, "probe_output_token_cap", lambda *a, **k: None)
    entries = [{"id": "m", "maxTokens": 131072}]
    pnc._clamp_entries_to_output_caps("https://ws", "tok", entries)
    assert entries[0]["maxTokens"] == 131072
