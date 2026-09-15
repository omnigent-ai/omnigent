"""SDK-harness message boundaries in the streamed Slack reply.

An in-process/scaffold harness (claude-sdk) streams every assistant message of
a turn as id-less ``response.output_text.delta`` events — unlike native
terminal harnesses, whose deltas carry a per-message ``message_id`` and already
get a paragraph break on id change. For the id-less shape the server publishes
the harness-agnostic boundary signal instead: after buffered narration is
persisted at a text→tool boundary (and at turn end), the committed segment is
published as ``response.output_item.done`` with the assistant ``message`` item.
The web UI and the TUI consume that event per segment and render the segments
separately; the Slack reply must not run them together into one paragraph
("…drafted the spec.Now waiting…").

The SSE body below encodes the wire shape captured from a real claude-sdk turn
against a live server (narration → Glob tool call → narration): an id-less
``running`` edge, id-less deltas, the committed ``message`` item published
between the two delta groups, the tool-call items, and the id-less ``idle``
that ends an in-process turn.

Vertical drive, like the rest of this suite: the REAL service and the REAL
``OmnigentClient`` (real httpx) run a full @-mention turn against
:class:`FakeOmnigentServer`, and the assertions read exactly what a Slack user
would see via :class:`RecordingSlackClient`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import respx
from fakes import FakeOmnigentServer, RecordingSlackClient, sse_delta, sse_status
from omnigent_slack.models import UserConfig
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.store import SQLiteStore

_SERVER = "http://omnigent.test"

# Two narration segments from consecutive assistant messages of ONE turn. The
# first ends and the second starts with sentence text, so a missing separator
# produces the telltale mid-punctuation run-on ("…the spec.Now waiting…").
_SEG_A = "Dispatched the sub-agents and drafted the spec."
_SEG_B = "Now waiting on the cross-vendor technical review of the spec."


def _sse_item_done(item: dict[str, Any]) -> str:
    """One ``response.output_item.done`` SSE frame carrying *item*."""
    return f"data: {json.dumps({'type': 'response.output_item.done', 'item': item})}\n\n"


def _assistant_message_item(text: str) -> dict[str, Any]:
    """The committed assistant message the relay publishes after each flush."""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


# The captured claude-sdk wire shape: id-less running edge; id-less deltas; the
# committed message item published between the delta groups (the boundary
# signal); the tool-call items; a second committed message; then completion and
# the id-less idle that ends an in-process turn.
_SDK_TURN_SSE_BODY = (
    sse_status("running")
    + sse_delta(_SEG_A)
    + _sse_item_done(_assistant_message_item(_SEG_A))
    + _sse_item_done(
        {
            "type": "function_call",
            "name": "Glob",
            "call_id": "call_1",
            "arguments": json.dumps({"pattern": "*.md"}),
            "status": "completed",
        }
    )
    + _sse_item_done({"type": "function_call_output", "call_id": "call_1", "output": ""})
    + sse_delta(_SEG_B)
    + _sse_item_done(_assistant_message_item(_SEG_B))
    + 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    + sse_status("idle")
)


class _NoopSetup:
    """SetupFlow stand-in for turns where the user is already configured."""

    async def prompt_unconfigured(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("configured user should not be prompted to set up")

    async def prompt_relogin(self, *args: object, **kwargs: object) -> bool:
        return True


async def _wait_for_turns(service: SlackOmnigentService, timeout: float = 10.0) -> None:
    """Await the spawned turn tasks (event-driven; timeout only bounds a hang)."""
    tasks = list(service._turn_tasks)
    if not tasks:
        return
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


@respx.mock
async def test_sdk_harness_turn_separates_back_to_back_messages(tmp_path: Path) -> None:
    """Back-to-back assistant messages of an id-less (SDK-harness) turn must be
    separated in the Slack reply, not concatenated into one run-on paragraph."""
    server = FakeOmnigentServer(_SERVER)
    server.harness = "claude-sdk"
    server.sse_body = _SDK_TURN_SSE_BODY
    server.install(respx.mock)

    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    await store.upsert_user_config(
        "T1",
        "U1",
        UserConfig(
            agent_id="ag_1",
            agent_name="debby",
            workspace="/home/bot/work",
            host_id="h1",
        ),
    )
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> run the task"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # What the Slack user reads: every streamed message this turn delivered,
    # in order. Separate Slack messages are themselves a visual break, so a
    # fix that seals into a new message also passes.
    assert client.streams, "turn delivered no streamed reply"
    delivered = "\n\n".join(stream.text for stream in client.streams)

    # Both narration segments reached the user, in order.
    idx_a = delivered.find(_SEG_A)
    idx_b = delivered.find(_SEG_B)
    assert idx_a != -1, f"first message never reached Slack: {delivered!r}"
    assert idx_b != -1, f"second message never reached Slack: {delivered!r}"
    assert idx_a < idx_b, f"messages out of order: {delivered!r}"

    # THE regression: with no boundary for id-less streams the two messages
    # butt against each other mid-punctuation ("…the spec.Now waiting…").
    assert _SEG_A + _SEG_B not in delivered, (
        f"distinct assistant messages ran together with no separator: {delivered!r}"
    )
    # And the separation is a real line break (the same paragraph break a
    # message_id change already produces for native harnesses).
    between = delivered[idx_a + len(_SEG_A) : idx_b]
    assert "\n" in between, (
        "expected a paragraph break between back-to-back assistant messages, "
        f"got {between!r} in {delivered!r}"
    )
