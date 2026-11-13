"""The server persists a web user message under its client-minted stable id.

Persisting a send under its 32-hex ``stable_id`` makes the append idempotent on
retry and lets the client recognize its own send coming back when a network drop
swallowed the POST's acknowledgement. Adoption is limited to the send path:
seeded ``initial_items`` keep store-assigned ids.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import ConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._sessions.helpers import _stable_id_reuse_is_exact_retry
from omnigent.server.routes.sessions import _build_new_item
from omnigent.server.schemas import SessionEventInput
from tests.server.helpers import create_test_agent

_STABLE_ID = "0f" * 16  # 32 lowercase hex chars, the shape web clients mint
_TEXT = "summarize the deploy status"


def _user_message(stable_id: object = _STABLE_ID) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "input_text", "text": _TEXT}],
        "stable_id": stable_id,
    }


def test_build_new_item_adopts_web_send_stable_id_when_asked() -> None:
    """A user message's valid 32-hex ``stable_id`` becomes the item's stable id."""
    body = SessionEventInput(type="message", data=_user_message())

    item = _build_new_item(body, "resp_1", adopt_stable_id=True)

    assert item.stable_id == _STABLE_ID


@pytest.mark.parametrize(
    "data",
    [
        _user_message("abc123"),  # too short
        _user_message("0F" * 16),  # uppercase hex
        _user_message(42),  # not a string
        {
            "role": "assistant",
            "agent": "helper",
            "content": [{"type": "output_text", "text": "hello"}],
            "stable_id": _STABLE_ID,
        },
    ],
)
def test_build_new_item_ignores_unusable_stable_id(data: dict[str, Any]) -> None:
    """Anything but a user message's 32-hex id keeps the store-assigned id."""
    item = _build_new_item(
        SessionEventInput(type="message", data=data), "resp_1", adopt_stable_id=True
    )

    assert item.stable_id is None


def _persisted(text: str = _TEXT, created_by: str | None = "user_a") -> ConversationItem:
    """The item the store hands back when a send's stable id is already persisted."""
    message = _user_message()
    message["content"] = [{"type": "input_text", "text": text}]
    built = _build_new_item(
        SessionEventInput(type="message", data=message),
        "resp_0",
        created_by=created_by,
        adopt_stable_id=True,
    )
    return ConversationItem(
        id=_STABLE_ID,
        type=built.type,
        status="completed",
        response_id=built.response_id,
        created_at=1000,
        data=built.data,
        created_by=created_by,
        deduplicated=True,
    )


def _resend(text: str = _TEXT, created_by: str | None = "user_a") -> NewConversationItem:
    """The item built from a send that reuses the persisted stable id."""
    message = _user_message()
    message["content"] = [{"type": "input_text", "text": text}]
    return _build_new_item(
        SessionEventInput(type="message", data=message),
        "resp_1",
        created_by=created_by,
        adopt_stable_id=True,
    )


def test_stable_id_reuse_by_the_same_author_is_a_retry_only_when_the_body_matches() -> None:
    """An identical resend is the lost-ack retry; an edited one is a new message."""
    assert _stable_id_reuse_is_exact_retry(_persisted(), _resend()) is True
    assert _stable_id_reuse_is_exact_retry(_persisted(), _resend(f"{_TEXT}, but edited")) is False
    # Single-user mode: both sides carry no identity and still compare equal.
    assert _stable_id_reuse_is_exact_retry(_persisted(created_by=None), _resend(created_by=None))


def test_stable_id_reuse_by_another_author_is_refused() -> None:
    """A visible item id must never let someone else's prompt run under that item."""
    with pytest.raises(OmnigentError) as excinfo:
        _stable_id_reuse_is_exact_retry(
            _persisted(created_by="user_a"), _resend(created_by="user_b")
        )
    assert excinfo.value.code == ErrorCode.CONFLICT


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch,
    forwarded: list[dict[str, Any]] | None = None,
    *,
    status: int = 202,
) -> httpx.AsyncClient:
    """Answer every forwarded turn with ``status``, recording each body in ``forwarded``."""

    def accept(request: httpx.Request) -> httpx.Response:
        if forwarded is not None:
            forwarded.append(json.loads(request.content))
        if status >= 400:
            return httpx.Response(status, json={"error": "no process manager"})
        return httpx.Response(status, json={"queued": True})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(accept),
        base_url="http://runner",
    )

    async def get_runner_client(*_: Any, **__: Any) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", get_runner_client)
    return fake_runner


