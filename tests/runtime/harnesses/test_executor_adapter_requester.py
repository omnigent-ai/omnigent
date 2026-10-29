"""Requester identity across persistent SDK callbacks and the real turn context.

The nearest adapter process fixture scripts MockExecutor events; its setup cannot
retain a real SDK tool callback across executions. These scenarios need controlled
callback/injection barriers and reuse the real scaffold's parsing, context creation,
SSE serialization and teardown rather than the adjacent recording-context stand-ins.

The OpenAI scenario runs the actual SDK in-process with a mocked HTTP model transport.
Claude coverage includes an unbound reader and the real Client/Query/control tasks
with a scripted CLI transport. Codex uses a real JSONL subprocess with a scripted
app-server. These do not validate live providers or SDK/home/sandbox startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import AsyncOpenAI
from starlette.responses import StreamingResponse

from omnigent.inner.claude_sdk_executor import _build_mcp_tools, _tool_call_metadata
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    InjectionDeliveryAmbiguous,
    ToolCallRequest,
    TurnComplete,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import MessageEvent, PolicyVerdictPayload, TurnContext
from omnigent.runtime.requester import (
    REQUESTER_FIELD,
    InputRequester,
    ToolRequester,
    current_tool_requester,
)
from tests._helpers.polling import poll_until_async


class _CallbackExecutor(Executor):
    """Keep one executor alive while the test controls callback and injection delivery."""

    def __init__(self) -> None:
        self.configs: asyncio.Queue[ExecutorConfig] = asyncio.Queue()
        self.finish = asyncio.Event()
        self.injected = asyncio.Event()
        self.finish_injection = asyncio.Event()
        self.injection_outcome: bool | BaseException = True

    async def run_turn(self, messages, tools, system_prompt, config=None):
        assert config is not None
        self.configs.put_nowait(config)
        await self.finish.wait()
        yield TurnComplete(response="done")

    async def enqueue_session_message(self, session_key, content):
        self.injected.set()
        await self.finish_injection.wait()
        if isinstance(self.injection_outcome, BaseException):
            raise self.injection_outcome
        return self.injection_outcome


def _message(origin: InputRequester | None, **fields: Any) -> MessageEvent:
    return MessageEvent.model_validate(
        {
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": "test input",
            **({REQUESTER_FIELD: origin.model_dump()} if origin is not None else {}),
            **fields,
        }
    )


@asynccontextmanager
async def _turn(
    adapter: ExecutorAdapter, executor: _CallbackExecutor, origin: InputRequester | None
) -> AsyncIterator[tuple[TurnContext, asyncio.Queue[dict[str, Any]], ExecutorConfig]]:
    """Use scaffold decoding, context construction, and SSE teardown for each execution."""
    executor.finish.clear()
    response = await adapter._start_or_inject_turn(
        _message(origin).to_create_request(), session_id="test-session"
    )
    assert isinstance(response, StreamingResponse)
    ctx = adapter._active_turn_ctx
    assert ctx is not None
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def read_stream() -> None:
        async for frame in response.body_iterator:
            assert isinstance(frame, bytes)
            events.put_nowait(json.loads(frame.decode().split("data: ", 1)[1]))

    task = asyncio.create_task(read_stream())
    try:
        config = await asyncio.wait_for(executor.configs.get(), timeout=3)
        yield ctx, events, config
    finally:
        ctx._cancel_pending()
        executor.finish.set()
        await asyncio.wait_for(task, timeout=3)


async def _next_call(events: asyncio.Queue[dict[str, Any]]) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            event = await events.get()
            item = event.get("item", {})
            if item.get("type") == "function_call" and item.get("status") == "action_required":
                return item


async def test_persistent_claude_callback_cannot_inherit_either_turns_requester() -> None:
    from claude_agent_sdk import create_sdk_mcp_server
    from claude_agent_sdk._internal.message_parser import parse_message
    from claude_agent_sdk._internal.query import Query
    from mcp.types import CallToolRequest

    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    owner = InputRequester(user_id="owner", account_generation="owner-generation")
    collaborator = InputRequester(user_id="collaborator", account_generation="other-generation")
    calls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    results: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async with _turn(adapter, executor, owner) as (first, _, config):
        assert config.requester == owner
        first_requester = first.requester.snapshot()
        bridge = executor._tool_executor
        [tool] = _build_mcp_tools(
            [{"name": "echo", "parameters": {"type": "object", "properties": {}}}], bridge
        )
        server = create_sdk_mcp_server(name="omnigent", tools=[tool])["instance"]
        handler = server.request_handlers[CallToolRequest]
        received_metadata: list[object] = []

        async def record_request(request):
            received_metadata.append(request.params.meta)
            return await handler(request)

        server.request_handlers[CallToolRequest] = record_request
        query = Query(
            transport=AsyncMock(),
            is_streaming_mode=True,
            sdk_mcp_servers={"omnigent": server},
        )

        async def persistent_reader() -> None:
            assert current_tool_requester.get() == first_requester
            while True:
                results.put_nowait(
                    await query._handle_sdk_mcp_request("omnigent", await calls.get())
                )

        token = current_tool_requester.set(first_requester)
        try:
            reader = asyncio.create_task(persistent_reader())
        finally:
            current_tool_requester.reset(token)

    try:
        for origin in (collaborator, owner):
            async with _turn(adapter, executor, origin) as (second, events, config):
                assert config.requester == origin
                assert executor._tool_executor is bridge
                message = parse_message(
                    {
                        "type": "assistant",
                        "session_id": "test-session",
                        "uuid": "assistant-record",
                        "user_message_uuid": origin.input_id,
                        "message": {
                            "model": "test-model",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_originating_call",
                                    "name": "mcp__omnigent__echo",
                                    "input": {},
                                }
                            ],
                        },
                    }
                )
                assert not hasattr(message, "user_message_uuid")
                block = message.content[0]
                adapter._translate_event(
                    ToolCallRequest(name=block.name, metadata=_tool_call_metadata(block)),
                    second,
                )
                # The SDK receives the provider ID, but currently drops it before dispatch.
                calls.put_nowait(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "echo",
                            "_meta": {"claudecode/toolUseId": "toolu_originating_call"},
                            "arguments": {
                                REQUESTER_FIELD: second.requester.snapshot().model_dump()
                            },
                        },
                    }
                )
                item = await _next_call(events)
                bound = ToolRequester.model_validate(item[REQUESTER_FIELD])
                assert bound.origin.user_id is None
                assert bound.execution_id not in {
                    first_requester.execution_id,
                    second.requester.execution_id,
                }
                assert item["call_id"] == "toolu_originating_call"
                assert received_metadata[-1] is None
                assert second._complete_tool(item["call_id"], '{"ok": true}')
                result = await asyncio.wait_for(results.get(), timeout=3)
                assert json.loads(result["result"]["content"][0]["text"]) == {"ok": True}
                assert adapter._executor is executor
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        await query.close()


async def test_delayed_bound_callback_cannot_rebind_to_a_new_execution() -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    async with _turn(adapter, executor, InputRequester(user_id="first")) as (first, _, _):
        original = first.requester.snapshot()

    async with _turn(adapter, executor, InputRequester(user_id="second")) as (second, events, _):
        adapter._translate_event(
            ToolCallRequest(name="echo", metadata={"call_id": "reused-id"}), second
        )
        result = await executor._tool_executor("echo", {}, call_id="reused-id", requester=original)
        assert result["code"] == "runner_turn_context_desync"
        assert not first._pending_tool_calls
        assert not second._pending_tool_calls
        assert list(adapter._pending_mcp_call_ids) == ["reused-id"]
        assert adapter._executor is executor
        assert adapter._current_ctx is second

        dispatch = asyncio.create_task(
            executor._tool_executor(
                "echo", {}, call_id="reused-id", requester=second.requester.snapshot()
            )
        )
        item = await _next_call(events)
        assert ToolRequester.model_validate(item[REQUESTER_FIELD]).origin.user_id == "second"
        assert second._complete_tool("reused-id", "{}")
        assert await dispatch == {}
        second.cancelled.set()
        cancelled = await executor._tool_executor("echo", {})
        assert cancelled["code"] == "runner_turn_context_desync"
        assert not second._pending_tool_calls


async def test_codex_stdio_producer_binding_survives_reused_reader_and_call_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real JSONL subprocess/reader/adapter/SSE; scripted model and mocked auth stores."""
    from fastapi import Request

    from omnigent.errors import OmnigentError
    from omnigent.inner.codex_executor import CodexExecutor, _CodexAppServerSession
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
    from omnigent.runtime.requester import REQUESTER_HEADER
    from omnigent.server import requester as server_requester
    from omnigent.server import session_orchestration

    script = """
import json, sys
turn = 0
def emit(value):
    print(json.dumps(value), flush=True)
def call(request_id, call_id, turn_id):
    emit({"id": request_id, "method": "item/tool/call", "params": {
        "threadId": "thread", "turnId": turn_id, "callId": call_id,
        "tool": "sys_session_message", "arguments": {"message": "hello"}}})
for line in sys.stdin:
    frame = json.loads(line)
    method = frame.get("method")
    if method == "thread/start":
        emit({"id": frame["id"], "result": {"thread": {"id": "thread"}}})
    elif method == "turn/start":
        turn += 1
        turn_id = f"turn-{turn}"
        emit({"id": frame["id"], "result": {"turn": {"id": turn_id}}})
        emit({"method": "turn/started", "params": {"turnId": turn_id}})
        if turn > 1:
            call(900, "reused-call", f"turn-{turn-1}")
        call(901, "reused-call", turn_id)
    elif method == "turn/steer":
        assert frame["params"]["expectedTurnId"] == turn_id
        emit({"id": frame["id"], "result": {"turnId": turn_id}})
    elif frame.get("id") == 901:
        call(902, "second-call", turn_id)
    elif frame.get("id") == 902:
        emit({"method": "item/agentMessage/delta", "params": {
            "turnId": turn_id, "itemId": "answer", "delta": "done"}})
        emit({"method": "turn/completed", "params": {"turn": {"id": turn_id}}})
"""
    source = SimpleNamespace(
        id="a" * 32,
        runner_id="runner",
        archived=False,
        labels={},
        title="private",
        parent_conversation_id=None,
    )
    conversations = Mock(get_conversation=Mock(return_value=source))
    permissions = Mock(
        get=Mock(return_value=SimpleNamespace(level=session_orchestration.LEVEL_OWNER))
    )
    for module in (session_orchestration, server_requester):
        monkeypatch.setattr(module, "token_bound_runner_id", lambda _: "runner")
    monkeypatch.setattr(session_orchestration, "account_generation", lambda _: "generation")
    release_delayed = asyncio.Event()
    delayed = None
    sessions = []
    replies = []

    def factory(**kwargs):
        bridge = kwargs.pop("tool_executor")

        async def dispatch(name, args, **binding):
            nonlocal delayed
            if delayed is None:

                async def invoke_later():
                    await release_delayed.wait()
                    return await bridge(name, args, **binding)

                delayed = asyncio.create_task(invoke_later())
            return await bridge(name, args, **binding)

        session = _CodexAppServerSession(**kwargs, tool_executor=dispatch)
        sessions.append(session)

        async def start():
            if session._proc is None:
                session._proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-u",
                    "-c",
                    script,
                    cwd=tmp_path,
                    env={},
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                session._reader_task = asyncio.create_task(session._reader_loop())

        send_response = session._send_response

        async def record_response(request_id, result):
            replies.append((request_id, result))
            await send_response(request_id, result)

        monkeypatch.setattr(session, "start", start)
        monkeypatch.setattr(session, "_send_response", record_response)
        # This test owns its fake subprocess; production SDK/home/sandbox startup is bypassed.
        monkeypatch.setattr(session, "close", AsyncMock())
        return session

    executor = CodexExecutor(
        codex_path=sys.executable, model="test-model", app_session_factory=factory
    )
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    readers = []
    try:
        for index, user in enumerate(("owner", "collaborator", "owner")):
            response = await adapter._start_or_inject_turn(
                _message(
                    InputRequester(user_id=user, account_generation="generation"),
                    tools=[
                        {
                            "type": "function",
                            "name": "sys_session_message",
                            "parameters": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                            },
                        }
                    ],
                ).to_create_request(),
                session_id="test-session",
            )
            assert isinstance(response, StreamingResponse)
            ctx = adapter._active_turn_ctx
            assert ctx is not None
            if index == 1:
                release_delayed.set()
            dispatched = []
            async with asyncio.timeout(10):
                async for frame in response.body_iterator:
                    event = json.loads(frame.decode().split("data: ", 1)[1])
                    item = event.get("item", {})
                    if item.get("status") != "action_required":
                        continue
                    bound = ToolRequester.model_validate(item[REQUESTER_FIELD])
                    assert bound.execution_id == ctx.requester.execution_id
                    expected_user = None if index == 2 and dispatched else user
                    assert bound.origin.user_id == expected_user
                    request = Request(
                        {
                            "type": "http",
                            "app": SimpleNamespace(
                                state=SimpleNamespace(
                                    feature_flags=SimpleNamespace(enabled=lambda _: True)
                                )
                            ),
                            "headers": [
                                (name.lower().encode(), value.encode())
                                for name, value in {
                                    REQUESTER_HEADER: bound.model_dump_json(),
                                    RUNNER_TUNNEL_TOKEN_HEADER: "test-token",
                                    ORCHESTRATION_SOURCE_HEADER: source.id,
                                }.items()
                            ],
                        }
                    )
                    if expected_user == "owner":
                        assert (
                            session_orchestration.require_orchestration_source(
                                request, "owner", conversations, permissions
                            )
                            is source
                        )
                    else:
                        with pytest.raises(OmnigentError, match="verified owner execution"):
                            session_orchestration.require_orchestration_source(
                                request, "owner", conversations, permissions
                            )
                    dispatched.append(item)
                    if index == 2 and len(dispatched) == 1:
                        injected = await adapter._start_or_inject_turn(
                            _message(
                                InputRequester(user_id="collaborator"), injection_id="steer"
                            ).to_create_request()
                        )
                        assert injected.status_code == 204
                        await poll_until_async(
                            lambda ctx=ctx: (
                                ctx.requester.origin.user_id is None and not ctx.requester._pending
                            ),
                            timeout_s=3,
                        )
                    assert ctx._complete_tool(item["call_id"], '{"ok": true}')
            assert [item["call_id"] for item in dispatched] == ["reused-call", "second-call"]
            readers.append(sessions[0]._reader_task)
        assert len(sessions) == 1
        assert readers[0] is readers[1] is readers[2]
        assert delayed is not None
        assert (await delayed)["code"] == "runner_turn_context_desync"
        stale_results = [result for request_id, result in replies if request_id == 900]
        assert len(stale_results) == 2
        for result in stale_results:
            assert result["success"] is False
            assert (
                json.loads(result["contentItems"][0]["text"])["code"]
                == "runner_turn_context_desync"
            )
    finally:
        release_delayed.set()
        if delayed is not None:
            delayed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delayed
        await adapter.on_shutdown()
        for session in sessions:
            if session._reader_task is not None:
                session._reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session._reader_task
            if session._proc is not None:
                if session._proc.returncode is None:
                    session._proc.terminate()
                await asyncio.wait_for(session._proc.wait(), timeout=3)


