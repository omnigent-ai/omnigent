"""Unit tests for GooseExecutor (headless Goose ACP / JSON-RPC 2.0 mode).

Covers construction defaults, provider-env overrides, tool-call extraction and
permission-outcome mapping from Goose's ACP ``session/request_permission`` shape,
usage mapping, prompt-block folding, the permission → policy/elicitation
round-trip, run_turn streaming, and the harness wrap. Protocol shapes match a
verified Goose 1.38 ``goose acp`` session.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.inner.goose_executor import GooseExecutor

# ---------------------------------------------------------------------------
# Construction / attribute defaults
# ---------------------------------------------------------------------------


def test_executor_default_attributes() -> None:
    executor = GooseExecutor(goose_path="goose")
    assert executor._goose_path == "goose"
    assert executor._model is None
    assert executor._provider is None
    assert executor._builtins == ("developer",)
    assert executor._proc is None
    assert executor._session_id is None
    assert executor._initialized is False
    assert executor._rpc_id == 0
    assert executor.max_context_tokens() is None


def test_executor_custom_model_provider_builtins() -> None:
    executor = GooseExecutor(
        model="claude-x", provider="anthropic", builtins=("developer", "computercontroller")
    )
    assert executor._model == "claude-x"
    assert executor._provider == "anthropic"
    assert executor._builtins == ("developer", "computercontroller")


def test_executor_cwd_defaults_and_explicit() -> None:
    assert GooseExecutor()._cwd == os.getcwd()
    assert GooseExecutor(cwd="/tmp")._cwd == "/tmp"


def test_provider_env_only_sets_when_present() -> None:
    assert GooseExecutor()._provider_env() == {}
    assert GooseExecutor(provider="anthropic")._provider_env() == {"GOOSE_PROVIDER": "anthropic"}
    assert GooseExecutor(model="claude-x")._provider_env() == {"GOOSE_MODEL": "claude-x"}
    assert GooseExecutor(provider="anthropic", model="claude-x")._provider_env() == {
        "GOOSE_PROVIDER": "anthropic",
        "GOOSE_MODEL": "claude-x",
    }


# ---------------------------------------------------------------------------
# Tool-call extraction (Goose ACP shapes)
# ---------------------------------------------------------------------------


def test_extract_tool_call_uses_title_and_raw_input() -> None:
    """Goose's permission ``toolCall`` names the tool via ``title`` + ``rawInput``."""
    params = {
        "toolCall": {
            "kind": "other",
            "status": "pending",
            "title": "shell",
            "rawInput": {"command": "echo hi"},
        }
    }
    name, args = GooseExecutor._extract_tool_call(params)
    assert name == "shell"
    assert args == {"command": "echo hi"}


def test_extract_tool_call_prefers_meta_tool_name() -> None:
    """When the precise ``_meta.goose.toolCall.toolName`` is present, prefer it."""
    params = {
        "toolCall": {
            "kind": "other",
            "title": "shell · echo hi",
            "rawInput": {"command": "echo hi"},
            "_meta": {"goose": {"toolCall": {"toolName": "developer__shell"}}},
        }
    }
    name, args = GooseExecutor._extract_tool_call(params)
    assert name == "developer__shell"
    assert args == {"command": "echo hi"}


def test_extract_tool_call_falls_back_to_kind_then_tool() -> None:
    assert GooseExecutor._extract_tool_call({"toolCall": {"kind": "execute"}}) == ("execute", {})
    assert GooseExecutor._extract_tool_call({}) == ("tool", {})


# ---------------------------------------------------------------------------
# Permission outcome mapping (Goose option kinds)
# ---------------------------------------------------------------------------

_GOOSE_OPTIONS = [
    {"optionId": "allow_always", "name": "allow_always", "kind": "allow_always"},
    {"optionId": "allow_once", "name": "allow_once", "kind": "allow_once"},
    {"optionId": "reject_once", "name": "reject_once", "kind": "reject_once"},
    {"optionId": "reject_always", "name": "reject_always", "kind": "reject_always"},
]


def test_permission_outcome_allow_prefers_once() -> None:
    out = GooseExecutor._permission_outcome({"options": _GOOSE_OPTIONS}, allow=True)
    assert out == {"outcome": "selected", "optionId": "allow_once"}


def test_permission_outcome_deny_prefers_reject_once() -> None:
    out = GooseExecutor._permission_outcome({"options": _GOOSE_OPTIONS}, allow=False)
    assert out == {"outcome": "selected", "optionId": "reject_once"}


