"""A native mirror names the web send it delivered only when the pending-input link is exact."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.datastructures import Headers

from omnigent.entities import ConversationItem, MessageData, NewConversationItem, parse_item_data
from omnigent.runtime import pending_inputs
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.helpers import _extract_persistent_item_from_sse
from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
from omnigent.server.routes.imports import ImportItemInput
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

_WEB_ID = "a" * 32
_OTHER_WEB_ID = "b" * 32
_FORGED_ID = "c" * 32


def _mirror(
    text: str, *, source_id: str | None = "codex-record-1", **item_data: Any
) -> SessionEventInput:
    """A forwarder's external item for a mirrored user message."""
    return SessionEventInput.model_validate(
        {
            "type": "external_conversation_item",
            "data": {
                **({"source_id": source_id} if source_id is not None else {}),
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                    **item_data,
                },
                "response_id": "resp_mirror",
            },
        }
    )


async def _persist_mirrors(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[str, str | None]],
    mirrors: list[SessionEventInput],
) -> tuple[dict[str, tuple[ConversationItem, MessageData]], list[dict[str, Any]]]:
    """Queue web sends, persist each mirror, and return the user items by text and receipts."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    for text, stable_id in queued:
        pending_inputs.record(conv.id, [{"type": "input_text", "text": text}], stable_id=stable_id)
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    for mirror in mirrors:
        await _persist_external_conversation_item(conv.id, conv, mirror, store, created_by=None)
    users = {
        item.data.content[0]["text"]: (item, item.data)
        for item in SqlAlchemyConversationStore(db_uri).list_items(conv.id, type="message").data
        if isinstance(item.data, MessageData) and item.data.role == "user"
    }
    receipts = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    return users, receipts


@pytest.mark.asyncio
async def test_text_matched_mirror_names_its_web_send(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    users, receipts = await _persist_mirrors(
        db_uri,
        monkeypatch,
        [("deploy the fix", _OTHER_WEB_ID), ("ship it", _WEB_ID)],
        [_mirror("ship it")],
    )

    item, data = users["ship it"]
    assert item.id != _WEB_ID
    assert data.web_stable_id == _WEB_ID
    assert item.to_api_dict()["web_stable_id"] == _WEB_ID
    [receipt] = [receipt for receipt in receipts if receipt["item_id"] == item.id]
    assert receipt["data"]["web_stable_id"] == _WEB_ID


@pytest.mark.parametrize(
    "queued,mirror_text,source_id",
    [
        ([("ship it", _WEB_ID)], "something else", "codex-record-1"),
        ([], "ship it", "codex-record-1"),
        ([("ship it", _WEB_ID), ("ship it", _OTHER_WEB_ID)], "ship it", "codex-record-1"),
        ([("ship it", None)], "ship it", "codex-record-1"),
        ([("ship it", _WEB_ID)], "ship it", None),
    ],
    ids=[
        "fifo-fallback",
        "typed-in-terminal",
        "shared-text",
        "send-without-stable-id",
        "committed-under-web-id",
    ],
)
@pytest.mark.asyncio
async def test_mirror_without_an_exact_link_names_no_web_send(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[str, str | None]],
    mirror_text: str,
    source_id: str | None,
) -> None:
    users, receipts = await _persist_mirrors(
        db_uri, monkeypatch, queued, [_mirror(mirror_text, source_id=source_id)]
    )

    item, data = users[mirror_text]
    assert "web_stable_id" not in data.model_dump()
    assert "web_stable_id" not in item.to_api_dict()
    assert "web_stable_id" not in receipts[-1]["data"]


@pytest.mark.asyncio
async def test_mirror_of_a_send_a_positional_drain_may_have_settled_names_no_web_send(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unmatched first mirror drains by position and marks the rest uncertain.
    users, receipts = await _persist_mirrors(
        db_uri,
        monkeypatch,
        [("first", _OTHER_WEB_ID), ("ship it", _WEB_ID)],
        [
            _mirror("reworded first", source_id="codex-record-1"),
            _mirror("ship it", source_id="codex-record-2"),
        ],
    )

    _, data = users["ship it"]
    assert "web_stable_id" not in data.model_dump()
    assert all("web_stable_id" not in receipt["data"] for receipt in receipts)


@pytest.mark.parametrize(
    "queued,expected",
    [([], None), ([("ship it", _WEB_ID)], _WEB_ID)],
    ids=["typed-in-terminal", "text-matched"],
)
@pytest.mark.asyncio
async def test_forwarder_cannot_set_web_stable_id(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[str, str | None]],
    expected: str | None,
) -> None:
    users, _ = await _persist_mirrors(
        db_uri, monkeypatch, queued, [_mirror("ship it", web_stable_id=_FORGED_ID)]
    )

    _, data = users["ship it"]
    assert data.web_stable_id == expected


def test_imported_item_cannot_set_web_stable_id() -> None:
    imported = ImportItemInput(
        type="message",
        response_id="resp_import",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
            "web_stable_id": _FORGED_ID,
        },
    ).to_item()

    assert isinstance(imported.data, MessageData)
    assert imported.data.web_stable_id is None


def test_web_stable_id_is_absent_from_the_wire_until_set() -> None:
    data = parse_item_data("message", {"role": "user", "content": []})

    assert isinstance(data, MessageData)
    assert "web_stable_id" not in data.model_dump()
    assert (
        data.model_copy(update={"web_stable_id": _WEB_ID}).model_dump()["web_stable_id"] == _WEB_ID
    )


@pytest.mark.parametrize(
    "first_text,drains_first",
    [("ship it", False), ("[Attached: /tmp/notes.md]\n\nship it", True)],
    ids=["generated-marker-tier", "literal-marker-exact-tier"],
)
@pytest.mark.asyncio
async def test_mirror_two_sends_could_produce_names_no_web_send(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, first_text: str, drains_first: bool
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    # Either entry can produce this mirror: one by a literal marker or bare text, one
    # by the marker generated for its attachment.
    first = pending_inputs.record(
        conv.id, [{"type": "input_text", "text": first_text}], stable_id=_WEB_ID
    )
    second = pending_inputs.record(
        conv.id,
        [
            {"type": "input_file", "file_id": "file_x", "filename": "notes.md"},
            {"type": "input_text", "text": "ship it"},
        ],
        stable_id=_OTHER_WEB_ID,
    )
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    item_id = await _persist_external_conversation_item(
        conv.id, conv, _mirror("[Attached: /tmp/notes.md]\n\nship it"), store, created_by=None
    )

    item = store.get_item(conv.id, item_id)
    assert item is not None and isinstance(item.data, MessageData)
    assert item.data.web_stable_id is None
    # Which entry drains is unchanged; only the link is withheld.
    [receipt] = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
        and call.args[1]["data"]["item_id"] == item_id
    ]
    assert receipt["cleared_pending_id"] == (first if drains_first else second)


@pytest.mark.asyncio
async def test_initial_items_cannot_set_web_stable_id(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=None))
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "seed"}],
                        "web_stable_id": _FORGED_ID,
                    },
                }
            ],
        },
    )

    assert created.status_code == 201, created.text
    items = (await client.get(f"/v1/sessions/{created.json()['id']}/items")).json()["data"]
    assert [item.get("web_stable_id") for item in items] == [None]


@pytest.mark.asyncio
async def test_durable_startup_input_cannot_set_web_stable_id(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Startup")

    saved = await ManagedStartupInputs(store, HostRegistry()).enqueue(
        conv.id,
        SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "first"}],
                "web_stable_id": _FORGED_ID,
            },
        ),
        user_id=None,
        headers=Headers(),
    )

    assert isinstance(saved.data, MessageData)
    assert saved.data.web_stable_id is None


def test_runner_sse_item_cannot_set_web_stable_id() -> None:
    item = _extract_persistent_item_from_sse(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
                "web_stable_id": _FORGED_ID,
            },
        }
    )

    assert item is not None and isinstance(item.data, MessageData)
    assert item.data.web_stable_id is None


def test_model_input_cannot_set_web_stable_id() -> None:
    raw: dict[str, Any] = {"role": "user", "content": [], "web_stable_id": _FORGED_ID}

    assert MessageData(**raw).web_stable_id is None
    assert MessageData.model_validate(raw).web_stable_id is None
    nested = NewConversationItem.model_validate(
        {"type": "message", "response_id": "r", "data": raw}
    )
    assert isinstance(nested.data, MessageData) and nested.data.web_stable_id is None


def test_stored_web_stable_id_survives_a_read(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    data = MessageData(role="user", content=[{"type": "input_text", "text": "ship it"}])
    [written] = store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_mirror",
                data=data.model_copy(update={"web_stable_id": _WEB_ID}),
            )
        ],
    )

    read = SqlAlchemyConversationStore(db_uri).get_item(conv.id, written.id)
    assert read is not None and isinstance(read.data, MessageData)
    assert read.data.web_stable_id == _WEB_ID