async def test_codex_unbound_callback_cannot_borrow_later_owner_or_ambient_requester() -> None:
    from omnigent.inner.codex_executor import _CodexAppServerSession

    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    async with _turn(adapter, executor, InputRequester(user_id="owner")) as (first, _, _):
        original = first.requester.snapshot()
        session = _CodexAppServerSession(
            codex_path="unused", cwd=".", env={}, tool_executor=executor._tool_executor
        )

    async with _turn(adapter, executor, InputRequester(user_id="owner")) as (later, events, _):
        token = current_tool_requester.set(original)
        try:
            dispatch = asyncio.create_task(
                session._execute_dynamic_tool(
                    "sys_session_message", {REQUESTER_FIELD: original.model_dump()}
                )
            )
        finally:
            current_tool_requester.reset(token)
        item = await _next_call(events)
        bound = ToolRequester.model_validate(item[REQUESTER_FIELD])
        assert bound.origin.user_id is None
        assert bound.execution_id not in {original.execution_id, later.requester.execution_id}
        assert later._complete_tool(item["call_id"], "{}")
        assert await dispatch == {}


async def test_bound_parallel_calls_keep_exact_ids_and_immutable_requesters() -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    async with _turn(adapter, executor, InputRequester(user_id="first")) as (ctx, events, _):
        original = ctx.requester.snapshot()
        for call_id in ("one", "two"):
            adapter._translate_event(
                ToolCallRequest(name="echo", metadata={"call_id": call_id}), ctx
            )
        first_dispatch = asyncio.create_task(
            executor._tool_executor("echo", {}, call_id="two", requester=original)
        )
        first_item = await _next_call(events)
        ctx.requester.consume(InputRequester(user_id="different"))
        second_dispatch = asyncio.create_task(
            executor._tool_executor("echo", {}, call_id="one", requester=original)
        )
        second_item = await _next_call(events)
        assert (first_item["call_id"], second_item["call_id"]) == ("two", "one")
        assert ToolRequester.model_validate(first_item[REQUESTER_FIELD]) == original
        assert ToolRequester.model_validate(second_item[REQUESTER_FIELD]).origin.user_id is None
        assert not adapter._pending_mcp_call_ids
        assert ctx._complete_tool("one", '{"order": 1}')
        assert ctx._complete_tool("two", '{"order": 2}')
        assert await first_dispatch == {"order": 2}
        assert await second_dispatch == {"order": 1}


