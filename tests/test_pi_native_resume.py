"""Tests for pi-native fork/resume session rebuild.

Covers the Omnigent-items -> Pi session JSONL converter, the safe-id guard,
the resume-file path resolution, and the end-to-end
``ensure_local_pi_resume_session`` (mocked Omnigent items endpoint).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.pi_native_resume import (
    ensure_local_pi_resume_session,
    fetch_all_session_items_for_pi_resume,
    is_safe_pi_session_id,
    mint_pi_session_id,
    pi_resume_session_path,
    pi_session_records_from_session_items,
    write_pi_session_records,
)

_EXTERNAL_ID = "019efdb8-54c8-7c02-be27-875eb2620635"


def _user_item(text: str, *, item_id: str = "u1", response_id: str = "r1") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "user",
        "response_id": response_id,
        "content": [{"type": "input_text", "text": text}],
    }


def _assistant_item(
    text: str, *, item_id: str = "a1", response_id: str = "r1", model: str = "claude-opus-4-8"
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "response_id": response_id,
        "model": model,
        "content": [{"type": "output_text", "text": text}],
    }


def _function_call_item(
    *, name: str, call_id: str, arguments: str, item_id: str = "fc1", response_id: str = "r1"
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "response_id": response_id,
        "name": name,
        "call_id": call_id,
        "arguments": arguments,
    }


def _function_output_item(
    *, call_id: str, output: str, item_id: str = "fo1", response_id: str = "r1"
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call_output",
        "response_id": response_id,
        "call_id": call_id,
        "output": output,
    }


# --------------------------------------------------------------------------
# safe-id guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("019efdb8-54c8-7c02-be27-875eb2620635", True),
        ("02857840-6362-408f-b41f-309e396ed7c6", True),
        ("not-a-uuid", False),
        ("../../etc/passwd", False),
        ("019efdb8-54c8-7c02-be27-875eb2620635; rm -rf /", False),
        ("", False),
    ],
)
def test_is_safe_pi_session_id(value: str, expected: bool) -> None:
    assert is_safe_pi_session_id(value) is expected


def test_mint_pi_session_id_is_safe() -> None:
    assert is_safe_pi_session_id(mint_pi_session_id())


# --------------------------------------------------------------------------
# converter
# --------------------------------------------------------------------------


def test_records_header_first_and_well_formed() -> None:
    records = pi_session_records_from_session_items(
        [_user_item("hello"), _assistant_item("hi there")],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    header = records[0]
    assert header["type"] == "session"
    assert header["version"] == 3
    assert header["id"] == _EXTERNAL_ID
    assert header["cwd"] == "/repo"
    # No parentId on the header (metadata, not in the tree).
    assert "parentId" not in header


def test_records_build_parent_chain() -> None:
    records = pi_session_records_from_session_items(
        [_user_item("q"), _assistant_item("a")],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    entries = records[1:]
    assert len(entries) == 2
    assert entries[0]["parentId"] is None
    assert entries[1]["parentId"] == entries[0]["id"]
    assert entries[0]["message"]["role"] == "user"
    assert entries[1]["message"]["role"] == "assistant"


def test_user_message_content_blocks() -> None:
    records = pi_session_records_from_session_items(
        [_user_item("remember BANANA42")],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    msg = records[1]["message"]
    assert msg["role"] == "user"
    assert msg["content"] == [{"type": "text", "text": "remember BANANA42"}]


def test_assistant_message_has_required_metadata() -> None:
    records = pi_session_records_from_session_items(
        [_assistant_item("ok", model="some-model")],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
        provider="omnigent",
    )
    msg = records[1]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == [{"type": "text", "text": "ok"}]
    assert msg["provider"] == "omnigent"
    # per-item model wins over the default
    assert msg["model"] == "some-model"
    assert msg["stopReason"] == "stop"
    assert msg["usage"]["totalTokens"] == 0
    assert msg["usage"]["cost"]["total"] == 0


def test_function_call_becomes_assistant_toolcall() -> None:
    records = pi_session_records_from_session_items(
        [_function_call_item(name="bash", call_id="call_1", arguments='{"cmd": "ls"}')],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    msg = records[1]["message"]
    assert msg["role"] == "assistant"
    block = msg["content"][0]
    assert block["type"] == "toolCall"
    assert block["id"] == "call_1"
    assert block["name"] == "bash"
    assert block["arguments"] == {"cmd": "ls"}


def test_function_output_becomes_toolresult() -> None:
    records = pi_session_records_from_session_items(
        [_function_output_item(call_id="call_1", output="file listing")],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    msg = records[1]["message"]
    assert msg["role"] == "toolResult"
    assert msg["toolCallId"] == "call_1"
    assert msg["content"] == [{"type": "text", "text": "file listing"}]
    assert msg["isError"] is False


def test_full_tool_roundtrip_chains_correctly() -> None:
    items = [
        _user_item("run ls", item_id="u1"),
        _function_call_item(name="bash", call_id="c1", arguments='{"cmd":"ls"}', item_id="fc1"),
        _function_output_item(call_id="c1", output="a.txt", item_id="fo1"),
        _assistant_item("Here is the listing.", item_id="a1"),
    ]
    records = pi_session_records_from_session_items(
        items,
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    entries = records[1:]
    assert [e["message"]["role"] for e in entries] == [
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    # Parent chain is strictly linear.
    assert entries[0]["parentId"] is None
    for prev, cur in itertools.pairwise(entries):
        assert cur["parentId"] == prev["id"]


def test_empty_text_items_are_dropped() -> None:
    items = [
        {"id": "u1", "type": "message", "role": "user", "content": []},
        {"id": "a1", "type": "message", "role": "assistant", "content": []},
        _user_item("real text", item_id="u2"),
    ]
    records = pi_session_records_from_session_items(
        items,
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    # header + only the non-empty user message
    assert len(records) == 2
    assert records[1]["message"]["content"][0]["text"] == "real text"


def test_interrupted_response_group_is_skipped() -> None:
    items = [
        _user_item("first done", item_id="u1", response_id="r1"),
        _assistant_item("first reply", item_id="a1", response_id="r1"),
        _user_item("cancelled question", item_id="u2", response_id="r2"),
        {
            "id": "a2",
            "type": "message",
            "role": "assistant",
            "response_id": "r2",
            "interrupted": True,
            "content": [{"type": "output_text", "text": "partial..."}],
        },
    ]
    records = pi_session_records_from_session_items(
        items,
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    texts = [
        b["text"]
        for r in records[1:]
        for b in r["message"].get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    # The whole r2 group (user + partial assistant) is excluded.
    assert "partial..." not in texts
    assert "cancelled question" not in texts
    assert "first reply" in texts


def test_deterministic_entry_ids() -> None:
    items = [_user_item("q"), _assistant_item("a")]
    first = pi_session_records_from_session_items(
        items, session_id="conv_abc", external_session_id=_EXTERNAL_ID, cwd=Path("/repo")
    )
    second = pi_session_records_from_session_items(
        items, session_id="conv_abc", external_session_id=_EXTERNAL_ID, cwd=Path("/repo")
    )
    assert [r["id"] for r in first[1:]] == [r["id"] for r in second[1:]]


# --------------------------------------------------------------------------
# path resolution + writing
# --------------------------------------------------------------------------


def test_resume_session_path_names_with_uuid(tmp_path: Path) -> None:
    path = pi_resume_session_path(tmp_path, _EXTERNAL_ID)
    assert path.parent == tmp_path
    assert path.name.endswith(f"_{_EXTERNAL_ID}.jsonl")


def test_resume_session_path_reuses_existing(tmp_path: Path) -> None:
    existing = tmp_path / f"2026-06-25T08-00-00-000Z_{_EXTERNAL_ID}.jsonl"
    existing.write_text("{}\n", encoding="utf-8")
    assert pi_resume_session_path(tmp_path, _EXTERNAL_ID) == existing


def test_write_pi_session_records_is_valid_jsonl(tmp_path: Path) -> None:
    records = pi_session_records_from_session_items(
        [_user_item("hi"), _assistant_item("yo")],
        session_id="conv_abc",
        external_session_id=_EXTERNAL_ID,
        cwd=Path("/repo"),
    )
    target = tmp_path / f"stamp_{_EXTERNAL_ID}.jsonl"
    write_pi_session_records(target, records)
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == len(records)
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["type"] == "session"
    # no temp file left behind
    assert not list(tmp_path.glob("*.tmp"))


# --------------------------------------------------------------------------
# end-to-end ensure_local_pi_resume_session (mocked items endpoint)
# --------------------------------------------------------------------------


def _items_handler(items: list[dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/items")
        return httpx.Response(200, json={"data": items, "has_more": False})

    return handler


@pytest.mark.asyncio
async def test_ensure_local_pi_resume_session_writes_history(tmp_path: Path) -> None:
    items = [_user_item("remember BANANA42"), _assistant_item("ok BANANA42")]
    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(_items_handler(items))
    ) as client:
        path = await ensure_local_pi_resume_session(
            client,
            session_id="conv_abc",
            external_session_id=_EXTERNAL_ID,
            session_dir=tmp_path,
            workspace=Path("/repo"),
            model="claude-opus-4-8",
        )
    assert path is not None
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["id"] == _EXTERNAL_ID
    texts = [
        b["text"]
        for r in parsed[1:]
        for b in r["message"].get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert "remember BANANA42" in texts
    assert "ok BANANA42" in texts


@pytest.mark.asyncio
async def test_ensure_local_pi_resume_session_empty_returns_none(tmp_path: Path) -> None:
    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(_items_handler([]))
    ) as client:
        path = await ensure_local_pi_resume_session(
            client,
            session_id="conv_abc",
            external_session_id=_EXTERNAL_ID,
            session_dir=tmp_path,
            workspace=Path("/repo"),
        )
    assert path is None
    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.asyncio
async def test_ensure_local_pi_resume_session_unsafe_id_returns_none(tmp_path: Path) -> None:
    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(_items_handler([_user_item("x")]))
    ) as client:
        path = await ensure_local_pi_resume_session(
            client,
            session_id="conv_abc",
            external_session_id="../evil",
            session_dir=tmp_path,
            workspace=Path("/repo"),
        )
    assert path is None


@pytest.mark.asyncio
async def test_ensure_local_pi_resume_session_reuses_existing(tmp_path: Path) -> None:
    existing = tmp_path / f"2026-06-25T08-00-00-000Z_{_EXTERNAL_ID}.jsonl"
    existing.write_text('{"type":"session"}\n', encoding="utf-8")

    # The handler would raise if called; reuse must short-circuit the fetch.
    def boom(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("items endpoint must not be hit when a local file exists")

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(boom)
    ) as client:
        path = await ensure_local_pi_resume_session(
            client,
            session_id="conv_abc",
            external_session_id=_EXTERNAL_ID,
            session_dir=tmp_path,
            workspace=Path("/repo"),
        )
    assert path == existing
    assert existing.read_text(encoding="utf-8") == '{"type":"session"}\n'


@pytest.mark.asyncio
async def test_fetch_paginates(tmp_path: Path) -> None:
    pages = [
        {"data": [_user_item("p1", item_id="u1")], "has_more": True, "last_id": "u1"},
        {"data": [_assistant_item("p2", item_id="a1")], "has_more": False},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, json=page)

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handler)
    ) as client:
        items = await fetch_all_session_items_for_pi_resume(client, "conv_abc")
    assert calls["n"] == 2
    assert [i["id"] for i in items] == ["u1", "a1"]
