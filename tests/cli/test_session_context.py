"""Unit tests for ``omnigent session context``."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner, Result

from omnigent.cli import cli
from omnigent.util.server_url import ServerUrl

_BASE = "http://localhost:6767"
_PATH = f"{_BASE}/v1/sessions/conv_abc123/context-breakdown"
_BREAKDOWN = {
    "session_id": "conv_abc123",
    "measured": {"total_tokens": 20000, "context_window": 200000},
    "categories": [
        {
            "kind": "tools",
            "tokens": 5400,
            "source": "estimated",
            "origin": "runner",
            "count": 18,
            "detail": "18 tools · MCP: github",
        },
        {"kind": "history", "tokens": None, "source": "unknown", "origin": "server"},
        {"kind": "other", "tokens": 14600, "source": "estimated", "origin": "server"},
        {"kind": "free", "tokens": 180000, "source": "measured", "origin": "server"},
    ],
    "estimates_exceed_measured": False,
    "attachments": [],
    "bytes_per_token": 4,
    "failed_attempt": {
        "total_tokens": 190000,
        "categories": [
            {"kind": "history", "tokens": 190000, "source": "estimated", "origin": "runner"}
        ],
    },
}


def _invoke(*args: str) -> Result:
    with patch("omnigent.cli._resolve_attach_server_url", return_value=ServerUrl(_BASE)):
        return CliRunner().invoke(cli, ["session", "context", "--id", "conv_abc123", *args])


@respx.mock
def test_session_context_prints_the_rows_the_server_resolved() -> None:
    """The header and every row come from the one breakdown response."""
    respx.get(_PATH).mock(return_value=httpx.Response(200, json=_BREAKDOWN))

    result = _invoke()

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Context window: 20,000 / 200,000 tokens (10% used, measured)"
    rows = [line.split() for line in lines]
    assert ["Tool", "definitions", "~5,400", "estimate"] in rows
    assert "18 tools · MCP: github" in result.output
    assert ["Conversation", "history", "not", "measured"] in rows
    assert ["Other", "~14,600", "estimate"] in rows
    assert ["Free", "space", "180,000", "measured"] in rows
    attempt = lines.index(
        "Last request was too long for the context window (~190,000 tokens when the turn started):"
    )
    assert lines[attempt + 1].split() == ["Conversation", "history", "~190,000", "estimate"]


@respx.mock
def test_session_context_before_any_usage_report() -> None:
    respx.get(_PATH).mock(
        return_value=httpx.Response(
            200, json={**_BREAKDOWN, "measured": None, "failed_attempt": None}
        )
    )

    result = _invoke()

    assert result.output.splitlines()[0] == "Context window: no usage reported yet"
    assert "too long" not in result.output


@respx.mock
def test_session_context_json_is_the_raw_response() -> None:
    respx.get(_PATH).mock(return_value=httpx.Response(200, json=_BREAKDOWN))

    result = _invoke("--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == _BREAKDOWN


@respx.mock
def test_session_context_tells_a_missing_session_from_an_older_server() -> None:
    respx.get(_PATH).mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "Session not found"}}
        )
    )
    missing = _invoke()
    respx.get(_PATH).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    older = _invoke()

    assert missing.exit_code != 0
    assert "Session 'conv_abc123' not found." in missing.output
    assert older.exit_code != 0
    assert "does not report a context breakdown" in older.output


@respx.mock
def test_session_context_reports_a_server_error_without_a_traceback() -> None:
    respx.get(_PATH).mock(return_value=httpx.Response(500, json={}))

    result = _invoke()

    assert result.exit_code != 0
    assert "Could not read the context breakdown (HTTP 500)" in result.output
