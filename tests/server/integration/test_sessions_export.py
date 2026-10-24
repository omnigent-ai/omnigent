"""Integration tests for ``GET /v1/sessions/{id}/export``.

The route streams a session as an ``omnigent.transcript/1`` file. Items are
seeded through ``POST /v1/sessions`` ``initial_items`` so the test exercises
the same persisted rows the CLI and web export read.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
import pytest

from omnigent.export import (
    CONTENT_FIELDS,
    TRANSCRIPT_SCHEMA,
    TranscriptSchemaError,
    item_from_entry,
    read_transcript,
    read_transcripts,
)
from omnigent.server.routes.sessions.routes_items import _EXPORT_PAGE_SIZE
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
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="{session_id}.jsonl"'), disposition

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


async def test_export_entries_rebuild_the_stored_items(client: httpx.AsyncClient) -> None:
    """Every entry carries ``raw``, so the file reproduces what the server stored."""
    session_id = await _seed_session(client)

    resp = await client.get(f"/v1/sessions/{session_id}/export")
    transcript = read_transcript(resp.text.splitlines(keepends=True))

    rebuilt = [item_from_entry(entry) for entry in transcript.entries]
    assert [item["type"] for item in rebuilt] == [item["type"] for item in _INITIAL_ITEMS]
    for seeded, item in zip(_INITIAL_ITEMS, rebuilt, strict=True):
        # Everything seeded comes back; so do fields the server stamped on
        # top of it, which is what the readable entry alone would have lost.
        assert seeded["data"].items() <= item["data"].items(), (seeded, item)
    assert rebuilt[0]["data"]["user_authored"] is True


async def test_export_numbers_across_item_pages(client: httpx.AsyncClient) -> None:
    """``seq`` and ``turn`` keep counting past the route's page size."""
    agent = await create_test_agent(client, name="export-paging-agent")
    count = _EXPORT_PAGE_SIZE + 7
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "external",
            "initial_items": [
                {
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": f"m{i}"}]},
                }
                for i in range(count)
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/export")

    transcript = read_transcript(resp.text.splitlines(keepends=True))
    assert [e.seq for e in transcript.entries] == list(range(1, count + 1))
    # Each user message is a request of its own, so it opens a turn.
    assert [e.turn for e in transcript.entries] == list(range(1, count + 1))
    assert [e.text for e in transcript.entries][-1] == f"m{count - 1}"


async def test_export_without_content_keeps_the_shape_and_drops_the_rest(
    client: httpx.AsyncClient,
) -> None:
    """``content=none`` is for sharing what a run did when what it said cannot leave."""
    session_id = await _seed_session(client)

    resp = await client.get(f"/v1/sessions/{session_id}/export", params={"content": "none"})

    assert resp.status_code == 200, resp.text
    transcript = read_transcript(resp.text.splitlines(keepends=True))

    assert transcript.header.redacted is True
    # The shape a reader came for: turns, kinds, the tools called and how they paired.
    assert [e.kind for e in transcript.entries] == [
        "message",
        "reasoning",
        "tool_call",
        "tool_result",
        "message",
    ]
    assert [e.seq for e in transcript.entries] == [1, 2, 3, 4, 5]
    call, result = transcript.entries[2], transcript.entries[3]
    assert call.tool == "sql"
    assert call.call_id == result.call_id == "call_1"
    assert transcript.entries[1].sealed is True
    # None of what was said, asked or returned.
    assert all(
        getattr(entry, field) is None for entry in transcript.entries for field in CONTENT_FIELDS
    )
    assert "churn by region" not in resp.text
    assert "select region" not in resp.text


async def test_export_defaults_to_the_full_session(client: httpx.AsyncClient) -> None:
    """The new parameter is opt-in; omitting it writes what it always wrote."""
    session_id = await _seed_session(client)

    resp = await client.get(f"/v1/sessions/{session_id}/export")

    assert read_transcript(resp.text.splitlines(keepends=True)).header.redacted is False
    assert "churn by region" in resp.text


async def test_export_rejects_an_unknown_content_mode(client: httpx.AsyncClient) -> None:
    """A typo must not quietly hand back the full session."""
    session_id = await _seed_session(client)

    resp = await client.get(f"/v1/sessions/{session_id}/export", params={"content": "redacted"})

    assert resp.status_code == 422, resp.text


async def test_export_can_be_gzipped(client: httpx.AsyncClient) -> None:
    """Large sessions are mostly text; the compressed download is the same file."""
    session_id = await _seed_session(client)

    plain = await client.get(f"/v1/sessions/{session_id}/export")
    resp = await client.get(f"/v1/sessions/{session_id}/export", params={"compress": "gzip"})

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/gzip")
    assert f'filename="{session_id}.jsonl.gz"' in resp.headers["content-disposition"]
    # Sent as a file rather than a transfer encoding, so what lands on disk is the
    # archive and not something a browser quietly expanded.
    assert "content-encoding" not in resp.headers
    assert resp.content[:2] == b"\x1f\x8b"

    # Same transcript; only the header's own export timestamp differs between calls.
    unzipped = read_transcript(gzip.decompress(resp.content).decode().splitlines(keepends=True))
    direct = read_transcript(plain.text.splitlines(keepends=True))
    assert unzipped.entries == direct.entries
    assert unzipped.header.model_dump(exclude={"exported"}) == direct.header.model_dump(
        exclude={"exported"}
    )


async def test_gzipped_export_can_still_drop_content(client: httpx.AsyncClient) -> None:
    """The two options are independent."""
    session_id = await _seed_session(client)

    resp = await client.get(
        f"/v1/sessions/{session_id}/export", params={"compress": "gzip", "content": "none"}
    )

    text = gzip.decompress(resp.content).decode()
    assert read_transcript(text.splitlines(keepends=True)).header.redacted is True
    assert "churn by region" not in text


async def _seed_child(client: httpx.AsyncClient, parent_id: str, title: str) -> str:
    """Create a sub-agent session under *parent_id*."""
    agent = await create_test_agent(client, name=f"child-agent-{title.replace(' ', '-')}")
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "title": title,
            "host_type": "external",
            "parent_session_id": parent_id,
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"work for {title}"}],
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_export_tree_bundles_the_sub_agent_sessions(client: httpx.AsyncClient) -> None:
    """A multi-agent run is only self-contained when its sub-sessions travel with it."""
    parent_id = await _seed_session(client)
    child_id = await _seed_child(client, parent_id, "research")
    grandchild_id = await _seed_child(client, child_id, "deep research")

    resp = await client.get(f"/v1/sessions/{parent_id}/export", params={"include": "tree"})

    assert resp.status_code == 200, resp.text
    sessions = read_transcripts(resp.text.splitlines(keepends=True))

    # Root first, then its descendants; a sub-agent's own sub-agents come too.
    assert [t.header.session for t in sessions] == [parent_id, child_id, grandchild_id]
    assert [t.header.parent_session for t in sessions] == [None, parent_id, child_id]
    assert sessions[1].entries[0].text == "work for research"
    # Each session is numbered from its own first turn.
    assert all(t.entries[0].seq == 1 for t in sessions)