@pytest.mark.parametrize("outcome", [True, False, "ambiguous", "error", "cancelled"])
@pytest.mark.parametrize("injection_user", ["first", "different", None])
async def test_injection_authority_is_restricted_until_delivery_is_known(
    outcome: bool | str, injection_user: str | None
) -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    executor.injection_outcome = {
        "ambiguous": InjectionDeliveryAmbiguous("delivery reply lost"),
        "error": RuntimeError("delivery failed"),
        "cancelled": asyncio.CancelledError(),
    }.get(outcome, outcome)
    async with _turn(adapter, executor, InputRequester(user_id="first")) as (ctx, events, _):
        original = ctx.requester.snapshot()
        response = await adapter._start_or_inject_turn(
            _message(
                InputRequester(user_id=injection_user) if injection_user else None,
                injection_id="injection",
            ).to_create_request()
        )
        assert response.status_code == 204
        await asyncio.wait_for(executor.injected.wait(), timeout=3)
        assert ctx.requester.snapshot().origin.user_id is None
        dispatch = asyncio.create_task(
            executor._tool_executor("echo", {}, call_id="pending", requester=original)
        )
        item = await _next_call(events)
        assert ToolRequester.model_validate(item[REQUESTER_FIELD]).origin.user_id is None
        assert ctx._complete_tool("pending", "{}")
        assert await dispatch == {}
        executor.finish_injection.set()
        await poll_until_async(lambda: not ctx.requester._pending, timeout_s=3)
        expected = "first" if outcome is False or injection_user == "first" else None
        assert ctx.requester.snapshot().origin.user_id == expected
        assert original.origin.user_id == "first"


