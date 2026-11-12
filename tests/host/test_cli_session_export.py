"""Unit tests for ``omnigent session export``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
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


def _mock_items(pages: list[dict[str, Any]], newest_id: str | None = "msg_2") -> respx.Route:
    """Serve *pages* in order, and *newest_id* to the export's newest-item probe."""
    remaining = list(pages)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("order") == "desc":
            newest = [{"id": newest_id}] if newest_id else []
            return httpx.Response(200, json={"data": newest, "has_more": bool(newest)})
        return httpx.Response(200, json=remaining.pop(0))

    return respx.get(f"{_BASE}/v1/sessions/conv_abc123/items").mock(side_effect=respond)


def _export(out_file: Path, *args: str) -> Any:
    with _patch_server():
        return CliRunner().invoke(
            cli,
            ["session", "export", "--id", "conv_abc123", "--output", str(out_file), *args],
        )


def _patch_server(base_url: str = _BASE) -> Any:
    """Patch the CLI so it uses *base_url* without spawning a real server."""
    return patch(
        "omnigent.cli._resolve_attach_server_url",
        return_value=ServerUrl(base_url),
    )


@respx.mock
def test_session_export_writes_records_by_default(tmp_path: Path) -> None:
    """By default the file is the record_type JSONL every released importer reads."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    _mock_items([_ITEMS_PAGE])

    out_file = tmp_path / "out.jsonl"
    result = _export(out_file)

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in out_file.read_text().splitlines() if line]
    assert lines[0] == {"record_type": "session_meta", **_SESSION_META}
    assert lines[1:] == [{"record_type": "item", **item} for item in _ITEMS_PAGE["data"]]


@respx.mock
def test_session_export_writes_a_transcript_on_request(tmp_path: Path) -> None:
    """--format transcript writes the schema header, then one entry per item."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    _mock_items([_ITEMS_PAGE])

    out_file = tmp_path / "out.jsonl"
    result = _export(out_file, "--format", "transcript")

    assert result.exit_code == 0, result.output
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
    # The blocks are carried once, in ``raw``.
    assert "content" not in entries[1]
    assert entries[1]["raw"]["content"] == [{"type": "output_text", "text": "hi there"}]
    assert entries[1]["agent"] == "my-agent"


@pytest.mark.parametrize("export_format", ["records", "transcript"])
@respx.mock
def test_session_export_leaves_out_items_appended_while_it_runs(
    tmp_path: Path, export_format: str
) -> None:
    """The file stops at the item that was newest when the export started."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    appended = {**_ITEMS_PAGE["data"][1], "id": "msg_3"}
    _mock_items(
        [{**_ITEMS_PAGE, "data": [*_ITEMS_PAGE["data"], appended], "last_id": "msg_3"}],
        newest_id="msg_2",
    )

    out_file = tmp_path / "out.jsonl"
    result = _export(out_file, "--format", export_format)

    assert result.exit_code == 0, result.output
    assert "Exported 2 item(s)" in result.output
    records = [json.loads(line) for line in out_file.read_text().splitlines() if line][1:]
    assert [r["id"] for r in records] == ["msg_1", "msg_2"]


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
    _mock_items([], newest_id=None)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path), _patch_server():
        result = runner.invoke(cli, ["session", "export", "--id", "conv_abc123"])
        assert result.exit_code == 0, result.output
        default_path = Path("conv_abc123.jsonl")
        assert default_path.exists()
        lines = [json.loads(line) for line in default_path.read_text().splitlines() if line]

    assert lines == [{"record_type": "session_meta", **_SESSION_META}]


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
    # First page call (no after param) → page1; second call (after=msg_1) → page2.
    _mock_items([page1, page2])

    out_file = tmp_path / "paged.jsonl"
    result = _export(out_file, "--format", "transcript")
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_file.read_text().splitlines() if line]
    entries = [r for r in records if "kind" in r]
    assert len(entries) == 2
    assert [r["id"] for r in entries] == ["msg_1", "msg_2"]
    assert [r["seq"] for r in entries] == [1, 2]


@respx.mock
def test_session_export_refuses_a_page_without_a_cursor(tmp_path: Path) -> None:
    """A page that claims more results but gives no cursor stops the export.

    The destination and any neighbouring file are left as they were, including
    one named like the export's own staging file.
    """
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    _mock_items([{**_ITEMS_PAGE, "has_more": True, "last_id": None}], newest_id="msg_9")
    out_file = tmp_path / "stuck.jsonl"
    out_file.write_text("previous export\n")
    neighbour = tmp_path / ".stuck.jsonl.partial"
    neighbour.write_text("someone else's file\n")

    result = _export(out_file)

    assert result.exit_code != 0
    assert "without a cursor" in result.output
    assert out_file.read_text() == "previous export\n"
    assert neighbour.read_text() == "someone else's file\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".stuck.jsonl.partial",
        "stuck.jsonl",
    ]


@respx.mock
def test_session_export_replaces_only_its_destination(tmp_path: Path) -> None:
    """A finished export replaces the destination and touches no neighbouring file."""
    respx.get(f"{_BASE}/v1/sessions/conv_abc123").mock(
        return_value=httpx.Response(200, json=_SESSION_META)
    )
    _mock_items([_ITEMS_PAGE])
    out_file = tmp_path / "out.jsonl"
    out_file.write_text("previous export\n")
    neighbour = tmp_path / ".out.jsonl.partial"
    neighbour.write_text("someone else's file\n")

    result = _export(out_file)

    assert result.exit_code == 0, result.output
    assert json.loads(out_file.read_text().splitlines()[0])["record_type"] == "session_meta"
    assert neighbour.read_text() == "someone else's file\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".out.jsonl.partial", "out.jsonl"]
