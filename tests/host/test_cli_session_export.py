"""Unit tests for ``omnigent session export``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.util.server_url import ServerUrl

_BASE = "http://localhost:6767"
_SPOG_DISPLAY = "https://example.databricks.com/omnigent?o=123"
_SPOG_API = "https://example.databricks.com/api/2.0/omnigent"

_SESSION_META = {
    "id": "conv_abc123",
    "title": "test session",
    "status": "idle",
    "created_at": 1700000000,
    "updated_at": 1700000001,
    "agent_id": None,
    "agent_name": None,
    "items": [],
}

_ITEMS_PAGE = {
    "data": [
        {
            "id": "msg_1",
            "type": "message",
            "status": "completed",
            "response_id": "resp_1",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "id": "msg_2",
            "type": "message",
            "status": "completed",
            "response_id": "resp_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi there"}],
            "model": "my-agent",
        },
    ],
    "first_id": "msg_1",
    "last_id": "msg_2",
    "has_more": False,
}


def _patch_server(base_url: str = _BASE) -> Any:
    """Patch the CLI so it uses *base_url* without spawning a real server."""
    return patch(
        "omnigent.cli._resolve_attach_server_url",
        return_value=ServerUrl(base_url),
    )


@respx.mock
def test_session_export_writes_jsonl(tmp_path: Path) -> None:
    """Export writes the schema header, then one transcript entry per item."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    respx.get(f"{_BASE}/v1/sessions/conv_abc123/items").mock(
        return_value=httpx.Response(200, json=_ITEMS_PAGE)
    )

    out_file = tmp_path / "out.jsonl"
    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(
            cli,
            ["session", "export", "--id", "conv_abc123", "--output", str(out_file)],
        )

    assert result.exit_code == 0, result.output
    assert out_file.exists()

    lines = [json.loads(line) for line in out_file.read_text().splitlines() if line]
    assert len(lines) == 3  # 1 header + 2 entries

    header = lines[0]
    assert header["schema"] == "omnigent.transcript/1"
    assert header["session"] == "conv_abc123"
    assert header["title"] == "test session"
    assert header["created"] == "2023-11-14T22:13:20Z"

    entries = lines[1:]
    assert [e["kind"] for e in entries] == ["message", "message"]
    assert [e["role"] for e in entries] == ["user", "assistant"]
    assert [(e["turn"], e["seq"]) for e in entries] == [(1, 1), (1, 2)]
    assert entries[1]["text"] == "hi there"
    assert entries[1]["content"] == [{"type": "output_text", "text": "hi there"}]
    assert entries[1]["agent"] == "my-agent"


@respx.mock
def test_session_export_spog_url_sends_workspace_selector(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A pasted SPOG URL keeps its ``?o=`` selector in request headers."""
    monkeypatch.setattr(
        "omnigent.cli._workspace_api_server_url",
        lambda _server: _SPOG_API,
    )
    session_route = respx.get(f"{_SPOG_API}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    respx.get(f"{_SPOG_API}/v1/sessions/conv_abc123/items").mock(
        return_value=httpx.Response(200, json={**_ITEMS_PAGE, "data": []})
    )

    result = CliRunner().invoke(
        cli,
        [
            "session",
            "export",
            "--id",
            "conv_abc123",
            "--output",
            str(tmp_path / "out.jsonl"),
            "--server",
            _SPOG_DISPLAY,
        ],
    )

    assert result.exit_code == 0, result.output
    assert session_route.calls[0].request.headers["X-Databricks-Org-Id"] == "123"


@respx.mock
def test_session_export_default_filename(tmp_path: Path) -> None:
    """Without --output, the file is named <session_id>.jsonl in cwd."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    respx.get(f"{_BASE}/v1/sessions/conv_abc123/items").mock(
        return_value=httpx.Response(200, json={**_ITEMS_PAGE, "data": [], "has_more": False})
    )

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path), _patch_server():
        result = runner.invoke(cli, ["session", "export", "--id", "conv_abc123"])
        assert result.exit_code == 0, result.output
        default_path = Path("conv_abc123.jsonl")
        assert default_path.exists()
        lines = [json.loads(line) for line in default_path.read_text().splitlines() if line]

    assert len(lines) == 1
    assert lines[0]["schema"] == "omnigent.transcript/1"
    assert lines[0]["session"] == "conv_abc123"


@respx.mock
def test_session_export_missing_session_errors(tmp_path: Path) -> None:
    """Export of an unknown session id exits non-zero with a clear message."""
    respx.get(f"{_BASE}/v1/sessions/conv_doesnotexist").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(
            cli,
            [
                "session",
                "export",
                "--id",
                "conv_doesnotexist",
                "--output",
                str(tmp_path / "out.jsonl"),
            ],
        )

    assert result.exit_code != 0
    assert "conv_doesnotexist" in result.output


@respx.mock
def test_session_export_items_ordered_ascending(tmp_path: Path) -> None:
    """Items in the JSONL appear in ascending position order (user then assistant)."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    respx.get(f"{_BASE}/v1/sessions/conv_abc123/items").mock(
        return_value=httpx.Response(200, json=_ITEMS_PAGE)
    )

    out_file = tmp_path / "ordered.jsonl"
    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(
            cli,
            ["session", "export", "--id", "conv_abc123", "--output", str(out_file)],
        )
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_file.read_text().splitlines() if line]
    entries = [r for r in records if "kind" in r]
    assert len(entries) == 2
    assert entries[0]["role"] == "user"
    assert entries[1]["role"] == "assistant"


@respx.mock
def test_session_export_pagination(tmp_path: Path) -> None:
    """Export follows has_more cursors to fetch all pages."""
    page1 = {
        "data": [
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "response_id": "r1",
                "role": "user",
                "content": [],
            }
        ],
        "first_id": "msg_1",
        "last_id": "msg_1",
        "has_more": True,
    }
    page2 = {
        "data": [
            {
                "id": "msg_2",
                "type": "message",
                "status": "completed",
                "response_id": "r1",
                "role": "assistant",
                "content": [],
                "model": "ag",
            }
        ],
        "first_id": "msg_2",
        "last_id": "msg_2",
        "has_more": False,
    }
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    # First call (no after param) → page1; second call (after=msg_1) → page2.
    items_route = respx.get(f"{_BASE}/v1/sessions/conv_abc123/items")
    items_route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]

    out_file = tmp_path / "paged.jsonl"
    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(
            cli,
            ["session", "export", "--id", "conv_abc123", "--output", str(out_file)],
        )
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_file.read_text().splitlines() if line]
    entries = [r for r in records if "kind" in r]
    assert len(entries) == 2
    assert [r["id"] for r in entries] == ["msg_1", "msg_2"]
    assert [r["seq"] for r in entries] == [1, 2]
