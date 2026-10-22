"""Authority belongs to consumed inputs and survives asynchronous dispatch unchanged."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
from omnigent.runner.request_authority import (
    RequesterClient,
    TurnAuthority,
    bind_tool_authority,
    native_snapshot,
)
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.server.schemas import SessionEventInput
from omnigent.session_authority import (
    INPUT_AUTHORITY_FIELD,
    MAX_AUTHORITY_INPUTS,
    REQUEST_AUTHORITY_HEADER,
    SOURCE_SESSION_HEADER,
    RequestAuthority,
    current_request_authority,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.session_message import MESSAGE_TOOL_NAMES
from omnigent.tools.manager import ToolManager


def test_private_transport_is_not_user_writable():
    from omnigent.server.routes._sessions.helpers import _build_new_item

    event = SessionEventInput.model_validate(
        {
            "type": "message",
            "data": {"role": "user", "content": [], "linked_request_id": uuid4().hex},
            "_trusted_transport": {INPUT_AUTHORITY_FIELD: uuid4().hex},
        }
    )
    assert event._trusted_transport == {}
    assert "_trusted_transport" not in event.model_dump()
    assert "linked_request_id" not in _build_new_item(event, "turn").data.model_dump()


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"verified": True, "input_ids": []},
        {"verified": True, "input_ids": ["actor@example.test"]},
        {"verified": True, "input_ids": ["a" * 32, "a" * 32]},
        {"verified": True, "input_ids": [uuid4().hex for _ in range(MAX_AUTHORITY_INPUTS + 1)]},
    ],
)
def test_malformed_authority_is_refused(value):
    assert not RequestAuthority.from_wire(value).verified


def test_steering_does_not_replace_inflight_authority():
    original = RequestAuthority((uuid4().hex,), proofs=("signed-proof",))
    later = RequestAuthority((uuid4().hex,), proofs=("signed-proof",))
    state = TurnAuthority(original)
    inflight = state.snapshot()
    state.begin_injection("queued", later)
    assert not state.snapshot().verified
    state.consume_injection("queued")
    assert state.snapshot().input_ids == original.input_ids + later.input_ids
    assert inflight == original
    state.begin_injection("refused", RequestAuthority((), verified=False))
    state.refuse_injection("refused")
    assert state.snapshot().verified
    state.consume_injection("unknown")
    assert not state.snapshot().verified


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried():
    from omnigent.runner.session_message import execute_message_tool

    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("Accepted response was lost", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout), base_url="http://server"
    ) as client:
        output = await execute_message_tool(
            {"session_id": uuid4().hex, "message": "work"}, client, uuid4().hex
        )
    assert "do not automatically resend" in json.loads(output)["error"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_parallel_http_and_nested_calls_preserve_each_snapshot():
    observed = []

    async def receive(request):
        await asyncio.sleep(0)
        observed.append(
            (
                request.headers[SOURCE_SESSION_HEADER],
                json.loads(request.headers[REQUEST_AUTHORITY_HEADER]),
            )
        )
        assert request.headers["authorization"] == "Bearer test-owner"
        return httpx.Response(200, json={})

    @bind_tool_authority
    async def nested():
        await asyncio.sleep(0)
        return current_request_authority.get()

    @bind_tool_authority
    async def dispatch(*, request_authority):
        task = asyncio.create_task(nested())
        async with RequesterClient(client, "manager", request_authority) as scoped:
            await scoped.get("/v1/sessions", headers={SOURCE_SESSION_HEADER: "forged"})
        return await task

    first, second = (
        RequestAuthority((uuid4().hex,), proofs=("signed-proof",)),
        RequestAuthority((uuid4().hex,), proofs=("signed-proof",)),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive),
        base_url="http://server",
        headers={"Authorization": "Bearer test-owner"},
    ) as client:
        assert await asyncio.gather(
            dispatch(request_authority=first), dispatch(request_authority=second)
        ) == [first, second]
        assert not client.is_closed
        assert SOURCE_SESSION_HEADER not in client.headers
        async with RequesterClient(client, "older-source", first) as older:
            async with RequesterClient(older, "worker", second) as newer:
                await newer.get("/v1/sessions")
    assert observed == [
        ("manager", first.to_wire()),
        ("manager", second.to_wire()),
        ("worker", second.to_wire()),
    ]
    assert current_request_authority.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "mode", ["consumed", "terminal", "missing-client-id", "replacement", "completed", "pending"]
)
async def test_native_consumed_inputs_are_checked_against_rpc_receipts(mode, legacy):
    input_id = uuid4().hex
    state = TurnAuthority(
        RequestAuthority((input_id,), proofs=("signed-proof",)), native_turn_id="turn-1"
    )
    page = {"result": {"data": [{"id": "turn-1", "status": "inProgress"}]}}
    item = {"type": "userMessage", "clientId": input_id}
    if mode == "terminal":
        item["clientId"] = uuid4().hex
    if mode == "missing-client-id":
        item.pop("clientId")
    if mode == "pending":
        state.begin_injection(
            "injection-1", RequestAuthority((uuid4().hex,), proofs=("signed-proof",))
        )
    last_page = {"result": {"data": [{"id": "turn-2"}]}} if mode == "replacement" else page
    if mode == "completed":
        last_page = {"result": {"data": [{"id": "turn-1", "status": "completed"}]}}
    client = AsyncMock()
    client.request.side_effect = [
        page,
        *(
            [
                CodexAppServerResponseError(
                    {"code": -32600, "message": "unknown variant `thread/items/list`"}
                )
            ]
            if legacy
            else []
        ),
        {
            "result": {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "inProgress",
                        "itemsView": "full",
                        "items": [item],
                    }
                ]
                if legacy
                else [{"turnId": "turn-1", "item": item}],
                "nextCursor": None,
            }
        },
        last_page,
    ]
    result = await native_snapshot(client, "thread", state)
    assert result.verified is (mode == "consumed")
    if mode == "consumed":
        assert result.input_ids == (input_id,)
        client.request.assert_any_call(
            "thread/turns/list" if legacy else "thread/items/list",
            {"threadId": "thread", "limit": 1, "sortDirection": "desc", "itemsView": "full"}
            if legacy
            else {"threadId": "thread", "turnId": "turn-1", "limit": 256},
        )
    if mode == "pending":
        client.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "summary",
        "wrong-turn",
        "completed",
        "malformed",
        "oversized",
        "duplicate",
        "rpc-error",
        "bad-page",
    ],
)
async def test_native_receipt_fallback_fails_closed(fault):
    input_id = uuid4().hex
    state = TurnAuthority(
        RequestAuthority((input_id,), proofs=("signed-proof",)), native_turn_id="turn-1"
    )
    page = {"result": {"data": [{"id": "turn-1", "status": "inProgress"}]}}
    item = {"type": "userMessage", "clientId": input_id}
    turn = {"id": "turn-1", "status": "inProgress", "itemsView": "full", "items": [item]}
    if fault == "summary":
        turn["itemsView"] = "summary"
    elif fault == "wrong-turn":
        turn["id"] = "turn-2"
    elif fault == "completed":
        turn["status"] = "completed"
    elif fault == "malformed":
        turn["items"] = [None]
    elif fault == "oversized":
        turn["items"] = [item] * 4097
    elif fault == "duplicate":
        turn["items"] = [item, item]
    error = CodexAppServerResponseError({"code": -32601, "message": "Method not found"})
    if fault == "rpc-error":
        error = CodexAppServerResponseError({"code": -32600, "message": "Invalid cursor"})
    client = AsyncMock()
    client.request.side_effect = [
        page,
        {} if fault == "bad-page" else error,
        {"result": {"data": [turn]}},
        page,
    ]
    if fault in {"rpc-error", "bad-page"}:
        with pytest.raises(CodexAppServerResponseError if fault == "rpc-error" else ValueError):
            await native_snapshot(client, "thread", state)
        assert client.request.call_count == 2
    else:
        assert not (await native_snapshot(client, "thread", state)).verified


@pytest.mark.parametrize("enabled", [False, True])
def test_tool_advertisement_uses_server_snapshot(enabled):
    spec = AgentSpec(spec_version=1, name="manager", skills_filter="none")
    manager = ToolManager(spec, session_messaging_enabled=enabled, os_env_schema_only=True)
    try:
        assert (set(manager.get_tool_names()) >= MESSAGE_TOOL_NAMES) is enabled
    finally:
        manager.shutdown()
    for configured in (spec, None):
        names = {
            schema["name"]
            for schema in build_native_relay_tool_schemas(
                configured, session_messaging_enabled=enabled
            )
        }
        assert (names >= MESSAGE_TOOL_NAMES) is enabled