async def test_export_defaults_to_this_session_alone(client: httpx.AsyncClient) -> None:
    """The bundle is opt-in: the default download is one session, as before."""
    parent_id = await _seed_session(client)
    await _seed_child(client, parent_id, "research")

    resp = await client.get(f"/v1/sessions/{parent_id}/export")

    sessions = read_transcripts(resp.text.splitlines(keepends=True))
    assert [t.header.session for t in sessions] == [parent_id]
    # The single-session reader still accepts it.
    assert read_transcript(resp.text.splitlines(keepends=True)).header.session == parent_id


async def test_reading_a_bundle_as_one_session_says_what_it_is(
    client: httpx.AsyncClient,
) -> None:
    """A caller that wanted one session gets told, not a silently truncated read."""
    parent_id = await _seed_session(client)
    await _seed_child(client, parent_id, "research")

    resp = await client.get(f"/v1/sessions/{parent_id}/export", params={"include": "tree"})

    with pytest.raises(TranscriptSchemaError, match="2 sessions"):
        read_transcript(resp.text.splitlines(keepends=True))


async def test_export_tree_can_be_redacted_and_gzipped(client: httpx.AsyncClient) -> None:
    """The three options compose."""
    parent_id = await _seed_session(client)
    child_id = await _seed_child(client, parent_id, "research")

    resp = await client.get(
        f"/v1/sessions/{parent_id}/export",
        params={"include": "tree", "content": "none", "compress": "gzip"},
    )

    text = gzip.decompress(resp.content).decode()
    sessions = read_transcripts(text.splitlines(keepends=True))
    assert [t.header.session for t in sessions] == [parent_id, child_id]
    assert all(t.header.redacted for t in sessions)
    assert "work for research" not in text


async def test_export_unknown_session_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/conv_does_not_exist/export")

    assert resp.status_code == 404
