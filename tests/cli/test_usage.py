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
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        assert path == "/v1/usage"
        # Return report with pagination fields (has_more=False for single page)
        report = dict(_REPORT)
        report["sessions_has_more"] = False
        report["sessions_last_id"] = None
        return _FakeResponse(report)


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


class _PaginatedClient:
    """Mock client that returns multiple pages of sessions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.call_count = 0

    def __enter__(self) -> _PaginatedClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        assert path == "/v1/usage"
        self.call_count += 1
        params = params or {}

        if self.call_count == 1:
            # First page
            return _FakeResponse(
                {
                    "cost_today": 1.5,
                    "cost_last_7d": 3.0,
                    "cost_last_30d": 9.0,
                    "total_cost_usd": 9.0,
                    "sessions": [
                        {"id": "conv_page1_a", "cost_usd": 1.0, "models": {}},
                        {"id": "conv_page1_b", "cost_usd": 0.5, "models": {}},
                    ],
                    "sessions_has_more": True,
                    "sessions_last_id": "conv_page1_b",
                }
            )
        elif params.get("after") == "conv_page1_b":
            # Second page
            return _FakeResponse(
                {
                    "cost_today": 1.5,
                    "cost_last_7d": 3.0,
                    "cost_last_30d": 9.0,
                    "total_cost_usd": 9.0,
                    "sessions": [
                        {"id": "conv_page2_a", "cost_usd": 0.75, "models": {}},
                    ],
                    "sessions_has_more": False,
                    "sessions_last_id": "conv_page2_a",
                }
            )
        else:
            raise AssertionError(f"Unexpected pagination state: {params}")


@pytest.fixture()
def _stub_paginated_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda *a, **k: "http://x")
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **k: {})
    monkeypatch.setattr(httpx, "Client", _PaginatedClient)


@pytest.mark.usefixtures("_stub_paginated_fetch")
def test_usage_command_fetches_all_pages() -> None:
    """CLI should loop through all pages and accumulate all sessions."""
    result = CliRunner().invoke(cli, ["usage", "--json", "--server", "http://x"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Should have all 3 sessions from both pages
    assert len(payload["sessions"]) == 3
    assert payload["sessions"][0]["id"] == "conv_page1_a"
    assert payload["sessions"][1]["id"] == "conv_page1_b"
    assert payload["sessions"][2]["id"] == "conv_page2_a"


@pytest.mark.usefixtures("_stub_paginated_fetch")
def test_usage_command_display_limit_still_works() -> None:
    """--limit should still cap the rendered table, even with multiple pages."""
    result = CliRunner().invoke(cli, ["usage", "--limit", "2", "--server", "http://x"])
    assert result.exit_code == 0, result.output
    # Should show "(last 2)" even though 3 sessions were fetched
    assert "(last 2)" in result.output
    assert "conv_page1_a" in result.output
    assert "conv_page1_b" in result.output
    # Third session should not be shown (limited to 2)
    assert "conv_page2_a" not in result.output


class _DuplicatingClient:
    """Mock client that returns duplicate sessions across pages (due to concurrent updates)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.call_count = 0

    def __enter__(self) -> _DuplicatingClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        assert path == "/v1/usage"
        self.call_count += 1
        params = params or {}

        if self.call_count == 1:
            # First page
            return _FakeResponse(
                {
                    "cost_today": 1.5,
                    "cost_last_7d": 3.0,
                    "cost_last_30d": 9.0,
                    "total_cost_usd": 9.0,
                    "sessions": [
                        {"id": "conv_a", "cost_usd": 1.0, "models": {}},
                        {"id": "conv_b", "cost_usd": 0.5, "models": {}},
                    ],
                    "sessions_has_more": True,
                    "sessions_last_id": "conv_b",
                }
            )
        elif params.get("after") == "conv_b":
            # Second page - conv_b appears again due to updated_at changing
            return _FakeResponse(
                {
                    "cost_today": 1.5,
                    "cost_last_7d": 3.0,
                    "cost_last_30d": 9.0,
                    "total_cost_usd": 9.0,
                    "sessions": [
                        {"id": "conv_b", "cost_usd": 0.5, "models": {}},  # Duplicate!
                        {"id": "conv_c", "cost_usd": 0.75, "models": {}},
                    ],
                    "sessions_has_more": False,
                    "sessions_last_id": "conv_c",
                }
            )
        else:
            raise AssertionError(f"Unexpected pagination state: {params}")


@pytest.fixture()
def _stub_duplicating_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda *a, **k: "http://x")
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **k: {})
    monkeypatch.setattr(httpx, "Client", _DuplicatingClient)


@pytest.mark.usefixtures("_stub_duplicating_fetch")
def test_usage_command_deduplicates_sessions() -> None:
    """CLI should de-duplicate sessions that appear in multiple pages."""
    result = CliRunner().invoke(cli, ["usage", "--json", "--server", "http://x"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Should have only 3 unique sessions (conv_a, conv_b, conv_c), not 4
    assert len(payload["sessions"]) == 3
    session_ids = {s["id"] for s in payload["sessions"]}
    assert session_ids == {"conv_a", "conv_b", "conv_c"}