@pytest.mark.asyncio
async def test_web_send_persists_under_its_stable_id_and_dedupes_a_retry(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the store: the persisted item's id IS the stable id, once."""
    forwarded: list[dict[str, Any]] = []
    fake_runner = _stub_runner(monkeypatch, forwarded)
    try:
        agent = await create_test_agent(client)
        create = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert create.status_code == 201, create.text
        session_id = create.json()["id"]
        payload = {"type": "message", "data": _user_message()}

        first = await client.post(f"/v1/sessions/{session_id}/events", json=payload)
        assert first.status_code == 202, first.text
        # A client whose acknowledgement was lost retries with the same stable id.
        retry = await client.post(f"/v1/sessions/{session_id}/events", json=payload)
        assert retry.status_code == 202, retry.text
    finally:
        await fake_runner.aclose()

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert [it["id"] for it in items if it["type"] == "message"] == [_STABLE_ID]
    # The retry is the same send, so it is still dispatched (its first forward
    # may have died) -- against the one persisted item, never a second copy.
    turns = [turn for turn in forwarded if turn.get("type") == "message"]
    assert [turn["persisted_item_id"] for turn in turns] == [_STABLE_ID, _STABLE_ID]


@pytest.mark.asyncio
async def test_runner_rejected_send_stays_persisted_without_delivery_acknowledgement(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused forward keeps the item (persist-before-forward) but acknowledges nothing.

    The web client leans on this split: a persisted item proves delivery only for a
    send whose acknowledgement was lost, never for one the server answered with an
    error -- that draft stays in the composer until a live ``session.input.consumed``.
    """
    consumed: list[str] = []
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._publish_input_consumed",
        lambda _session_id, item, *_args, **_kwargs: consumed.append(item.id),
    )
    fake_runner = _stub_runner(monkeypatch, status=501)
    try:
        agent = await create_test_agent(client)
        create = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert create.status_code == 201, create.text
        session_id = create.json()["id"]
        refused = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "message", "data": _user_message()},
        )
    finally:
        await fake_runner.aclose()

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.RUNNER_UNAVAILABLE
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert [it["id"] for it in items if it["type"] == "message"] == [_STABLE_ID]
    assert consumed == []
    # The snapshot records the refusal against this very item, so a client whose
    # 503 was lost can tell its own refused send from another message's rejection.
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert snapshot["status"] == "failed"
    assert snapshot["last_task_error"]["code"] == "runner_rejected_event"
    assert snapshot["last_task_error"]["item_id"] == _STABLE_ID


@pytest.mark.asyncio
async def test_web_send_of_an_edited_body_under_a_persisted_stable_id_gets_a_fresh_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-adoption web bundle's edited retry keeps working: new item, new id, still run.

    Older bundles retain a failed send's stable id even after the user edits the
    restored text. The edited body must neither be refused (the old client would
    restore and resend it under the same id forever) nor run under the ORIGINAL
    item: it is persisted under a store-assigned id, as before adoption.
    """
    forwarded: list[dict[str, Any]] = []
    fake_runner = _stub_runner(monkeypatch, forwarded)
    edited_text = f"{_TEXT}, but edited"
    try:
        agent = await create_test_agent(client)
        create = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert create.status_code == 201, create.text
        session_id = create.json()["id"]
        events = f"/v1/sessions/{session_id}/events"

        first = await client.post(events, json={"type": "message", "data": _user_message()})
        assert first.status_code == 202, first.text
        edited = _user_message()
        edited["content"] = [{"type": "input_text", "text": edited_text}]
        resend = await client.post(events, json={"type": "message", "data": edited})
        assert resend.status_code == 202, resend.text
    finally:
        await fake_runner.aclose()

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    messages = [it for it in items if it["type"] == "message"]
    assert [m["content"][0]["text"] for m in messages] == [_TEXT, edited_text]
    assert messages[0]["id"] == _STABLE_ID
    assert messages[1]["id"] != _STABLE_ID
    # Each body ran under the item that actually holds it.
    turns = [turn for turn in forwarded if turn.get("type") == "message"]
    assert [(turn["content"][0]["text"], turn["persisted_item_id"]) for turn in turns] == [
        (_TEXT, _STABLE_ID),
        (edited_text, messages[1]["id"]),
    ]


@pytest.mark.asyncio
async def test_seeded_user_message_keeps_a_store_assigned_id(client: httpx.AsyncClient) -> None:
    """``initial_items`` are not a send: a client id there is not adopted."""
    agent = await create_test_agent(client)

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [{"type": "message", "data": _user_message()}],
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    message_ids = [it["id"] for it in items if it["type"] == "message"]
    assert len(message_ids) == 1
    assert message_ids != [_STABLE_ID]
