"""Exercise resolved uploads through the adapter and SDK request boundaries."""

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.native_attachments import (
    FRAMEWORK_NOTICE_BLOCK_TYPE,
    expand_framework_notices,
    framework_notices,
    framework_text_notice_block,
)
from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor
from omnigent.runner.app_support import _resolve_forwarded_message_content
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest
from tests.inner.test_openai_agents_sdk_executor import (
    _fake_agents_sdk,
    _FakeResult,
    _FakeRunner,
)


def _sdk_executor(monkeypatch: pytest.MonkeyPatch, provider: str) -> tuple[Any, list, list, list]:
    """Build a Claude or OpenAI Agents SDK executor over fake SDKs.

    :returns: ``(executor, hooks_seen, prompts_seen, clients)``.
    """
    import claude_agent_sdk

    hooks_seen = []
    prompts_seen = []
    clients = []

    class Result:
        session_id = "claude-session"
        result = "done"

    class Client:
        fail_query = False

        def __init__(self, options):
            self.options = options
            clients.append(self)

        async def connect(self):
            pass

        async def query(self, prompt, session_id):
            if self.fail_query:
                raise RuntimeError("query failed before hook consumption")
            prompts_seen.append(
                prompt if isinstance(prompt, str) else [item async for item in prompt]
            )
            callback = self.options.hooks["UserPromptSubmit"][0].hooks[0]
            hooks_seen.append(await callback({}, None, {}))
            assert await callback({}, None, {}) == {}

        async def receive_response(self):
            yield Result()

        async def disconnect(self):
            pass

    if provider == "openai":
        monkeypatch.setattr(_FakeRunner, "last_calls", [])
        monkeypatch.setattr(_FakeRunner, "next_results", [])
        monkeypatch.setattr(_FakeRunner, "next_result", _FakeResult([], final_output="done"))
        monkeypatch.setattr(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk", _fake_agents_sdk
        )
        executor = OpenAIAgentsSDKExecutor(client=object(), model="test-model")
    else:
        sdk = SimpleNamespace(**vars(claude_agent_sdk))
        sdk.ClaudeSDKClient = Client
        sdk.ResultMessage = Result
        monkeypatch.setattr("omnigent.inner.claude_sdk_executor._ensure_sdk", lambda: sdk)
        executor = ClaudeSDKExecutor()

    async def allow_policy(*args, **kwargs):
        return SimpleNamespace(action="POLICY_ACTION_ALLOW")

    executor._policy_evaluator = allow_policy
    monkeypatch.setattr(
        "omnigent.runtime.harnesses._executor_adapter.is_tracing_enabled", lambda: False
    )
    return executor, hooks_seen, prompts_seen, clients


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "claude"])
@pytest.mark.parametrize("image_first", [False, True])
async def test_runner_upload_notices_reach_sdk_on_fresh_and_reused_sessions(
    monkeypatch: pytest.MonkeyPatch, provider: str, image_first: bool
) -> None:
    from agents.models.chatcmpl_converter import Converter

    executor, hooks_seen, prompts_seen, clients = _sdk_executor(monkeypatch, provider)
    adapter = ExecutorAdapter(lambda: executor, session_key="session")
    history = []

    def respond(request):
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
        width = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200, json={"metadata": {"source_metadata": {"width": width, "height": 4000}}}
        )

    widths = [6000, 8000, None] if image_first else [None, 6000, 8000, None]
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://test"
        ) as client:
            for index, width in enumerate(widths):
                authored = [{"type": "input_text", "text": f"turn {index}"}]
                if width:
                    authored.insert(0, {"type": "input_image", "file_id": str(width)})
                original = copy.deepcopy(authored)
                content = await _resolve_forwarded_message_content(
                    authored, session_id="session", server_client=client
                )
                history.append({"type": "message", "role": "user", "content": content})
                request = CreateResponseRequest(model="test-agent", input=copy.deepcopy(history))
                queue = asyncio.Queue()
                await adapter.run_turn(request, TurnContext(f"r{index}", queue, asyncio.Event()))
                assert authored == original
                emitted = []
                while not queue.empty():
                    emitted.append(queue.get_nowait())
                assert "response.failed" not in str(emitted), emitted

                if provider == "openai":
                    sdk_input = _FakeRunner.last_calls[-1]["input"]
                    assert FRAMEWORK_NOTICE_BLOCK_TYPE not in json.dumps(sdk_input)
                    sdk_input = Converter.items_to_messages(sdk_input)
                    context = json.dumps(
                        [item for item in sdk_input if item.get("role") == "system"]
                    )
                    users = [item for item in sdk_input if item.get("role") == "user"]
                    assert len(users) == 1
                    assert "downscaled" not in json.dumps(users)
                else:
                    context = json.dumps(hooks_seen[-1], ensure_ascii=False)
                    assert "downscaled" not in json.dumps(prompts_seen[-1])
                    assert FRAMEWORK_NOTICE_BLOCK_TYPE not in json.dumps(prompts_seen[-1])
                    assert len(clients) == 1
                    assert executor._pending_framework_context == {}
                if width:
                    assert f"{width}×4000" in context.replace("\\u00d7", "×")
                    assert f"{14000 - width}×4000" not in context.replace("\\u00d7", "×")
                else:
                    assert "downscaled" not in context
                history.append({"type": "message", "role": "assistant", "content": "done"})
            if provider == "claude":
                clients[0].fail_query = True
                content = await _resolve_forwarded_message_content(
                    [{"type": "input_image", "file_id": "6000"}],
                    session_id="session",
                    server_client=client,
                )
                history.append({"type": "message", "role": "user", "content": content})
                with pytest.raises(RuntimeError, match="query failed before hook consumption"):
                    await adapter.run_turn(
                        CreateResponseRequest(model="test-agent", input=history),
                        TurnContext("failed", asyncio.Queue(), asyncio.Event()),
                    )
                assert executor._pending_framework_context == {}
    finally:
        await executor.close()


