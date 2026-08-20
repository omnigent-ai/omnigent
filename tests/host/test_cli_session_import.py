"""Unit tests for ``omnigent session import``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import _import_item_payload, cli

_BASE = "http://localhost:6767"


def _patch_server(base_url: str = _BASE) -> Any:
    """Patch the CLI so it uses *base_url* without spawning a real server."""
    return patch("omnigent.cli._resolve_attach_server", return_value=base_url)


def _write_export(path: Path, *, meta: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Write a session-export JSONL: one meta line + one line per item."""
    lines = [{"record_type": "session_meta", **meta}]
    lines += [{"record_type": "item", **item} for item in items]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_import_item_payload_dealiases_model_to_agent() -> None:
    """An assistant item's ``model`` alias maps back to ``agent`` and validates."""
    exported = {
        "record_type": "item",
        "id": "msg_2",
        "type": "message",
        "status": "completed",
        "response_id": "resp_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
        "model": "claude-native-ui",
    }

    payload = _import_item_payload(exported)

    assert payload["type"] == "message"
    # Envelope fields dropped; ``model`` de-aliased to ``agent``.
    assert "id" not in payload["data"]
    assert "status" not in payload["data"]
    assert "model" not in payload["data"]
    assert payload["data"]["agent"] == "claude-native-ui"
    assert payload["data"]["role"] == "assistant"


def test_import_item_payload_preserves_compaction_model() -> None:
    """``compaction.model`` is a real field, not the agent alias — keep it."""
    exported = {
        "record_type": "item",
        "id": "cmp_1",
        "type": "compaction",
        "status": "completed",
        "response_id": "resp_1",
        "summary": "did stuff",
        "last_item_id": "msg_9",
        "model": "openai/gpt-4o",
        "token_count": 42,
    }

    payload = _import_item_payload(exported)

    # Must NOT be renamed to agent, and must survive.
    assert payload["data"]["model"] == "openai/gpt-4o"
    assert "agent" not in payload["data"]


def test_import_item_payload_preserves_routing_decision_model_and_agent() -> None:
    """``routing_decision`` has both a required ``model`` and a real ``agent``."""
    exported = {
        "record_type": "item",
        "id": "rt_1",
        "type": "routing_decision",
        "status": "completed",
        "response_id": "resp_1",
        "model": "databricks-claude-opus-4-8",
        "applied": True,
        "rationale": "deep reasoning",
        "agent": "claude_code",
    }

    payload = _import_item_payload(exported)

    # Both fields must survive intact — no collision, no loss.
    assert payload["data"]["model"] == "databricks-claude-opus-4-8"
    assert payload["data"]["agent"] == "claude_code"


def test_import_item_payload_rejects_invalid_item() -> None:
    """A payload that does not validate for its type raises a click error."""
    import click

    bad = {"record_type": "item", "type": "function_call", "id": "x"}  # missing name/call_id
    try:
        _import_item_payload(bad)
    except click.ClickException as exc:
        assert "invalid" in str(exc).lower()
    else:
        raise AssertionError("expected ClickException for an invalid item")


@respx.mock
def test_session_import_creates_session(tmp_path: Path) -> None:
    """Import reads the JSONL and POSTs a create with de-aliased initial_items."""
    src = tmp_path / "s.jsonl"
    _write_export(
        src,
        meta={"id": "conv_old", "title": "orig", "agent_id": "ag_abc", "harness": "claude-native"},
        items=[
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
                "content": [{"type": "output_text", "text": "hi"}],
                "model": "claude-native-ui",
            },
        ],
    )

    route = respx.post(f"{_BASE}/v1/sessions").mock(
        return_value=httpx.Response(200, json={"id": "conv_new", "agent_id": "ag_abc"})
    )

    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(cli, ["session", "import", "-i", str(src)])

    assert result.exit_code == 0, result.output
    assert "conv_new" in result.output
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["agent_id"] == "ag_abc"
    assert body["title"] == "orig"
    assert body["host_type"] == "external"
    assert "host_id" not in body
    assert len(body["initial_items"]) == 2
    # The assistant item's model alias was de-aliased to agent before send.
    assert body["initial_items"][1]["data"]["agent"] == "claude-native-ui"


@respx.mock
def test_session_import_falls_back_to_native_agent(tmp_path: Path) -> None:
    """When the exported agent_id 404s, import retries with the native agent."""
    from omnigent.db.utils import builtin_agent_id
    from omnigent.native_coding_agents import native_coding_agent_for_harness

    native = native_coding_agent_for_harness("claude-native")
    assert native is not None
    fallback_id = builtin_agent_id(native.agent_name)

    src = tmp_path / "s.jsonl"
    _write_export(
        src,
        meta={"id": "conv_old", "agent_id": "ag_missing", "harness": "claude-native"},
        items=[
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "response_id": "resp_1",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
    )

    seen_agent_ids: list[str] = []

    def _responder(request: httpx.Request) -> httpx.Response:
        agent_id = json.loads(request.content)["agent_id"]
        seen_agent_ids.append(agent_id)
        if agent_id == "ag_missing":
            return httpx.Response(404, json={"error": {"message": "Agent not found"}})
        return httpx.Response(200, json={"id": "conv_new"})

    respx.post(f"{_BASE}/v1/sessions").mock(side_effect=_responder)

    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(cli, ["session", "import", "-i", str(src)])

    assert result.exit_code == 0, result.output
    assert "conv_new" in result.output
    # First tried the exported id, then fell back to the native agent id.
    assert seen_agent_ids == ["ag_missing", fallback_id]


def test_session_import_missing_meta_errors(tmp_path: Path) -> None:
    """A file with no session_meta line is rejected with a clear message."""
    src = tmp_path / "bad.jsonl"
    src.write_text(json.dumps({"record_type": "item", "type": "message"}) + "\n", encoding="utf-8")

    runner = CliRunner()
    with _patch_server():
        result = runner.invoke(cli, ["session", "import", "-i", str(src)])

    assert result.exit_code != 0
    assert "session_meta" in result.output
