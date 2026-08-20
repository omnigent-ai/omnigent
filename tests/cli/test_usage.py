"""Tests for the ``omnigent usage`` command and its renderer."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from omnigent.cli import _render_usage, cli

_REPORT: dict[str, Any] = {
    "cost_today": 1.5,
    "cost_last_7d": 3.0,
    "cost_last_30d": 9.0,
    "total_cost_usd": 9.0,
    "sessions": [
        {
            "id": "conv_abc",
            "cost_usd": 1.5,
            "models": {"claude-opus-4-8": 1.5},
        },
        {
            "id": "conv_def",
            "cost_usd": 6.02,
            # Multi-model: costs deliberately don't sum to cost_usd (faithful).
            "models": {
                "claude-opus-4-8": 6.02,
                "system.ai.claude-opus-4-8[1m]": 13.58,
            },
        },
    ],
}


def test_render_usage_summary_and_sessions(capsys: pytest.CaptureFixture[str]) -> None:
    _render_usage(_REPORT, limit=10)
    out = capsys.readouterr().out
    assert "best-effort estimates" in out
    assert "Summary" in out
    assert "Today" in out and "$1.50" in out
    assert "Last 7 days" in out and "$3.00" in out
    assert "Last 30 days" in out
    assert "All time" in out and "$9.00" in out
    assert "Per session" in out and "(last 2)" in out
    assert "conv_abc" in out
    # Both models of the multi-model session are listed verbatim (no collapse).
    assert "claude-opus-4-8" in out
    assert "system.ai.claude-opus-4-8[1m]" in out
    assert "$13.58" in out


def test_render_usage_limit_caps_rows(capsys: pytest.CaptureFixture[str]) -> None:
    _render_usage(_REPORT, limit=1)
    out = capsys.readouterr().out
    assert "(last 1)" in out
    assert "conv_abc" in out
    assert "conv_def" not in out


def test_render_usage_shows_full_id(capsys: pytest.CaptureFixture[str]) -> None:
    full_id = "conv_" + "0123456789abcdef" * 2
    _render_usage(
        {"sessions": [{"id": full_id, "cost_usd": 1.0, "models": {}}]},
        limit=10,
    )
    out = capsys.readouterr().out
    assert full_id in out
    assert "…" not in out


def test_render_usage_empty(capsys: pytest.CaptureFixture[str]) -> None:
    _render_usage({"sessions": []}, limit=10)
    out = capsys.readouterr().out
    assert "Summary" in out
    assert "No usage recorded yet." in out


def test_usage_command_help() -> None:
    result = CliRunner().invoke(cli, ["usage", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.output
    assert "--json" in result.output


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return _REPORT


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, path: str) -> _FakeResponse:
        assert path == "/v1/usage"
        return _FakeResponse()


@pytest.fixture()
def _stub_usage_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda *a, **k: "http://x")
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **k: {})
    monkeypatch.setattr(httpx, "Client", _FakeClient)


@pytest.mark.usefixtures("_stub_usage_fetch")
def test_usage_command_renders_table() -> None:
    result = CliRunner().invoke(cli, ["usage", "--server", "http://x"])
    assert result.exit_code == 0, result.output
    assert "Summary" in result.output
    assert "conv_abc" in result.output


@pytest.mark.usefixtures("_stub_usage_fetch")
def test_usage_command_json() -> None:
    result = CliRunner().invoke(cli, ["usage", "--json", "--server", "http://x"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cost_today"] == 1.5
    assert payload["sessions"][0]["models"] == {"claude-opus-4-8": 1.5}