async def test_native_requester_only_resolves_on_existing_codex_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
    from omnigent.inner.codex_native_executor import CodexNativeExecutor
    from omnigent.runtime.requester import ExecutionRequester

    def unexpected_factory() -> Executor:
        pytest.fail("requester lookup must not construct an executor")

    adapter = ExecutorAdapter(executor_factory=unexpected_factory)
    identity = CodexExecutionIdentity("session", "thread", "turn")
    assert await adapter.native_tool_requester(identity.to_wire()) is None
    adapter._executor = _CallbackExecutor()
    assert await adapter.native_tool_requester(identity.to_wire()) is None

    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    expected = ExecutionRequester(InputRequester(user_id="origin")).snapshot()
    resolve = AsyncMock(return_value=expected)
    monkeypatch.setattr(executor, "resolve_tool_requester", resolve)
    adapter._executor = executor
    assert await adapter.native_tool_requester({"thread_id": "thread"}) is None
    resolve.assert_not_called()
    assert await adapter.native_tool_requester(identity.to_wire()) is expected
    resolve.assert_awaited_once_with(identity)


@pytest.mark.parametrize("injection_user", ["first", "different", None])
async def test_codex_injection_forwards_the_protected_input_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injection_user: str | None
) -> None:
    from omnigent.inner.codex_native_executor import CodexNativeExecutor

    controller = _CallbackExecutor()
    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    monkeypatch.setattr(executor, "run_turn", controller.run_turn)
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "enqueue_session_message", enqueue)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    origin = InputRequester(user_id=injection_user) if injection_user else None
    async with _turn(adapter, controller, InputRequester(user_id="first")):
        await adapter._start_or_inject_turn(
            _message(origin, injection_id="steer").to_create_request()
        )
        await poll_until_async(lambda: enqueue.await_count == 1, timeout_s=3)
        assert enqueue.call_args.args == (adapter._session_key, "test input")
        forwarded = enqueue.call_args.kwargs["requester"]
        assert isinstance(forwarded, InputRequester)
        assert forwarded.user_id == injection_user
        if origin is not None:
            assert forwarded == origin


