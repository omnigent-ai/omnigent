"""Persisted SDK tool evidence reaches fresh Codex threads as native images."""

from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import patch

import pytest

from omnigent.inner import codex_harness
from omnigent.inner.codex_executor import _prompt_for_turn, _to_codex_input_items
from omnigent.inner.executor import Executor, TurnComplete
from omnigent.runtime.harnesses._executor_adapter import (
    ExecutorAdapter,
    _translate_input_to_messages,
)
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.runtime.mcp_tool_result import encode_mcp_image_result
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server.schemas import CreateResponseRequest
from tests._image_fixtures import _TINY_PNG_BASE64


def _output(*, is_error: bool = False) -> str:
    return encode_mcp_image_result(
        [
            {"type": "text", "text": "before\nsecond line"},
            {"type": "image", "data": _TINY_PNG_BASE64, "mimeType": "image/png"},
            {"type": "text", "text": "correction: East 61; verification_word amber"},
        ],
        is_error=is_error,
    )


def _history(output: str) -> list[dict]:
    return [
        {"type": "message", "role": "user", "content": "Inspect the panel."},
        {"type": "function_call", "name": "panel", "call_id": "call-1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": output},
        {"type": "message", "role": "assistant", "content": "Inspected."},
        {"type": "message", "role": "user", "content": "Read the prior evidence again."},
    ]


def _wire(history: list[dict], *, is_new_thread: bool = True) -> list[dict]:
    messages = _translate_input_to_messages(history, replay_tool_history=True)
    prompt = _prompt_for_turn(messages, is_new_thread=is_new_thread)
    return (
        _to_codex_input_items(prompt)
        if isinstance(prompt, list)
        else [{"type": "text", "text": prompt}]
    )


@pytest.mark.parametrize("is_error", [False, True])
def test_persisted_tool_image_replays_once_in_native_order(is_error: bool) -> None:
    history = _history(_output(is_error=is_error))
    original = copy.deepcopy(history)
    wire = _wire(history)
    images = [item for item in wire if item["type"] == "image"]
    assert images == [{"type": "image", "url": f"data:image/png;base64,{_TINY_PNG_BASE64}"}]
    image_index = next(i for i, item in enumerate(wire) if item["type"] == "image")
    assert wire[image_index - 1]["text"] == "before\nsecond line"
    assert wire[image_index + 1]["text"] == "correction: East 61; verification_word amber"
    text = "\n".join(item["text"] for item in wire if item["type"] == "text")
    assert _TINY_PNG_BASE64 not in text
    assert "Tool call panel (call-1): {}" in text
    assert "Tool result (call-1):" in text
    assert ("Error:" in text) is is_error
    assert history == original


def test_warm_thread_receives_only_latest_user_and_other_adapters_keep_existing_behavior() -> None:
    history = _history(_output())
    assert _wire(history, is_new_thread=False) == [
        {"type": "text", "text": "Read the prior evidence again."}
    ]
    ordinary = _translate_input_to_messages(history)
    assert [message["role"] for message in ordinary] == ["user", "assistant", "user"]
    assert _TINY_PNG_BASE64 not in json.dumps(ordinary)


def test_single_user_tool_history_is_not_discarded() -> None:
    history = _history(_output())
    del history[0]
    assert len([item for item in _wire(history) if item["type"] == "image"]) == 1


@pytest.mark.parametrize("legacy", ["mcp", "anthropic", "newline"])
def test_legacy_tool_images_use_existing_replay_validation(legacy: str) -> None:
    block = {"type": "image", "data": _TINY_PNG_BASE64, "mimeType": "image/png"}
    if legacy == "anthropic":
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _TINY_PNG_BASE64},
        }
    output = json.dumps(block)
    if legacy == "newline":
        output = f"before\n{output}\nafter"
    wire = _wire(_history(output))
    assert [item["url"] for item in wire if item["type"] == "image"] == [
        f"data:image/png;base64,{_TINY_PNG_BASE64}"
    ]


@pytest.mark.parametrize("shape", ["invalid-source", "clipped-envelope", "large-invalid"])
def test_unreplayable_images_never_become_native_input_or_large_prompt_text(shape: str) -> None:
    if shape == "invalid-source":
        output = json.dumps(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "!" * 9000},
            }
        )
    elif shape == "clipped-envelope":
        output = _output()[: _output().index(_TINY_PNG_BASE64) + 25]
    else:
        output = encode_mcp_image_result(
            [{"type": "image", "mimeType": "image/png", "data": "!" * 9000}], is_error=False
        )
    wire = _wire(_history(output))
    assert not any(item["type"] == "image" for item in wire)
    text = "\n".join(item["text"] for item in wire)
    assert "omitted" in text
    assert "!" * 100 not in text
    assert len(text) < 3000


def test_persisted_cap_preserves_later_text_without_inventing_a_lost_image(monkeypatch) -> None:
    monkeypatch.setattr("omnigent.runtime.tool_output.MAX_TOOL_OUTPUT_BYTES", 600)
    oversized = json.loads(_output())
    oversized["content"][1]["data"] = (" " * 20).join(_TINY_PNG_BASE64)
    raw = json.dumps(oversized)
    output = cap_tool_output(raw)
    assert output != raw
    wire = _wire(_history(output))
    assert not any(item["type"] == "image" for item in wire)
    text = "\n".join(item["text"] for item in wire)
    assert "before\nsecond line" in text
    assert "correction: East 61; verification_word amber" in text
    assert "omitted" in text


def test_prior_and_latest_user_images_are_not_serialized_into_text() -> None:
    uri = f"data:image/png;base64,{_TINY_PNG_BASE64}"
    history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": uri}],
        },
        {"type": "message", "role": "assistant", "content": "Saw it."},
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Compare."},
                {"type": "input_image", "image_url": uri},
            ],
        },
    ]
    wire = _wire(history)
    assert len([item for item in wire if item["type"] == "image"]) == 2
    assert uri not in "\n".join(item["text"] for item in wire if item["type"] == "text")
    assert _wire(history, is_new_thread=False) == [
        {"type": "text", "text": "Compare."},
        {"type": "image", "url": uri},
    ]


def test_text_only_prompt_format_stays_unchanged() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "last"},
    ]
    assert _prompt_for_turn(messages, is_new_thread=True) == (
        "Conversation so far:\nuser: first\nassistant: reply\nuser: last\n\n"
        "Respond to the latest user message, using the conversation above as context."
    )


def test_codex_harness_explicitly_enables_tool_history_replay() -> None:
    with patch.object(codex_harness, "ExecutorAdapter") as adapter:
        codex_harness.create_app()
    assert adapter.call_args.kwargs["replay_tool_history"] is True


@pytest.mark.asyncio
async def test_adapter_run_turn_passes_persisted_images_to_the_real_codex_prompt_builder() -> None:
    captured = []

    class CaptureExecutor(Executor):
        async def run_turn(self, messages, tools, system_prompt, config=None):
            prompt = _prompt_for_turn(messages, is_new_thread=True)
            assert isinstance(prompt, list)
            captured.extend(_to_codex_input_items(prompt))
            yield TurnComplete(response="captured")

    adapter = ExecutorAdapter(executor_factory=CaptureExecutor, replay_tool_history=True)
    ctx = TurnContext(response_id="replay", event_queue=asyncio.Queue(), cancelled=asyncio.Event())
    await adapter.run_turn(CreateResponseRequest(model="fixture", input=_history(_output())), ctx)
    assert [item for item in captured if item["type"] == "image"] == [
        {"type": "image", "url": f"data:image/png;base64,{_TINY_PNG_BASE64}"}
    ]
