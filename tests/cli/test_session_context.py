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
_BREAKDOWN = {
    "session_id": "conv_abc123",
    "system_prompt_tokens": 900,
    "skill_count": 0,
    "skill_listing_tokens": 0,
    "tools": {"mcp_servers": [], "local_tool_count": 0, "builtin_tool_count": 2},
    "attachments": [
        {"filename": "chart.png", "delivery": "inline", "estimated_tokens": 1600},
        # A control sequence in a filename never reaches the terminal.
        {"filename": "trace\x1b[31m.gz", "delivery": "workspace"},
    ],
    "history": {"estimated_tokens": 4000, "item_count": 6},
    "bytes_per_token": 4,
    "reported": None,
    "failed_attempt": {"history_tokens": 190000, "history_item_count": 300, "tool_count": 0},
}


def _mock_server(breakdown_status: int = 200) -> None:
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(
            200, json={"id": "conv_abc123", "last_total_tokens": 20000, "context_window": 200000}
        )
    )
    respx.get(f"{_BASE}/v1/sessions/conv_abc123/context-breakdown").mock(
        return_value=httpx.Response(breakdown_status, json=_BREAKDOWN)
    )


def _invoke(*args: str) -> Result:
    with patch("omnigent.cli._resolve_attach_server_url", return_value=ServerUrl(_BASE)):
        return CliRunner().invoke(cli, ["session", "context", "--id", "conv_abc123", *args])


@respx.mock
def test_session_context_prints_the_breakdown() -> None:
    """Without a runner report the server's estimates fill the rows, then any failed attempt."""
    _mock_server()

    result = _invoke()

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Context window: 20,000 / 200,000 tokens (10% used, measured)"
    assert any(line.split() == ["System", "prompt", "~900", "estimate"] for line in lines)
    assert "2 built-in tools" in result.output
    assert any(line.split() == ["trace[31m.gz", "in", "workspace"] for line in lines)
    assert "\x1b" not in result.output
    # Other is the measured total minus the itemized estimates: 20,000 - 6,500.
    assert any(line.split() == ["Other", "~13,500", "estimate"] for line in lines)
    assert any(line.split() == ["Free", "space", "180,000", "measured"] for line in lines)
    assert "Last request was too long for the context window (~190,000 tokens attempted):" in (
        result.output
    )


@respx.mock
def test_session_context_json_is_the_raw_response() -> None:
    _mock_server()

    result = _invoke("--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == _BREAKDOWN


@respx.mock
def test_session_context_explains_an_older_server() -> None:
    _mock_server(breakdown_status=404)

    result = _invoke()

    assert result.exit_code != 0
    assert "does not report a context breakdown" in result.output


@respx.mock
def test_session_context_reports_a_server_error_without_a_traceback() -> None:
    _mock_server(breakdown_status=500)

    result = _invoke()

    assert result.exit_code != 0
    assert "Could not read the context breakdown (HTTP 500)" in result.output