async def test_real_openai_sdk_cached_tools_use_each_runs_execution() -> None:
    from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor

    requests: list[dict[str, Any]] = []
    release_delayed_call = asyncio.Event()
    delayed_call: asyncio.Task[dict[str, Any]] | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal delayed_call
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            state = executor._session_states[adapter._session_key]
            callback = state.agent.tools[0].on_invoke_tool

            async def invoke_later() -> dict[str, Any]:
                await release_delayed_call.wait()
                return await callback(None, "{}")

            delayed_call = asyncio.create_task(invoke_later())
        elif len(requests) == 3:
            release_delayed_call.set()
        tool_result = body["messages"][-1]["role"] == "tool"
        delta = (
            {"content": "done"}
            if tool_result
            else {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"provider-call-{len(requests)}",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            }
        )
        chunk = {
            "id": f"completion-{len(requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "stop" if tool_result else "tool_calls",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="http://model.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        executor = OpenAIAgentsSDKExecutor(client=client, model="test-model", use_responses=False)
        executor._policy_evaluator = AsyncMock(
            return_value=PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
        )
        adapter = ExecutorAdapter(executor_factory=lambda: executor)
        history: list[dict[str, str]] = []
        callbacks = []
        execution_ids = set()
        try:
            for user in ("first", "second", None):
                history.append({"role": "user", "content": f"input from {user}"})
                origin = InputRequester(user_id=user) if user else None
                response = await adapter._start_or_inject_turn(
                    _message(
                        origin,
                        content=list(history),
                        tools=[
                            {
                                "type": "function",
                                "name": "echo",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    ).to_create_request(),
                    session_id="sdk-session",
                )
                assert isinstance(response, StreamingResponse)
                ctx = adapter._active_turn_ctx
                assert ctx is not None
                dispatched = []
                terminal = None
                async with asyncio.timeout(10):
                    async for frame in response.body_iterator:
                        event = json.loads(frame.decode().split("data: ", 1)[1])
                        terminal = event["type"]
                        item = event.get("item", {})
                        if item.get("status") == "action_required":
                            dispatched.append(item)
                            assert ctx._complete_tool(item["call_id"], '{"ok": true}')
                assert terminal == "response.completed"
                [item] = dispatched
                requester = ToolRequester.model_validate(item[REQUESTER_FIELD])
                assert requester.origin.user_id == user
                assert requester.execution_id == ctx.requester.execution_id
                execution_ids.add(requester.execution_id)
                state = executor._session_states[adapter._session_key]
                callbacks.append(state.agent.tools[0].on_invoke_tool)
                history.append({"role": "assistant", "content": "done"})
            assert len(execution_ids) == 3
            assert callbacks[0] is callbacks[1] is callbacks[2]
            assert len(requests) == 6
            assert delayed_call is not None
            assert (await delayed_call)["code"] == "runner_turn_context_desync"
        finally:
            if delayed_call is not None:
                delayed_call.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await delayed_call
            await adapter.on_shutdown()


async def test_claude_sdk_raw_transport_binds_calls_before_query_drops_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real Client/Query/tasks, with only the CLI byte source simulated (no provider)."""
    from claude_agent_sdk._internal.transport import subprocess_cli

    from omnigent.inner import claude_sdk_executor as claude

    submitted: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    connected = []

    class ScriptedCLI:
        def __init__(self, prompt, options):
            assert prompt == ""
            assert options.permission_prompt_tool_name == "stdio"
            self._process = None
            self._stdout_stream = self._stdin_stream = self._stderr_stream = None
            self.options = options
            connected.append(self)

        async def connect(self):
            pass

        async def write(self, data):
            frame = json.loads(data)
            if frame["type"] == "user":
                submitted.put_nowait(frame)
            elif frame["type"] == "control_request":
                frames.put_nowait(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": frame["request_id"],
                            "response": {},
                        },
                    }
                )
            else:
                replies.put_nowait(frame)

        async def read_messages(self):
            while True:
                yield await frames.get()

        async def close(self):
            pass

    monkeypatch.setattr(subprocess_cli, "SubprocessCLITransport", ScriptedCLI)
    monkeypatch.setattr(claude, "_find_system_claude", lambda: None)
    executor = claude.ClaudeSDKExecutor()
    executor._policy_evaluator = AsyncMock(
        return_value=PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
    )
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    finished = asyncio.Event()
    delayed_started = asyncio.Event()
    release_delayed = asyncio.Event()
    received_metadata = []
    reader_tasks = []
    bindings = []

    def assistant(call_id, *, echo=None, parent=None, name="mcp__omnigent__echo"):
        frames.put_nowait(
            {
                "type": "assistant",
                "session_id": "provider-session",
                "uuid": f"row-{call_id}",
                "parent_tool_use_id": parent,
                **({"user_message_uuid": echo, "user_message_uuids": [echo]} if echo else {}),
                "message": {
                    "model": "test-model",
                    "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {}}],
                },
            }
        )

    def call(call_id, *, arguments=None):
        frames.put_nowait(
            {
                "type": "control_request",
                "request_id": call_id,
                "request": {
                    "subtype": "mcp_message",
                    "server_name": "omnigent",
                    "message": {
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "tools/call",
                        "params": {
                            "name": "echo",
                            "arguments": arguments or {},
                            "_meta": {"claudecode/toolUseId": call_id},
                        },
                    },
                },
            }
        )

    try:
        for index, user in enumerate(("owner", "collaborator", "owner")):
            finished.clear()
            response = await adapter._start_or_inject_turn(
                _message(
                    InputRequester(user_id=user),
                    tools=[
                        {
                            "type": "function",
                            "name": "echo",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                ).to_create_request(),
                session_id="sdk-session",
            )
            assert isinstance(response, StreamingResponse)
            ctx = adapter._active_turn_ctx
            assert ctx is not None
            dispatched = asyncio.Queue()

            async def consume(response=response, ctx=ctx, dispatched=dispatched):
                async for frame in response.body_iterator:
                    event = json.loads(frame.decode().split("data: ", 1)[1])
                    item = event.get("item", {})
                    if item.get("status") == "action_required":
                        dispatched.put_nowait(item)
                        assert ctx._complete_tool(item["call_id"], '{"ok": true}')
                finished.set()

            consumer = asyncio.create_task(consume())
            try:
                submission = await asyncio.wait_for(submitted.get(), timeout=5)
                state = executor._clients[adapter._session_key]
                query = state.client._query
                reader_tasks.append(query._read_task)
                # The real SDK parser drops input UUIDs; its MCP dispatcher also drops _meta.
                if index == 0:
                    from mcp.types import CallToolRequest

                    server = query.sdk_mcp_servers["omnigent"]
                    handler = server.request_handlers[CallToolRequest]

                    async def delayed_handler(request, handler=handler):
                        received_metadata.append(request.params.meta)
                        binding = claude._claude_tool_binding.get()
                        if binding and binding.call_id == "late-owner":
                            delayed_started.set()
                            await release_delayed.wait()
                            assert claude._claude_tool_binding.get() is binding
                        return await handler(request)

                    server.request_handlers[CallToolRequest] = delayed_handler
                root_id = f"root-{index}"
                frames.put_nowait(
                    {
                        "type": "stream_event",
                        "uuid": f"stream-{index}",
                        "session_id": "provider-session",
                        "parent_tool_use_id": None,
                        "user_message_uuid": submission["uuid"],
                        "event": {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "tool_use",
                                "id": f"partial-{index}",
                                "name": "mcp__omnigent__echo",
                                "input": {},
                            },
                        },
                    }
                )
                # A partial streamed block is not a completed assistant call record.
                call(f"partial-{index}")
                item = await asyncio.wait_for(dispatched.get(), timeout=3)
                assert ToolRequester.model_validate(item[REQUESTER_FIELD]).origin.user_id is None
                await asyncio.wait_for(replies.get(), timeout=3)
                assistant(root_id, echo=submission["uuid"])
                call(root_id)
                await poll_until_async(
                    lambda dispatched=dispatched: not dispatched.empty() or not replies.empty(),
                    timeout_s=3,
                )
                assert not dispatched.empty(), json.dumps(await replies.get())
                item = await asyncio.wait_for(dispatched.get(), timeout=3)
                bound = ToolRequester.model_validate(item[REQUESTER_FIELD])
                assert bound == ctx.requester.snapshot()
                assert item["call_id"] == root_id
                bindings.append(bound)
                await asyncio.wait_for(replies.get(), timeout=3)

                # Later assistant messages omit the root echo; children join the retained parent.
                assistant(f"spawn-{index}", name="Agent")
                assistant(f"child-{index}", parent=f"spawn-{index}")
                call(f"child-{index}")
                item = await asyncio.wait_for(dispatched.get(), timeout=3)
                assert ToolRequester.model_validate(item[REQUESTER_FIELD]) == bound
                await asyncio.wait_for(replies.get(), timeout=3)

                if index == 0:
                    assistant("late-owner")
                    call("late-owner")
                    await asyncio.wait_for(delayed_started.wait(), timeout=3)
                if index == 1:
                    release_delayed.set()
                    reply = await asyncio.wait_for(replies.get(), timeout=3)
                    result = reply["response"]["response"]["mcp_response"]["result"]
                    assert (
                        json.loads(result["content"][0]["text"])["code"]
                        == "runner_turn_context_desync"
                    )
                    assert dispatched.empty()
                    # A child still emitting after its original root ended cannot borrow this one.
                    assistant("late-child", parent="spawn-0")
                    call("late-child")
                    reply = await asyncio.wait_for(replies.get(), timeout=3)
                    result = reply["response"]["response"]["mcp_response"]["result"]
                    assert (
                        json.loads(result["content"][0]["text"])["code"]
                        == "runner_turn_context_desync"
                    )
                    assert dispatched.empty()
                # A call missing its producer record cannot use argument-supplied authority.
                call(f"unrecorded-{index}", arguments={REQUESTER_FIELD: bound.model_dump()})
                item = await asyncio.wait_for(dispatched.get(), timeout=3)
                assert ToolRequester.model_validate(item[REQUESTER_FIELD]).origin.user_id is None
                await asyncio.wait_for(replies.get(), timeout=3)
                frames.put_nowait(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "duration_ms": 1,
                        "duration_api_ms": 1,
                        "num_turns": 1,
                        "session_id": "provider-session",
                        "total_cost_usd": 0,
                        "result": "done",
                        "uuid": f"result-{index}",
                    }
                )
                await asyncio.wait_for(finished.wait(), timeout=3)
            finally:
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer
        assert len(connected) == 1
        assert reader_tasks[0] is reader_tasks[1] is reader_tasks[2]
        assert {binding.origin.user_id for binding in bindings} == {"owner", "collaborator"}
        assert len({binding.execution_id for binding in bindings}) == 3
        assert received_metadata and all(meta is None for meta in received_metadata)
        assert claude._claude_tool_binding.get() is None
    finally:
        release_delayed.set()
        await adapter.on_shutdown()
