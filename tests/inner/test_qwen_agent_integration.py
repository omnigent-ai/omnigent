"""Integration tests for the qwen agent fixture, with a mocked ACP subprocess.

Drives :class:`omnigent.inner.qwen_executor.QwenExecutor` end-to-end using the
same harness / model / system prompt as the real agent
(``tests/resources/examples/qwen_perm_test.yaml`` — the permanent twin of the
manual ``tmp/qwen_perm_test.yaml``). The ``qwen --acp`` subprocess is faked via
a stub ``_send`` + the executor's notification queue, so no real ``qwen`` CLI or
LLM is needed.

Covers:
- the agent fixture loads as a qwen + os_env spec,
- a normal message → streamed response turn (with system-prompt folding),
- a mid-turn permission approval round-trip (accept), and
- a mid-turn permission gated by a TOOL_CALL policy DENY.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.inner.qwen_executor import QwenExecutor
from omnigent.spec._omnigent_compat import load_omnigent_yaml

_AGENT_YAML = (
    Path(__file__).resolve().parents[1] / "resources" / "examples" / "qwen_perm_test.yaml"
)
_SESSION_ID = "sess-int"


def _load_agent() -> Any:
    """Load the qwen agent fixture as an AgentSpec."""
    return load_omnigent_yaml(_AGENT_YAML)


def _executor_for_agent() -> QwenExecutor:
    """Build a QwenExecutor wired like the runner would for the fixture.

    The ACP subprocess is pre-faked: marked initialized with a live session so
    ``run_turn`` skips spawning/handshake and goes straight to ``session/prompt``.
    """
    spec = _load_agent()
    ex = QwenExecutor(model=spec.executor.model)
    ex._initialized = True
    ex._session_id = _SESSION_ID
    ex._proc = MagicMock()
    ex._proc.returncode = None
    return ex


def _agent_message_chunk(text: str) -> dict[str, Any]:
    """A ``session/update`` streaming text chunk notification."""
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": _SESSION_ID,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def _permission_request(req_id: int) -> dict[str, Any]:
    """A realistic ``session/request_permission`` (captured from ``qwen --acp``)."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": _SESSION_ID,
            "options": [
                {"optionId": "proceed_always_project", "kind": "allow_always"},
                {"optionId": "proceed_once", "kind": "allow_once"},
                {"optionId": "cancel", "kind": "reject_once"},
            ],
            "toolCall": {
                "toolCallId": "tc-1",
                "status": "pending",
                "title": "rm -f victim.txt (Delete victim.txt)",
                "kind": "execute",
                "rawInput": {"command": "rm -f victim.txt"},
                "_meta": {"toolName": "run_shell_command"},
            },
        },
    }