def test_text_notice_renders_as_system_context_beside_the_user_text() -> None:
    typed = {"type": "input_text", "text": "what failed?"}
    message = {"role": "user", "content": [typed, framework_text_notice_block("run summary")]}

    assert framework_notices(message["content"]) == ["run summary"]
    assert expand_framework_notices([message]) == [
        {"role": "system", "content": [{"type": "input_text", "text": "run summary"}]},
        {"role": "user", "content": [typed]},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "claude"])
@pytest.mark.parametrize("with_image", [False, True])
async def test_code_run_notice_reaches_sdk_context_not_user_text(
    monkeypatch: pytest.MonkeyPatch, provider: str, with_image: bool
) -> None:
    from agents.models.chatcmpl_converter import Converter

    executor, hooks_seen, prompts_seen, _clients = _sdk_executor(monkeypatch, provider)
    adapter = ExecutorAdapter(lambda: executor, session_key="session")
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": "why did it fail?"},
        framework_text_notice_block("CODE-RUN-SUMMARY"),
    ]
    if with_image:
        content.insert(0, {"type": "input_image", "image_url": "data:image/png;base64,cG5n"})
    request = CreateResponseRequest(
        model="test-agent", input=[{"type": "message", "role": "user", "content": content}]
    )
    queue: asyncio.Queue = asyncio.Queue()
    try:
        await adapter.run_turn(request, TurnContext("r0", queue, asyncio.Event()))
    finally:
        await executor.close()
    emitted = []
    while not queue.empty():
        emitted.append(queue.get_nowait())
    assert "response.failed" not in str(emitted), emitted

    if provider == "openai":
        sdk_input = Converter.items_to_messages(_FakeRunner.last_calls[-1]["input"])
        context = json.dumps([item for item in sdk_input if item.get("role") == "system"])
        user_text = json.dumps([item for item in sdk_input if item.get("role") == "user"])
    else:
        context = json.dumps(hooks_seen[-1])
        user_text = json.dumps(prompts_seen[-1])
    assert "CODE-RUN-SUMMARY" in context
    assert "CODE-RUN-SUMMARY" not in user_text
    assert "why did it fail?" in user_text