def test_permission_outcome_cancels_when_no_matching_option() -> None:
    # allow requested but only reject options offered → cancelled (fail-safe).
    only_reject = [{"optionId": "r", "kind": "reject_once"}]
    assert GooseExecutor._permission_outcome({"options": only_reject}, allow=True) == {
        "outcome": "cancelled"
    }
    assert GooseExecutor._permission_outcome({"options": []}, allow=False) == {
        "outcome": "cancelled"
    }


# ---------------------------------------------------------------------------
# Usage mapping
# ---------------------------------------------------------------------------


def test_usage_from_result_maps_goose_keys() -> None:
    result = {"stopReason": "end_turn", "usage": {"totalTokens": 100, "inputTokens": 80, "outputTokens": 20}}
    assert GooseExecutor._usage_from_result(result) == {
        "input_tokens": 80,
        "output_tokens": 20,
        "total_tokens": 100,
    }


def test_usage_from_result_none_when_absent() -> None:
    assert GooseExecutor._usage_from_result({"stopReason": "end_turn"}) is None
    assert GooseExecutor._usage_from_result({"usage": "nope"}) is None


# ---------------------------------------------------------------------------
# Prompt-block folding
# ---------------------------------------------------------------------------


def test_text_from_blocks_text_and_file() -> None:
    blocks = [
        {"type": "input_text", "text": "do the thing"},
        {"type": "input_file", "filename": "a.txt", "file_data": "data:text/plain;base64,aGk="},
        {"type": "input_file", "filename": "b.pdf", "file_data": "data:application/pdf;base64,AAA="},
    ]
    text = GooseExecutor._text_from_blocks(blocks)
    assert "do the thing" in text
    assert "--- attached file: a.txt ---\nhi\n--- end of a.txt ---" in text
    assert "[attached file: b.pdf]" in text  # binary → marker, not inlined


# ---------------------------------------------------------------------------
# Permission round-trip (agent → client request)
# ---------------------------------------------------------------------------


def _perm_request(req_id: int = 9) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "20260623_1",
            "options": _GOOSE_OPTIONS,
            "toolCall": {
                "kind": "other",
                "status": "pending",
                "title": "shell",
                "rawInput": {"command": "rm -f victim.txt"},
            },
        },
    }


@pytest.mark.asyncio
async def test_respond_to_permission_allows_when_no_gates_wired() -> None:
    executor = GooseExecutor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(_perm_request())
    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}


@pytest.mark.asyncio
async def test_respond_to_permission_denied_by_policy() -> None:
    executor = GooseExecutor()
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_DENY")
    )
    executor._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(_perm_request())

    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    executor._elicitation_handler.assert_not_called()  # DENY short-circuits
    phase, data = executor._policy_evaluator.call_args.args
    assert phase == "PHASE_TOOL_CALL"
    assert data == {"name": "shell", "arguments": {"command": "rm -f victim.txt"}}


@pytest.mark.asyncio
async def test_respond_to_permission_elicitation_allow_and_deny() -> None:
    # Accept → allow_once.
    allow_exec = GooseExecutor()
    allow_exec._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent_a: list[dict] = []
    allow_exec._send = AsyncMock(side_effect=lambda m: sent_a.append(m))  # type: ignore[method-assign]
    await allow_exec._respond_to_agent_request(_perm_request())
    assert sent_a[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}
    allow_exec._elicitation_handler.assert_awaited_once_with("shell", {"command": "rm -f victim.txt"})

    # Deny → reject_once.
    deny_exec = GooseExecutor()
    deny_exec._elicitation_handler = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    sent_d: list[dict] = []
    deny_exec._send = AsyncMock(side_effect=lambda m: sent_d.append(m))  # type: ignore[method-assign]
    await deny_exec._respond_to_agent_request(_perm_request())
    assert sent_d[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}


@pytest.mark.asyncio
async def test_respond_to_unknown_method_returns_jsonrpc_error() -> None:
    executor = GooseExecutor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 11, "method": "terminal/create", "params": {}}
    )
    assert sent[0]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# run_turn streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_streams_text_and_usage() -> None:
    """run_turn yields TextChunk for agent_message_chunk and a TurnComplete with
    usage parsed from the final session/prompt result."""
    executor = GooseExecutor()
    executor._initialized = True
    executor._session_id = "20260623_1"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            req_id = msg["id"]
            await executor._queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Done"},
                        }
                    },
                }
            )

            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "stopReason": "end_turn",
                                "usage": {"totalTokens": 10, "inputTokens": 7, "outputTokens": 3},
                            },
                        }
                    )

            loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]

    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "be nice")]
    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]

    assert [c.text for c in text_chunks] == ["Done"]
    assert len(completes) == 1
    assert completes[0].response == "Done"
    assert completes[0].usage == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


@pytest.mark.asyncio
async def test_close_with_no_process_is_a_noop() -> None:
    await GooseExecutor().close()  # must not raise