def _make_fake_send(
    ex: QwenExecutor, loop: asyncio.AbstractEventLoop, *, queue_on_prompt: list[dict[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """Build a stub ``_send`` that fakes one qwen turn.

    On the ``session/prompt`` request it enqueues ``queue_on_prompt`` (chunks
    and/or server-initiated requests qwen would emit mid-turn) then schedules
    the prompt response to resolve with ``end_turn`` on the next loop tick — so
    queued items are processed before the turn completes. All sent messages
    (including the executor's permission replies) are recorded.
    """
    sent: list[dict[str, Any]] = []

    async def fake_send(msg: dict[str, Any]) -> None:
        sent.append(msg)
        if msg.get("method") == "session/prompt":
            req_id = msg["id"]
            for item in queue_on_prompt:
                await ex._queue.put(item)

            def _resolve() -> None:
                fut = ex._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
                    )

            loop.call_soon(_resolve)

    return fake_send, sent


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


def test_agent_fixture_loads_as_qwen_with_os_env() -> None:
    """The fixture loads as a qwen harness agent with local OS access."""
    spec = _load_agent()
    assert spec.executor.config.get("harness") == "qwen"
    assert spec.executor.model == "qwen/qwen3-coder:free"
    assert spec.os_env is not None
    assert spec.os_env.sandbox.type == "none"
    assert spec.instructions  # system prompt present


# ---------------------------------------------------------------------------
# Normal turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_turn_streams_response_and_folds_system_prompt() -> None:
    """A plain message streams a TextChunk + TurnComplete; prompt folds in turn 1."""
    spec = _load_agent()
    ex = _executor_for_agent()
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_agent_message_chunk("Hello!")])
    ex._send = fake_send  # type: ignore[method-assign]

    events = []
    async for ev in ex.run_turn([{"role": "user", "content": "hi there"}], [], spec.instructions):
        events.append(ev)

    # System prompt folded into the first turn's prompt text.
    prompt_msg = next(m for m in sent if m.get("method") == "session/prompt")
    prompt_text = prompt_msg["params"]["prompt"][0]["text"]
    assert spec.instructions.strip().splitlines()[0] in prompt_text
    assert prompt_text.endswith("hi there")

    chunks = [e for e in events if isinstance(e, TextChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert [c.text for c in chunks] == ["Hello!"]
    assert len(completes) == 1
    assert completes[0].response == "Hello!"


# ---------------------------------------------------------------------------
# Approval round-trip (mid-turn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_with_approval_accept_sends_allow_outcome() -> None:
    """A mid-turn permission request is approved via elicitation → allow_once."""
    ex = _executor_for_agent()
    ex._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_permission_request(req_id=999)])
    ex._send = fake_send  # type: ignore[method-assign]

    events = []
    async for ev in ex.run_turn(
        [{"role": "user", "content": "delete victim.txt"}], [], "be helpful"
    ):
        events.append(ev)

    # Elicitation was asked with the real tool name + args from the payload.
    ex._elicitation_handler.assert_awaited_once_with(
        "run_shell_command", {"command": "rm -f victim.txt"}
    )
    # The executor replied to the permission request selecting the once-grant.
    replies = [m for m in sent if m.get("id") == 999 and "result" in m]
    assert replies, "no permission reply was sent"
    assert replies[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "proceed_once"}
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_turn_with_policy_deny_rejects_without_elicitation() -> None:
    """A TOOL_CALL policy DENY rejects the permission and skips elicitation."""
    ex = _executor_for_agent()
    ex._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_DENY")
    )
    ex._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_permission_request(req_id=999)])
    ex._send = fake_send  # type: ignore[method-assign]

    events = []
    async for ev in ex.run_turn(
        [{"role": "user", "content": "delete victim.txt"}], [], "be helpful"
    ):
        events.append(ev)

    replies = [m for m in sent if m.get("id") == 999 and "result" in m]
    assert replies, "no permission reply was sent"
    assert replies[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "cancel"}
    ex._elicitation_handler.assert_not_called()  # policy DENY short-circuits
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_turn_with_file_attachment_reaches_agent() -> None:
    """A message with a file attachment forwards the text AND notes the file.

    Regression: a content-block list (input_text + input_file) used to be
    dropped wholesale by the executor, so file-path messages never reached qwen.
    """
    ex = _executor_for_agent()
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_agent_message_chunk("ok")])
    ex._send = fake_send  # type: ignore[method-assign]

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "review this file"},
                {"type": "input_file", "file_id": "f_1", "filename": "foo.py"},
            ],
        }
    ]
    events = []
    async for ev in ex.run_turn(messages, [], "be helpful"):
        events.append(ev)

    prompt_msg = next(m for m in sent if m.get("method") == "session/prompt")
    prompt_text = prompt_msg["params"]["prompt"][0]["text"]
    assert "review this file" in prompt_text
    assert "[attached file: foo.py]" in prompt_text
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_turn_with_image_forwards_acp_image_block() -> None:
    """An attached image is forwarded as a real ACP image block to qwen.

    Regression: input_image blocks used to be dropped, so a vision request
    ("what is this" + image) reached qwen with no image at all.
    """
    ex = _executor_for_agent()
    ex._image_supported = True  # learned from initialize in real runs
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_agent_message_chunk("a duck")])
    ex._send = fake_send  # type: ignore[method-assign]

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this"},
                {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
            ],
        }
    ]
    events = []
    async for ev in ex.run_turn(messages, [], "be helpful"):
        events.append(ev)

    prompt = next(m for m in sent if m.get("method") == "session/prompt")["params"]["prompt"]
    text_blocks = [b for b in prompt if b["type"] == "text"]
    image_blocks = [b for b in prompt if b["type"] == "image"]
    assert any("what is this" in b["text"] for b in text_blocks)
    assert image_blocks == [{"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}]
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_turn_without_image_capability_falls_back_to_marker() -> None:
    """When qwen lacks image capability, the image degrades to a text marker."""
    ex = _executor_for_agent()
    ex._image_supported = False  # initialize reported no image support
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_agent_message_chunk("ok")])
    ex._send = fake_send  # type: ignore[method-assign]

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,iVBORw0KGgo=",
                    "filename": "p.png",
                },
            ],
        }
    ]
    async for _ in ex.run_turn(messages, [], "be helpful"):
        pass

    prompt = next(m for m in sent if m.get("method") == "session/prompt")["params"]["prompt"]
    assert all(b["type"] != "image" for b in prompt), "no real image block when unsupported"
    text = "\n".join(b["text"] for b in prompt if b["type"] == "text")
    assert "what is this" in text
    assert "[attached image: p.png]" in text


@pytest.mark.asyncio
async def test_turn_with_image_only_omits_empty_text_block() -> None:
    """An image-only message (no text, no system fold) sends just the image block."""
    ex = _executor_for_agent()
    ex._image_supported = True
    ex._system_prompt_sent = True  # simulate a later turn — no system-prompt fold
    loop = asyncio.get_event_loop()
    fake_send, sent = _make_fake_send(ex, loop, queue_on_prompt=[_agent_message_chunk("ok")])
    ex._send = fake_send  # type: ignore[method-assign]

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}
            ],
        }
    ]
    async for _ in ex.run_turn(messages, [], "be helpful"):
        pass

    prompt = next(m for m in sent if m.get("method") == "session/prompt")["params"]["prompt"]
    assert prompt == [{"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}]
