"""Integration tests for ``GET /v1/sessions/{id}/export``.

The route streams a session as an ``omnigent.transcript/1`` file. Items are
seeded through ``POST /v1/sessions`` ``initial_items`` so the test exercises
the same persisted rows the CLI and web export read.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.export import TRANSCRIPT_SCHEMA, read_transcript
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_INITIAL_ITEMS: list[dict[str, Any]] = [
    {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "churn by region"}]},
    },
    {
        "type": "reasoning",
        "data": {
            "agent": "test-agent",
            "summary": [{"type": "summary_text", "text": "Group by region"}],
            "encrypted_content": "gAAAA",
        },
    },
    {
        "type": "function_call",
        "data": {
            "agent": "test-agent",
            "name": "sql",
            "arguments": json.dumps({"query": "select region, count(*) from churn group by 1"}),
            "call_id": "call_1",
        },
    },
    {
        "type": "function_call_output",
        "data": {"call_id": "call_1", "output": "west,12\neast,7"},
    },
    {
        "type": "message",
        "data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "West churns most."}],
            "agent": "test-agent",
        },
    },
]


async def _seed_session(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(client, name="export-agent")
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "title": "Churn analysis",
            "host_type": "external",
            "initial_items": _INITIAL_ITEMS,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_export_streams_a_versioned_transcript(client: httpx.AsyncClient) -> None:
    """The download is NDJSON: a schema header, then every item in order."""
    session_id = await _seed_session(client)

    resp = await client.get(f"/v1/sessions/{session_id}/export")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["content-disposition"] == f'attachment; filename="{session_id}.jsonl"'

    transcript = read_transcript(resp.text.splitlines(keepends=True))
    header = transcript.header
    assert header.schema_ == TRANSCRIPT_SCHEMA
    assert header.session == session_id
    assert header.title == "Churn analysis"
    assert header.agent == "export-agent"
    assert header.exported is not None

    kinds = [e.kind for e in transcript.entries]
    assert kinds == ["message", "reasoning", "tool_call", "tool_result", "message"]
    assert [e.seq for e in transcript.entries] == [1, 2, 3, 4, 5]
    assert transcript.entries[0].text == "churn by region"

    reasoning = transcript.entries[1]
    assert reasoning.sealed is True
    assert reasoning.text == "Group by region"

    call, result = transcript.entries[2], transcript.entries[3]
    assert call.tool == "sql"
    assert call.tool_input == {"query": "select region, count(*) from churn group by 1"}
    assert call.call_id == result.call_id == "call_1"
    assert result.tool_output == "west,12\neast,7"


async def test_export_unknown_session_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/conv_does_not_exist/export")

    assert resp.status_code == 404
