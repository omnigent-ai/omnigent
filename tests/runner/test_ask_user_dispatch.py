"""Async questions persist through the existing external-item contract."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from omnigent.entities.conversation import Conversation, MessageData
from omnigent.inner.executor import MockExecutor
from omnigent.policies.types import PolicyResult
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    dispatch_tool_locally,
    execute_tool,
    should_dispatch_locally,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.orchestration import _new_external_conversation_item
from omnigent.server.schemas import SessionEventInput
from omnigent.spec.types import AgentSpec, Phase, PolicyAction


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_advertises_ask_user(spec: AgentSpec | None) -> None:
    tool = next(s for s in build_native_relay_tool_schemas(spec) if s["name"] == "sys_ask_user")
    assert tool["parameters"]["required"] == ["questions"]
    assert should_dispatch_locally("sys_ask_user")


@pytest.mark.asyncio
async def test_ask_user_persists_before_ack_and_forwards_trusted_call_identity() -> None:
    started, persisted = asyncio.Event(), asyncio.Event()
    questions = [
        {"title": "Pick a color", "options": ["Blue", "Green"]},
        {"title": "Any details?"},
    ]
    posted: list[dict] = []
    item = None

    async def server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal item
        assert request.url.path == "/v1/sessions/conv_current/events"
        body = json.loads(request.content)
        posted.append(body)
        assert body["type"] == "external_conversation_item"
        item = _new_external_conversation_item(
            "conv_current", SessionEventInput.model_validate(body)
        )
        started.set()
        await persisted.wait()
        return httpx.Response(200, json={"queued": False, "item_id": item.stable_id})

    def harness_handler(request: httpx.Request) -> httpx.Response:
        assert persisted.is_set()
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"queued": True})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(server_handler), base_url="http://server"
        ) as server,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness_handler), base_url="http://harness"
        ) as harness,
    ):
        task = asyncio.create_task(
            dispatch_tool_locally(
                tool_name="sys_ask_user",
                arguments=json.dumps({"questions": questions}),
                call_id="actual-tool-call",
                response_id="actual-response",
                conversation_id="conv_current",
                agent_name="Cline",
                agent_spec=AgentSpec(spec_version=1),
                server_client=server,
                harness_client=harness,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert not task.done()
        finally:
            persisted.set()
            output = await asyncio.wait_for(task, timeout=2)

    assert item is not None and isinstance(item.data, MessageData)
    prompt = item.data.async_user_input
    assert prompt is not None and prompt.call_id == "actual-tool-call"
    assert [q.model_dump(exclude_none=True) for q in prompt.questions] == questions
    assert prompt.reply_id is not None
    assert item.data.role == "assistant" and item.data.agent == "Cline"
    assert posted[0]["data"]["response_id"] == "actual-response"
    ack = json.loads(output)
    assert ack["status"] == "awaiting_user_input"
    assert ack["item_id"] == item.stable_id
    assert ack["call_id"] == "actual-tool-call"
    assert "answer" not in ack and "approved" not in ack
    assert posted[1] == {"type": "tool_result", "call_id": "actual-tool-call", "output": output}


@pytest.mark.asyncio
async def test_ask_user_direct_replay_identity_and_generated_invocations() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"item_id": "persisted-question"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server:
        for response_id, call_id in (
            ("known-response", "known-call"),
            ("known-response", "known-call"),
            ("later-response", "known-call"),
            (None, None),
            (None, None),
        ):
            output = await execute_tool(
                tool_name="sys_ask_user",
                arguments=json.dumps({"questions": [{"title": "Pick", "options": ["Yes", "No"]}]}),
                conversation_id="conv_current",
                call_id=call_id,
                response_id=response_id,
                server_client=server,
            )
            assert "error" not in json.loads(output)
    source_ids = [b["data"]["source_id"] for b in bodies]
    assert source_ids[0] == source_ids[1]
    assert source_ids[2] != source_ids[0]
    assert len(set(source_ids)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"questions": []},
        {"questions": [{"title": "  "}]},
        {"questions": [{"title": 42}]},
        {"questions": [{"title": "Pick", "options": []}]},
        {"questions": [{"title": "Pick", "options": [" "]}]},
        {"questions": [{"title": "Pick", "options": "Yes"}]},
        {"questions": [{"title": "Pick", "extra": True}]},
        *(
            {"questions": [{"title": "Pick"}], key: "untrusted"}
            for key in ("session_id", "call_id", "agent")
        ),
    ],
)
async def test_ask_user_rejects_invalid_or_identity_bearing_args(args: dict) -> None:
    server = AsyncMock(spec=httpx.AsyncClient)
    output = await execute_tool(
        tool_name="sys_ask_user",
        arguments=json.dumps(args),
        conversation_id="conv_current",
        server_client=server,
    )
    assert "error" in json.loads(output)
    server.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, body", [(403, {}), (500, {}), (200, {}), (200, []), (200, "bad-json")]
)
async def test_ask_user_never_acknowledges_failed_or_unconfirmed_persistence(
    status: int, body: object
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if body == "bad-json":
            return httpx.Response(status, text="not JSON")
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server:
        output = await execute_tool(
            tool_name="sys_ask_user",
            arguments='{"questions": [{"title": "Pick"}]}',
            conversation_id="conv_current",
            server_client=server,
        )
    assert "error" in json.loads(output)
    assert "status" not in json.loads(output)


@pytest.mark.asyncio
async def test_ask_user_preserves_granted_surface_gate() -> None:
    server = AsyncMock(spec=httpx.AsyncClient)
    with patch("omnigent.runner.tool_dispatch._granted_tool_names", return_value=frozenset()):
        output = await execute_tool(
            tool_name="sys_ask_user",
            arguments='{"questions": [{"title": "Pick"}]}',
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
            server_client=server,
        )
    assert "not enabled" in json.loads(output)["error"]
    server.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [PolicyAction.ALLOW, PolicyAction.DENY, PolicyAction.ASK])
async def test_ask_user_uses_existing_server_proxy_and_policy_gates(
    action: PolicyAction, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server proxies this runner-owned name without registering or auto-allowing it."""
    spec = AgentSpec(spec_version=1)
    conversation = Conversation(
        id="conv_current",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_current",
        agent_id="agent_current",
    )
    store = Mock()
    store.get_conversation.return_value = conversation
    arguments = {"questions": [{"title": "Which format?", "options": ["JSON", "CSV"]}]}
    persisted: list[dict] = []
    forwarded: list[dict] = []
    queue = asyncio.Queue()
    context = TurnContext(
        response_id="acp-response",
        session_id="conv_current",
        event_queue=queue,
        cancelled=asyncio.Event(),
    )
    adapter = ExecutorAdapter(MockExecutor, harness_label="Cline")
    adapter._current_ctx, adapter._current_agent = context, "Cline"

    async def server_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/sessions/conv_current/mcp":
            response = await orchestration._handle_mcp_tools_call(
                rpc_id=body["id"],
                session_id="conv_current",
                params=body["params"],
                conversation_store=store,
                agent_store=Mock(),
                runner_router=Mock(),
            )
            return httpx.Response(response.status_code, content=bytes(response.body))
        assert request.url.path == "/v1/sessions/conv_current/events"
        item = _new_external_conversation_item(
            "conv_current", SessionEventInput.model_validate(body)
        )
        persisted.append(body)
        return httpx.Response(202, json={"queued": False, "item_id": item.stable_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server_request), base_url="http://server"
    ) as server:

        async def execute(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/sessions/conv_current/mcp/execute"
            body = json.loads(request.content)
            forwarded.append(body)
            output = await execute_tool(
                tool_name=body["params"]["name"],
                arguments=json.dumps(body["params"]["arguments"]),
                conversation_id="conv_current",
                agent_spec=spec,
                server_client=server,
            )
            return httpx.Response(200, json={"result": {"output": output}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(execute), base_url="http://runner"
        ) as runner:
            policy = AsyncMock(
                return_value=(Mock(), PolicyResult(action=action, reason="test gate"))
            )
            monkeypatch.setattr(orchestration, "_load_agent_spec_for_session", lambda *_: spec)
            monkeypatch.setattr(orchestration, "_evaluate_policy_with_fresh_engine", policy)
            monkeypatch.setattr(
                orchestration, "_get_runner_client", AsyncMock(return_value=runner)
            )
            monkeypatch.setattr(
                orchestration,
                "_register_policy_elicitation",
                AsyncMock(return_value="elicit_gate"),
            )
            monkeypatch.setattr(orchestration, "_pending_policy_ask_writes", {})
            if action == PolicyAction.ALLOW:

                def harness_result(request: httpx.Request) -> httpx.Response:
                    body = json.loads(request.content)
                    assert context._complete_tool(body["call_id"], body["output"])
                    return httpx.Response(200, json={"queued": True})

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(harness_result), base_url="http://harness"
                ) as harness:
                    callback = asyncio.create_task(
                        adapter._stable_tool_executor(
                            "sys_ask_user", arguments, call_id="acp-call"
                        )
                    )
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=2)
                        assert event.type == "response.output_item.done"
                        assert event.item["status"] == "action_required"
                        assert event.item["call_id"] == "acp-call"
                        await dispatch_tool_locally(
                            tool_name=event.item["name"],
                            arguments=event.item["arguments"],
                            call_id=event.item["call_id"],
                            response_id=context.response_id,
                            conversation_id="conv_current",
                            agent_spec=spec,
                            server_client=server,
                            harness_client=harness,
                            mcp_manager=ProxyMcpManager("conv_current", server),
                        )
                        ack = await asyncio.wait_for(callback, timeout=2)
                    finally:
                        if not callback.done():
                            callback.cancel()
                            await asyncio.gather(callback, return_exceptions=True)
            else:
                response = await orchestration._handle_mcp_tools_call(
                    rpc_id=1,
                    session_id="conv_current",
                    params={"name": "sys_ask_user", "arguments": arguments},
                    conversation_store=store,
                    agent_store=Mock(),
                    runner_router=Mock(),
                )
                payload = json.loads(bytes(response.body))

    phases = [call.args[-1].phase for call in policy.await_args_list]
    assert phases[0] == Phase.TOOL_CALL
    assert policy.await_args_list[0].args[-1].tool_name == "sys_ask_user"
    if action == PolicyAction.ALLOW:
        assert ack["status"] == "awaiting_user_input"
        assert len(persisted) == 1 and len(forwarded) == 1
        prompt = persisted[0]["data"]["item_data"]["async_user_input"]
        assert prompt["call_id"] == ack["call_id"] != "acp-call"
        assert persisted[0]["data"]["response_id"] != "acp-response"
        operation = forwarded[0]["_omnigent_operation"]
        assert operation["id"].startswith("mcpop_") and operation["step"] == "initial"
        assert phases == [Phase.TOOL_CALL, Phase.TOOL_RESULT]
    else:
        assert persisted == forwarded == []
        if action == PolicyAction.DENY:
            assert "Denied by policy" in payload["error"]["message"]
        else:
            assert "elicit_gate" in json.dumps(payload)
