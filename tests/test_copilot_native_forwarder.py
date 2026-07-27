"""Tests for the copilot-native TUI→web transcript forwarder."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.copilot_native_bridge import copilot_home, session_events_path
from omnigent.copilot_native_forwarder import (
    _read_state,
    clear_copilot_bridge_state,
    forward_copilot_events_to_session,
)

_SESSION = "conv_copilot_fwd"
_COPILOT_UUID = "ceb02e26-545a-4e8e-90c5-500266e61376"

# Event shapes captured from a real GitHub Copilot CLI 1.0.63 session
# (``~/.copilot/session-state/<uuid>/events.jsonl``), trimmed to the fields the
# forwarder reads.
_EVENTS: list[dict[str, Any]] = [
    {
        "id": "4a40db16-5bcb-4258-855c-914e12aac352",
        "parentId": None,
        "type": "session.start",
        "data": {"sessionId": _COPILOT_UUID, "copilotVersion": "1.0.63"},
    },
    {
        "id": "e0c0c870-68dd-4599-9c61-dc7e5551c47f",
        "parentId": "f83a5850-6b0f-4462-af33-4b3721029840",
        "type": "user.message",
        "data": {
            "content": "what are you?",
            # The CLI's own scaffolding wrapper; must not reach the chat bubble.
            "transformedContent": (
                "<current_datetime>2026-06-24</current_datetime>\n\nwhat are you?"
            ),
            "attachments": [],
        },
    },
    {
        "id": "8261600b-a282-491d-bb75-6471aeb88a33",
        "parentId": "e0c0c870-68dd-4599-9c61-dc7e5551c47f",
        "type": "assistant.turn_start",
        "data": {"turnId": "0"},
    },
    {
        "id": "b0000000-0000-0000-0000-00000000t001",
        "parentId": "8261600b-a282-491d-bb75-6471aeb88a33",
        "type": "assistant.message",
        # A tool-only step: no prose to mirror, the embedded terminal shows it.
        "data": {"content": "", "toolRequests": [{"name": "bash"}], "turnId": "0"},
    },
    {
        "id": "a1eab9a6-37c7-40cc-8160-3ee583d6119a",
        "parentId": "8261600b-a282-491d-bb75-6471aeb88a33",
        "type": "assistant.message",
        "data": {
            "content": "I am the GitHub Copilot CLI.",
            "model": "claude-haiku-4.5",
            "outputTokens": 196,
            "toolRequests": [],
            "turnId": "0",
        },
    },
    {
        "id": "fc83f68d-0062-490e-a756-da09c49f027f",
        "parentId": "a1eab9a6-37c7-40cc-8160-3ee583d6119a",
        "type": "assistant.turn_end",
        "data": {"turnId": "0"},
    },
]


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    """Append *events* as NDJSON, the way the Copilot CLI does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Records every event POST the forwarder makes."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request body and return 200."""
        self.posts.append(json.loads(request.content))
        return httpx.Response(200, json={})


async def _drain(
    *, bridge_dir: Path, events_file: Path, transport: _CapturingTransport, expected: int
) -> None:
    """Run the forwarder until *expected* total posts have landed, then cancel it."""

    async def _run() -> None:
        await forward_copilot_events_to_session(
            base_url="http://test",
            headers={},
            session_id=_SESSION,
            bridge_dir=bridge_dir,
            agent_name="copilot-native-ui",
            copilot_session_id=_COPILOT_UUID,
            events_file=events_file,
            poll_interval_s=0.01,
        )

    task = asyncio.create_task(_run())
    try:
        for _ in range(500):
            if len(transport.posts) >= expected:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {len(transport.posts)} posts after wait; want {expected}")
    finally:
        task.cancel()
        _ = await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def patched_transport(monkeypatch: pytest.MonkeyPatch) -> _CapturingTransport:
    """Make every AsyncClient the forwarder builds use a capturing transport."""
    transport = _CapturingTransport()
    real_init = httpx.AsyncClient.__init__

    def _init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        kwargs.pop("auth", None)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return transport


@pytest.mark.asyncio
async def test_forwarder_mirrors_user_and_assistant_messages(
    tmp_path: Path, patched_transport: _CapturingTransport
) -> None:
    """Copilot's event stream becomes chat bubbles plus a turn-end idle edge.

    Without this the web conversation shows the user's bubble and an assistant
    turn that never fills in, and no parent orchestrator can observe the turn
    completing.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, _EVENTS)

    # user bubble + assistant bubble + idle edge
    await _drain(
        bridge_dir=bridge_dir,
        events_file=events_file,
        transport=patched_transport,
        expected=3,
    )

    posts = patched_transport.posts
    assert [post["type"] for post in posts] == [
        "external_conversation_item",
        "external_conversation_item",
        "external_session_status",
    ]
    user_item = posts[0]["data"]["item_data"]
    assert user_item["role"] == "user"
    assert user_item["content"][0]["text"] == "what are you?"
    assistant_item = posts[1]["data"]["item_data"]
    assert assistant_item["role"] == "assistant"
    assert assistant_item["agent"] == "copilot-native-ui"
    assert assistant_item["content"][0]["text"] == "I am the GitHub Copilot CLI."
    # The idle edge closes out the assistant bubble it belongs to, which is what
    # drives the web streaming lifecycle and wakes a parent's inbox.
    assert posts[2]["data"]["status"] == "idle"
    assert posts[2]["data"]["response_id"] == posts[1]["data"]["response_id"]


@pytest.mark.asyncio
async def test_forwarder_resumes_from_persisted_offset_without_reposting(
    tmp_path: Path, patched_transport: _CapturingTransport
) -> None:
    """A restart resumes at the persisted byte offset instead of replaying."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, _EVENTS)

    await _drain(
        bridge_dir=bridge_dir,
        events_file=events_file,
        transport=patched_transport,
        expected=3,
    )
    assert _read_state(bridge_dir).offset == events_file.stat().st_size

    # A fresh forwarder over the same bridge dir: it must pick up only the newly
    # appended event, not replay the three it already posted.
    _write_events(
        events_file,
        [
            {
                "id": "cc000000-0000-0000-0000-0000000000c2",
                "type": "assistant.message",
                "data": {"content": "second reply", "turnId": "1"},
            }
        ],
    )
    await _drain(
        bridge_dir=bridge_dir,
        events_file=events_file,
        transport=patched_transport,
        expected=4,
    )
    assert len(patched_transport.posts) == 4
    assert patched_transport.posts[3]["data"]["item_data"]["content"][0]["text"] == (
        "second reply"
    )


@pytest.mark.asyncio
async def test_clear_bridge_state_drops_the_cursor(tmp_path: Path) -> None:
    """Re-creating a terminal wipes the prior launch's forward cursor."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "copilot_forwarder.json").write_text(
        json.dumps({"offset": 42, "seen_uuids": ["x"]}), encoding="utf-8"
    )
    assert _read_state(bridge_dir).offset == 42
    clear_copilot_bridge_state(bridge_dir)
    assert _read_state(bridge_dir).offset == 0


def test_session_events_path_follows_copilot_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tailed path tracks ``COPILOT_HOME``, which the pane inherits."""
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    assert session_events_path(_COPILOT_UUID) == (
        Path.home() / ".copilot" / "session-state" / _COPILOT_UUID / "events.jsonl"
    )
    monkeypatch.setenv("COPILOT_HOME", "/custom/copilot")
    assert copilot_home() == Path("/custom/copilot")
    assert session_events_path(_COPILOT_UUID) == (
        Path("/custom/copilot") / "session-state" / _COPILOT_UUID / "events.jsonl"
    )
