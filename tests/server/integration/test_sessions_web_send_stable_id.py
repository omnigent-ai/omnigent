"""The server persists a web user message under its client-minted stable id.

A web client stamps each send POST with a 32-hex ``stable_id``. Persisting
the message under that id makes the append idempotent on retry AND lets the
client recognize its own send coming back over the stream: when a network
drop (backgrounding, a VPN blip) swallows the POST's acknowledgement, the
committed item arriving under the send's stable id is the proof the message
was delivered — without it the client treats the send as failed and restores
the already-sent prompt into the composer.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.routes.sessions import _build_new_item
from omnigent.server.schemas import SessionEventInput
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_STABLE_ID = "0f" * 16  # 32 lowercase hex chars, the shape web clients mint


def test_build_new_item_adopts_web_send_stable_id() -> None:
    """A user message's valid 32-hex ``stable_id`` becomes the item's stable id."""
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
            "stable_id": _STABLE_ID,
        },
    )

    item = _build_new_item(body, "resp_1")

    assert item.stable_id == _STABLE_ID


@pytest.mark.parametrize(
    "data",
    [
        # Wrong shape: too short.
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}], "stable_id": "abc123"},
        # Wrong shape: uppercase hex.
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
            "stable_id": "0F" * 16,
        },
        # Wrong type entirely.
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}], "stable_id": 42},
        # Not a user message.
        {
            "role": "assistant",
            "agent": "helper",
            "content": [{"type": "output_text", "text": "hello"}],
            "stable_id": _STABLE_ID,
        },
    ],
)
def test_build_new_item_ignores_unusable_stable_id(data: dict) -> None:
    """Anything but a user message's 32-hex id keeps the store-assigned id."""
    item = _build_new_item(SessionEventInput(type="message", data=data), "resp_1")

    assert item.stable_id is None


async def test_seeded_user_message_persists_under_its_stable_id(
    client: httpx.AsyncClient,
) -> None:
    """End to end through the store: the persisted item's id IS the stable id."""
    agent = await create_test_agent(client)

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "kick off"}],
                        "stable_id": _STABLE_ID,
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    message_ids = [it["id"] for it in items if it["type"] == "message"]
    assert message_ids == [_STABLE_ID]
