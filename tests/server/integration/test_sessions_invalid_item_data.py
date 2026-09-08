"""Integration tests for malformed item ``data`` on the session routes.

The route boundary validates an item's *type* against
``_ALLOWED_EVENT_TYPES``, but ``data`` arrives as a free-form dict — so a
caller can name a known type and omit the fields that type requires.
``_build_new_item`` is where that mismatch surfaces, and an escaping
pydantic ``ValidationError`` made it an unhandled 500 instead of a
client error naming the bad field.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import _build_new_item
from omnigent.server.schemas import SessionEventInput
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Route helper: bad data is a client error, not a crash ────────────────────


@pytest.mark.parametrize(
    "item_type,data",
    [
        # The shape seen in production: a known type, no payload at all.
        ("message", {}),
        # Present but incomplete — ``content`` is still required.
        ("message", {"role": "user"}),
        # Right keys, wrong type for ``content``.
        ("message", {"role": "user", "content": "not a list"}),
    ],
)
def test_build_new_item_rejects_invalid_data(item_type: str, data: dict[str, Any]) -> None:
    """Incomplete item ``data`` raises a client error naming the item type.

    ``parse_item_data`` raises pydantic's ``ValidationError``, which is not
    an ``OmnigentError`` and so had no registered handler on the session
    routes — it escaped as an unhandled 500 with a full traceback in the
    server error log.
    """
    body = SessionEventInput(type=item_type, data=data)

    with pytest.raises(OmnigentError) as excinfo:
        _build_new_item(body, "resp_1")

    assert excinfo.value.code == ErrorCode.INVALID_INPUT
    assert item_type in excinfo.value.message


def test_build_new_item_still_accepts_valid_data() -> None:
    """The guard does not disturb a well-formed item."""
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    )

    item = _build_new_item(body, "resp_1")

    assert item.type == "message"
    assert item.response_id == "resp_1"


# ── End to end: the create route answers 4xx, not 500 ────────────────────────


async def test_create_session_with_malformed_initial_item_is_client_error(
    client: httpx.AsyncClient,
) -> None:
    """``POST /v1/sessions`` rejects an unusable ``initial_items`` entry.

    With no runner bound the items are persisted as a history-only seed,
    which is the path that reached ``_build_new_item`` unguarded in
    production.
    """
    agent = await create_test_agent(client)

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [{"type": "message", "data": {}}],
        },
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == ErrorCode.INVALID_INPUT


async def test_create_session_with_valid_initial_item_still_succeeds(
    client: httpx.AsyncClient,
) -> None:
    """A well-formed seed item is unaffected by the guard."""
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
                    },
                }
            ],
        },
    )

    assert resp.status_code == 201, resp.text
