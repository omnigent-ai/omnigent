"""Tests for the ``omnigent import`` CLI command.

Exercise the command end-to-end with ``CliRunner``, stubbing the network seam
(``omnigent.cli._host_http_json``) and server resolution — no live server. The
parser itself is covered by ``tests/test_transcript_import.py``; here we verify
the command's parse → resolve → list-agents → POST → report flow and its error
handling.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnigent.cli import _HostHttpResult, cli


def _write_claude_transcript(tmp_path: Path) -> Path:
    """Write a small valid Claude transcript and return its path."""
    path = tmp_path / "session.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "inspect TODO.md"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "text", "text": "Reading it."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "TODO.md"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "body"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _fake_http(
    calls: list[dict[str, Any]],
    *,
    agents_status: int = 200,
    agents_body: Any = None,
    create_status: int = 200,
    create_body: Any = None,
) -> Callable[..., _HostHttpResult]:
    """Build a stub for ``_host_http_json`` that records calls and routes by path."""
    resolved_agents = (
        agents_body if agents_body is not None else {"data": [{"id": "ag_1", "name": "a"}]}
    )
    resolved_create = (
        create_body if create_body is not None else {"id": "conv_abc", "object": "session"}
    )

    def fake(
        *,
        base_url: str,
        method: str,
        path: str,
        params: Any = None,
        json_body: Any = None,
        timeout_s: float = 10.0,
    ) -> _HostHttpResult:
        calls.append(
            {"method": method, "path": path, "json_body": json_body, "base_url": base_url}
        )
        if path == "/v1/agents":
            return _HostHttpResult(status_code=agents_status, body=resolved_agents)
        if path == "/v1/sessions":
            return _HostHttpResult(status_code=create_status, body=resolved_create)
        return _HostHttpResult(status_code=404, body={"detail": "unexpected path"})

    return fake


def _patch_server(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    """Force server resolution to *url* (None = no server available)."""
    monkeypatch.setattr("omnigent.cli._resolve_host_server", lambda _server: url)
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)


def test_import_dry_run_summarizes_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--dry-run`` parses and summarizes but makes no server calls."""
    transcript = _write_claude_transcript(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http(calls))

    result = CliRunner().invoke(cli, ["import", str(transcript), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Parsed claude transcript: 2 messages, 1 tool calls, 1 tool outputs" in result.output
    assert "Dry run" in result.output
    assert calls == []


def test_import_creates_session_and_reports_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful import lists agents, POSTs the history, and prints the id."""
    transcript = _write_claude_transcript(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http(calls))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code == 0, result.output
    assert "conv_abc" in result.output
    paths = [c["path"] for c in calls]
    assert paths == ["/v1/agents", "/v1/sessions"]

    create = next(c for c in calls if c["path"] == "/v1/sessions")
    body = create["json_body"]
    assert body["agent_id"] == "ag_1"
    assert body["labels"] == {"imported_from": "claude"}
    assert body["title"] == "inspect TODO.md"
    types = [item["type"] for item in body["initial_items"]]
    assert types == ["message", "message", "function_call", "function_call_output"]
    # The assistant/function items carry 'agent' (not the output-only 'model').
    assert body["initial_items"][1]["data"]["agent"] == "claude-sonnet-4-6"
    assert "model" not in body["initial_items"][1]["data"]


def test_import_with_explicit_agent_skips_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--agent`` binds directly and avoids the GET /v1/agents lookup."""
    transcript = _write_claude_transcript(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http(calls))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(
        cli, ["import", str(transcript), "--server", "http://test.local", "--agent", "ag_explicit"]
    )

    assert result.exit_code == 0, result.output
    assert [c["path"] for c in calls] == ["/v1/sessions"]
    create = next(c for c in calls if c["path"] == "/v1/sessions")
    assert create["json_body"]["agent_id"] == "ag_explicit"


def test_import_custom_title_and_source_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--title`` and ``--source`` override detection and the synthesized title."""
    transcript = _write_claude_transcript(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http(calls))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(
        cli,
        [
            "import",
            str(transcript),
            "--server",
            "http://test.local",
            "--source",
            "claude",
            "--title",
            "My import",
        ],
    )

    assert result.exit_code == 0, result.output
    create = next(c for c in calls if c["path"] == "/v1/sessions")
    assert create["json_body"]["title"] == "My import"


def test_import_no_server_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no --server and no configured/local server, the command fails loud."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http([]))
    _patch_server(monkeypatch, None)

    result = CliRunner().invoke(cli, ["import", str(transcript)])

    assert result.exit_code != 0
    assert "No server specified" in result.output


def test_import_unparseable_transcript_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An undetectable transcript surfaces a clean error and no network call."""
    path = tmp_path / "junk.jsonl"
    path.write_text(json.dumps({"unrelated": "object"}) + "\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http(calls))

    result = CliRunner().invoke(cli, ["import", str(path), "--server", "http://test.local"])

    assert result.exit_code != 0
    assert "could not detect transcript format" in result.output
    assert calls == []


def test_import_server_error_surfaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-2xx create response surfaces the status and error text."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._host_http_json",
        _fake_http([], create_status=500, create_body={"detail": "boom"}),
    )
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code != 0
    assert "Import failed (500)" in result.output
    assert "boom" in result.output


def test_import_no_agents_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the server has no agents and none is given, the command fails loud."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http([], agents_body={"data": []}))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code != 0
    assert "No agents are registered" in result.output


def test_import_multiple_agents_picks_first_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With several agents and no --agent, the first is bound and a warning shown."""
    transcript = _write_claude_transcript(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.cli._host_http_json",
        _fake_http(calls, agents_body={"data": [{"id": "ag_1"}, {"id": "ag_2"}]}),
    )
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code == 0, result.output
    assert "Multiple agents available" in result.output
    create = next(c for c in calls if c["path"] == "/v1/sessions")
    assert create["json_body"]["agent_id"] == "ag_1"


def test_import_agents_list_failure_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-200 GET /v1/agents surfaces a clean error with the --agent hint."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http([], agents_status=500))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code != 0
    assert "Could not list agents" in result.output
    assert "--agent" in result.output


def test_import_success_without_id_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 2xx create whose body lacks an id fails loud rather than printing junk."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr(
        "omnigent.cli._host_http_json", _fake_http([], create_body={"object": "session"})
    )
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code != 0
    assert "returned no session id" in result.output


def test_import_accepts_201_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 201 Created response is treated as success (not only 200)."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http([], create_status=201))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner().invoke(cli, ["import", str(transcript), "--server", "http://test.local"])

    assert result.exit_code == 0, result.output
    assert "conv_abc" in result.output


def test_import_bare_session_id_only_on_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the bare session id reaches stdout; summaries go to stderr."""
    transcript = _write_claude_transcript(tmp_path)
    monkeypatch.setattr("omnigent.cli._host_http_json", _fake_http([]))
    _patch_server(monkeypatch, "http://test.local")

    result = CliRunner(mix_stderr=False).invoke(
        cli, ["import", str(transcript), "--server", "http://test.local"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "conv_abc"
    assert "Imported" in result.stderr
