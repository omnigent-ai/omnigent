"""Unit tests for DroidExecutor (headless Factory AI Droid ACP / JSON-RPC 2.0).

Covers construction defaults, launch-argv building, tool-call extraction and
permission-outcome mapping from the ACP ``session/request_permission`` shape,
usage mapping, prompt-block folding, the permission → policy/elicitation
round-trip, run_turn streaming (text / reasoning / tool events), interrupt, and
the harness wrap.

The ``initialize`` handshake AND every per-turn ``session/update`` payload are
VERIFIED live against Factory CLI 0.164.0 with a working ``FACTORY_API_KEY`` —
real ``session/prompt`` turns were captured (see docs/droid-spike.md), so the
shapes asserted here (agent_message_chunk / agent_thought_chunk / tool_call /
tool_call_update / session/request_permission options / session/cancel) mirror
the actual wire. Notable verified deviations pinned below: droid ignores the
``--auto`` / ``-m`` / ``-r`` CLI flags in acp mode (model is applied via
``session/set_model``); the ``session/prompt`` result is ``{"stopReason": ...}``
with no usage; permission ``optionId`` values (``proceed_once`` …) differ from
their ``kind`` values.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.droid_executor import DroidExecutor
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)

# ---------------------------------------------------------------------------
# Construction / attribute defaults
# ---------------------------------------------------------------------------


def test_executor_default_attributes() -> None:
    executor = DroidExecutor(droid_path="droid")
    assert executor._droid_path == "droid"
    assert executor._model is None
    # ``--auto`` is passed but IGNORED by acp mode (session stays in normal /
    # Auto-Off); the default is retained only for forward-compat.
    assert executor._auto == "high"
    assert executor._reasoning is None
    assert executor._proc is None
    assert executor._session_id is None
    assert executor._initialized is False
    assert executor._rpc_id == 0
    assert executor.max_context_tokens() is None


def test_executor_custom_model_auto_reasoning() -> None:
    executor = DroidExecutor(model="claude-sonnet-5", auto="medium", reasoning="xhigh")
    assert executor._model == "claude-sonnet-5"
    assert executor._auto == "medium"
    assert executor._reasoning == "xhigh"


def test_executor_cwd_defaults_and_explicit() -> None:
    assert DroidExecutor()._cwd == os.getcwd()
    assert DroidExecutor(cwd="/tmp")._cwd == "/tmp"


def test_capability_flags() -> None:
    executor = DroidExecutor()
    assert executor.supports_streaming() is True
    # Droid runs its own tool loop, so observed tool events must not be
    # re-dispatched by the Session.
    assert executor.handles_tools_internally() is True


# ---------------------------------------------------------------------------
# Launch argv
# ---------------------------------------------------------------------------


def test_launch_argv_defaults() -> None:
    argv = DroidExecutor(cwd="/w")._launch_argv()
    assert argv[:5] == ["exec", "--output-format", "acp", "--cwd", "/w"]
    assert "--auto" in argv and argv[argv.index("--auto") + 1] == "high"
    assert "-m" not in argv  # no model → no -m
    assert "-r" not in argv


def test_launch_argv_with_model_and_reasoning() -> None:
    argv = DroidExecutor(cwd="/w", model="gpt-5.5", auto="low", reasoning="high")._launch_argv()
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert argv[argv.index("-r") + 1] == "high"
    assert argv[argv.index("--auto") + 1] == "low"


# ---------------------------------------------------------------------------
# Tool-call extraction (ACP shapes)
# ---------------------------------------------------------------------------


def test_extract_tool_call_uses_title_and_raw_input() -> None:
    params = {
        "toolCall": {
            "kind": "execute",
            "status": "pending",
            "title": "shell",
            "rawInput": {"command": "echo hi"},
        }
    }
    name, args = DroidExecutor._extract_tool_call(params)
    assert name == "shell"
    assert args == {"command": "echo hi"}


def test_extract_tool_call_prefers_meta_tool_name() -> None:
    params = {
        "toolCall": {
            "kind": "execute",
            "title": "shell · echo hi",
            "rawInput": {"command": "echo hi"},
            "_meta": {"toolName": "Execute"},
        }
    }
    name, args = DroidExecutor._extract_tool_call(params)
    assert name == "Execute"
    assert args == {"command": "echo hi"}


def test_extract_tool_call_prefers_nested_vendor_meta_tool_name() -> None:
    params = {
        "toolCall": {
            "kind": "execute",
            "title": "shell",
            "rawInput": {"command": "echo hi"},
            "_meta": {"factory": {"toolCall": {"toolName": "Execute"}}},
        }
    }
    name, _ = DroidExecutor._extract_tool_call(params)
    assert name == "Execute"


def test_extract_tool_call_falls_back_to_kind_then_tool() -> None:
    assert DroidExecutor._extract_tool_call({"toolCall": {"kind": "execute"}}) == ("execute", {})
    assert DroidExecutor._extract_tool_call({}) == ("tool", {})


def test_extract_tool_call_verified_droid_edit_shape() -> None:
    # VERIFIED live: a droid ``edit`` tool_call has NO ``_meta.toolName`` — the
    # name resolves to the human ``title``; ``rawInput`` holds the args.
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "toolu_bdrk_01QAwPetASkE8K97tSBbAfAG",
        "title": "Create /work/hello.txt",
        "kind": "edit",
        "status": "pending",
        "rawInput": {"file_path": "/work/hello.txt", "content": "hi from droid"},
    }
    name, args = DroidExecutor._extract_tool_call(update)
    assert name == "Create /work/hello.txt"
    assert args == {"file_path": "/work/hello.txt", "content": "hi from droid"}


def test_tool_call_complete_event_read_rawoutput_dict() -> None:
    # VERIFIED live: a ``read`` tool_call_update carries ``rawOutput`` as a
    # ``{"text": ...}`` dict (edit/execute carry none).
    executor = DroidExecutor()
    done = executor._tool_call_complete_event(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "toolu_read_1",
            "title": "Read /work/readme.txt",
            "status": "completed",
            "rawOutput": {"text": "line one\nline two\nline three"},
        }
    )
    assert isinstance(done, ToolCallComplete)
    assert done.status is ToolCallStatus.SUCCESS
    assert done.result == {"text": "line one\nline two\nline three"}


def test_tool_call_complete_event_status_only_edit() -> None:
    # VERIFIED live: a completed ``edit`` update is status-only (no rawOutput).
    executor = DroidExecutor()
    done = executor._tool_call_complete_event(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c", "status": "completed"}
    )
    assert isinstance(done, ToolCallComplete)
    assert done.result is None


# ---------------------------------------------------------------------------
# Permission outcome mapping
# ---------------------------------------------------------------------------

_OPTIONS = [
    {"optionId": "allow_always", "name": "allow_always", "kind": "allow_always"},
    {"optionId": "allow_once", "name": "allow_once", "kind": "allow_once"},
    {"optionId": "reject_once", "name": "reject_once", "kind": "reject_once"},
    {"optionId": "reject_always", "name": "reject_always", "kind": "reject_always"},
]


def test_permission_outcome_allow_prefers_once() -> None:
    out = DroidExecutor._permission_outcome({"options": _OPTIONS}, allow=True)
    assert out == {"outcome": "selected", "optionId": "allow_once"}


def test_permission_outcome_deny_prefers_reject_once() -> None:
    out = DroidExecutor._permission_outcome({"options": _OPTIONS}, allow=False)
    assert out == {"outcome": "selected", "optionId": "reject_once"}


def test_permission_outcome_cancels_when_no_matching_option() -> None:
    only_reject = [{"optionId": "r", "kind": "reject_once"}]
    assert DroidExecutor._permission_outcome({"options": only_reject}, allow=True) == {
        "outcome": "cancelled"
    }
    assert DroidExecutor._permission_outcome({"options": []}, allow=False) == {
        "outcome": "cancelled"
    }


# The exact option set droid 0.164.0 offers for a write/execute permission
# (VERIFIED live) — note the ``optionId`` values differ from ``kind``.
_DROID_REAL_OPTIONS = [
    {"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
    {
        "optionId": "proceed_always",
        "name": "Allow & auto-run low risk commands",
        "kind": "allow_always",
    },
    {"optionId": "cancel", "name": "No, cancel", "kind": "reject_once"},
]


def test_permission_outcome_maps_verified_droid_option_ids() -> None:
    # allow → proceed_once (once-scoped, by kind not optionId)
    assert DroidExecutor._permission_outcome({"options": _DROID_REAL_OPTIONS}, allow=True) == {
        "outcome": "selected",
        "optionId": "proceed_once",
    }
    # deny → the only reject_* option droid offers
    assert DroidExecutor._permission_outcome({"options": _DROID_REAL_OPTIONS}, allow=False) == {
        "outcome": "selected",
        "optionId": "cancel",
    }


# ---------------------------------------------------------------------------
# Tool-call event mapping (session/update tool_call / tool_call_update)
# ---------------------------------------------------------------------------


def test_tool_call_request_event_emits_once_per_id() -> None:
    executor = DroidExecutor()
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_1",
        "title": "shell",
        "rawInput": {"command": "ls"},
    }
    event = executor._tool_call_request_event(update)
    assert isinstance(event, ToolCallRequest)
    assert event.name == "shell"
    assert event.args == {"command": "ls"}
    assert event.metadata["call_id"] == "call_1"
    # Same id again → suppressed (Droid re-sends tool_call as status advances).
    assert executor._tool_call_request_event(update) is None


def test_tool_call_complete_event_success_and_error() -> None:
    executor = DroidExecutor()
    done = executor._tool_call_complete_event(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "title": "shell",
            "status": "completed",
            "rawOutput": {"stdout": "ok"},
        }
    )
    assert isinstance(done, ToolCallComplete)
    assert done.status is ToolCallStatus.SUCCESS
    assert done.metadata["call_id"] == "call_1"

    failed = executor._tool_call_complete_event(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_2",
            "title": "shell",
            "status": "failed",
            "rawOutput": "boom",
        }
    )
    assert isinstance(failed, ToolCallComplete)
    assert failed.status is ToolCallStatus.ERROR
    assert failed.error == "boom"


def test_tool_call_complete_event_none_for_interim_status() -> None:
    executor = DroidExecutor()
    assert (
        executor._tool_call_complete_event(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c", "status": "in_progress"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Usage mapping
# ---------------------------------------------------------------------------


def test_usage_from_result_maps_camel_keys() -> None:
    result = {
        "stopReason": "end_turn",
        "usage": {"totalTokens": 100, "inputTokens": 80, "outputTokens": 20},
    }
    assert DroidExecutor._usage_from_result(result) == {
        "input_tokens": 80,
        "output_tokens": 20,
        "total_tokens": 100,
    }


def test_usage_from_result_maps_snake_keys() -> None:
    # droid's non-acp ``--output-format json`` reports snake_case usage; the
    # mapper accepts it too (forward-compat if acp ever gains usage).
    result = {
        "stopReason": "end_turn",
        "usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
    }
    assert DroidExecutor._usage_from_result(result) == {
        "input_tokens": 80,
        "output_tokens": 20,
        "total_tokens": 100,
    }


def test_usage_from_result_none_when_absent() -> None:
    # VERIFIED live: droid acp ``session/prompt`` result is stopReason-only, so
    # this is the real per-turn path — usage is None.
    assert DroidExecutor._usage_from_result({"stopReason": "end_turn"}) is None
    assert DroidExecutor._usage_from_result({"usage": "nope"}) is None


# ---------------------------------------------------------------------------
# Prompt-block folding
# ---------------------------------------------------------------------------


def test_text_from_blocks_text_and_file() -> None:
    blocks = [
        {"type": "input_text", "text": "do the thing"},
        {"type": "input_file", "filename": "a.txt", "file_data": "data:text/plain;base64,aGk="},
        {
            "type": "input_file",
            "filename": "b.pdf",
            "file_data": "data:application/pdf;base64,AAA=",
        },
    ]
    text = DroidExecutor._text_from_blocks(blocks)
    assert "do the thing" in text
    assert "--- attached file: a.txt ---\nhi\n--- end of a.txt ---" in text
    assert "[attached file: b.pdf]" in text


# ---------------------------------------------------------------------------
# Permission round-trip (agent → client request)
# ---------------------------------------------------------------------------


def _perm_request(req_id: int = 9) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_1",
            "options": _OPTIONS,
            "toolCall": {
                "kind": "execute",
                "status": "pending",
                "title": "shell",
                "rawInput": {"command": "rm -f victim.txt"},
            },
        },
    }


@pytest.mark.asyncio
async def test_respond_to_permission_allows_when_no_gates_wired() -> None:
    executor = DroidExecutor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(_perm_request())
    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}


@pytest.mark.asyncio
async def test_respond_to_permission_denied_by_policy() -> None:
    executor = DroidExecutor()
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_DENY")
    )
    executor._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(_perm_request())

    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    executor._elicitation_handler.assert_not_called()
    phase, data = executor._policy_evaluator.call_args.args
    assert phase == "PHASE_TOOL_CALL"
    assert data == {"name": "shell", "arguments": {"command": "rm -f victim.txt"}}


@pytest.mark.asyncio
async def test_respond_to_permission_elicitation_allow_and_deny() -> None:
    allow_exec = DroidExecutor()
    allow_exec._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent_a: list[dict] = []
    allow_exec._send = AsyncMock(side_effect=lambda m: sent_a.append(m))  # type: ignore[method-assign]
    await allow_exec._respond_to_agent_request(_perm_request())
    assert sent_a[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}
    allow_exec._elicitation_handler.assert_awaited_once_with(
        "shell", {"command": "rm -f victim.txt"}
    )

    deny_exec = DroidExecutor()
    deny_exec._elicitation_handler = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    sent_d: list[dict] = []
    deny_exec._send = AsyncMock(side_effect=lambda m: sent_d.append(m))  # type: ignore[method-assign]
    await deny_exec._respond_to_agent_request(_perm_request())
    assert sent_d[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}


@pytest.mark.asyncio
async def test_respond_to_unknown_method_returns_jsonrpc_error() -> None:
    executor = DroidExecutor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 11, "method": "terminal/create", "params": {}}
    )
    assert sent[0]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Filesystem delegation
# ---------------------------------------------------------------------------


class _FakeOSEnv:
    def __init__(self, read_result: dict | None = None, write_result: dict | None = None) -> None:
        self._read_result = read_result if read_result is not None else {}
        self._write_result = write_result if write_result is not None else {}
        self.read_calls: list[tuple] = []
        self.write_calls: list[tuple] = []
        self.closed = False

    async def read(self, path: str, offset: int = 1, limit: int | None = None) -> dict:
        self.read_calls.append((path, offset, limit))
        return self._read_result

    async def write(self, path: str, content: str) -> dict:
        self.write_calls.append((path, content))
        return self._write_result

    def close(self) -> None:
        self.closed = True


def test_fs_delegation_flag_tracks_os_env() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    assert DroidExecutor()._fs_delegation is False
    assert DroidExecutor(os_env=OSEnvSpec(type="caller_process"))._fs_delegation is True
    assert (
        DroidExecutor(os_env=OSEnvSpec(type="caller_process", fork=True))._fs_delegation is False
    )


@pytest.mark.asyncio
async def test_initialize_advertises_fs_capability_per_delegation() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    init_result = {"result": {"agentCapabilities": {"promptCapabilities": {}}}}

    on = DroidExecutor(os_env=OSEnvSpec(type="caller_process"))
    on._rpc = AsyncMock(return_value=init_result)  # type: ignore[method-assign]
    await on._ensure_initialized()
    assert on._rpc.call_args.args[1]["clientCapabilities"]["fs"] == {
        "readTextFile": True,
        "writeTextFile": True,
    }

    off = DroidExecutor()
    off._rpc = AsyncMock(return_value=init_result)  # type: ignore[method-assign]
    await off._ensure_initialized()
    assert off._rpc.call_args.args[1]["clientCapabilities"]["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }


@pytest.mark.asyncio
async def test_fs_read_returns_content_and_maps_window() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    executor = DroidExecutor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv(read_result={"content": "hi\n", "encoding": "utf-8"})
    executor._os_environment = fake  # type: ignore[assignment]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "fs/read_text_file",
            "params": {"path": "a.txt", "line": 2, "limit": 5},
        }
    )

    assert sent[0]["result"] == {"content": "hi\n"}
    assert fake.read_calls == [("a.txt", 2, 5)]


@pytest.mark.asyncio
async def test_fs_read_missing_file_maps_to_enoent() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    executor = DroidExecutor(os_env=OSEnvSpec(type="caller_process"))
    executor._os_environment = _FakeOSEnv(  # type: ignore[assignment]
        read_result={"error": "[Errno 2] No such file or directory: 'gone.txt'"}
    )
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 5, "method": "fs/read_text_file", "params": {"path": "gone.txt"}}
    )

    assert sent[0]["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_fs_read_binary_file_is_rejected() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    executor = DroidExecutor(os_env=OSEnvSpec(type="caller_process"))
    executor._os_environment = _FakeOSEnv(  # type: ignore[assignment]
        read_result={"content": "AAAA", "encoding": "base64"}
    )
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 6, "method": "fs/read_text_file", "params": {"path": "img.png"}}
    )

    assert sent[0]["error"]["code"] == -32603


@pytest.mark.asyncio
async def test_fs_write_writes_through_os_env() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    executor = DroidExecutor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv(write_result={"path": "out.txt"})
    executor._os_environment = fake  # type: ignore[assignment]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "fs/write_text_file",
            "params": {"path": "out.txt", "content": "abc"},
        }
    )

    assert sent[0]["result"] == {}
    assert fake.write_calls == [("out.txt", "abc")]


@pytest.mark.asyncio
async def test_fs_unsupported_when_delegation_off() -> None:
    executor = DroidExecutor()
    assert executor._fs_delegation is False
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 7, "method": "fs/read_text_file", "params": {"path": "/x"}}
    )

    assert sent[0]["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_close_releases_fs_os_environment() -> None:
    from omnigent.inner.datamodel import OSEnvSpec

    executor = DroidExecutor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv()
    executor._os_environment = fake  # type: ignore[assignment]

    await executor.close()

    assert fake.closed is True
    assert executor._os_environment is None


# ---------------------------------------------------------------------------
# run_turn streaming
# ---------------------------------------------------------------------------


def _ready_executor() -> DroidExecutor:
    executor = DroidExecutor()
    executor._initialized = True
    executor._session_id = "sess_1"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    return executor


@pytest.mark.asyncio
async def test_run_turn_streams_text_and_usage() -> None:
    executor = _ready_executor()
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

    events = [
        e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "be nice")
    ]
    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]

    assert [c.text for c in text_chunks] == ["Done"]
    assert len(completes) == 1
    assert completes[0].response == "Done"
    assert completes[0].usage == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


@pytest.mark.asyncio
async def test_run_turn_emits_reasoning_chunk() -> None:
    executor = _ready_executor()
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
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "thinking..."},
                        }
                    },
                }
            )
            loop.call_soon(
                lambda: executor._pending[req_id].set_result(
                    {"id": req_id, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    assert len(reasoning) == 1
    assert reasoning[0].delta == "thinking..."
    assert reasoning[0].event_type == "reasoning_text"


@pytest.mark.asyncio
async def test_run_turn_emits_tool_call_request_and_complete() -> None:
    executor = _ready_executor()
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
                            "sessionUpdate": "tool_call",
                            "toolCallId": "c1",
                            "title": "shell",
                            "rawInput": {"command": "ls"},
                        }
                    },
                }
            )
            await executor._queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "c1",
                            "title": "shell",
                            "status": "completed",
                            "rawOutput": {"stdout": "file.txt"},
                        }
                    },
                }
            )
            loop.call_soon(
                lambda: executor._pending[req_id].set_result(
                    {"id": req_id, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    requests = [e for e in events if isinstance(e, ToolCallRequest)]
    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(requests) == 1 and requests[0].name == "shell"
    assert requests[0].args == {"command": "ls"}
    assert len(completes) == 1 and completes[0].status is ToolCallStatus.SUCCESS


@pytest.mark.asyncio
async def test_run_turn_real_shape_text_then_stopreason_only() -> None:
    # VERIFIED live: a plain turn streams agent_message_chunk(s), the session's
    # metadata updates (current_mode/config/available_commands), then resolves
    # with a stopReason-only result → usage is None.
    executor = _ready_executor()
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            rid = msg["id"]
            for upd in (
                {"sessionUpdate": "current_mode_update", "currentModeId": "normal"},
                {"sessionUpdate": "config_option_update", "configOptions": []},
                {"sessionUpdate": "available_commands_update", "availableCommands": []},
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "P"}},
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "ONG"},
                },
            ):
                await executor._queue.put(
                    {"jsonrpc": "2.0", "method": "session/update", "params": {"update": upd}}
                )
            loop.call_soon(
                lambda: executor._pending[rid].set_result(
                    {"id": rid, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert text == "PONG"
    assert len(completes) == 1
    assert completes[0].response == "PONG"
    assert completes[0].usage is None


@pytest.mark.asyncio
async def test_run_turn_survives_malformed_update_params() -> None:
    # follow-up (a): a null / non-dict ``params`` or ``update`` must not raise
    # (which would drop the turn); it is coerced to empty and skipped.
    executor = _ready_executor()
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            rid = msg["id"]
            await executor._queue.put(
                {"jsonrpc": "2.0", "method": "session/update", "params": None}
            )
            await executor._queue.put(
                {"jsonrpc": "2.0", "method": "session/update", "params": {"update": None}}
            )
            await executor._queue.put(
                {"jsonrpc": "2.0", "method": "session/update", "params": {"update": "nope"}}
            )
            await executor._queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"update": {"sessionUpdate": "some_future_variant", "x": 1}},
                }
            )
            await executor._queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "ok"},
                        }
                    },
                }
            )
            loop.call_soon(
                lambda: executor._pending[rid].set_result(
                    {"id": rid, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert text == "ok"
    assert len(completes) == 1 and completes[0].response == "ok"


def test_history_prefix_serializes_prior_turns() -> None:
    prior = [
        {"role": "user", "content": "what is 2+2"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "4"}]},
    ]
    out = DroidExecutor._history_prefix(prior)
    assert out.startswith("Conversation so far:")
    assert "user: what is 2+2" in out
    assert "assistant: 4" in out
    assert out.rstrip().endswith("using the conversation above as context.")


@pytest.mark.asyncio
async def test_run_turn_replays_history_on_fresh_session() -> None:
    executor = DroidExecutor()
    executor._initialized = True
    executor._session_id = "sess_fresh"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()
    sent_prompts: list[str] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            sent_prompts.append(msg["params"]["prompt"][0]["text"])
            req_id = msg["id"]
            loop.call_soon(
                lambda: executor._pending[req_id].set_result(
                    {"id": req_id, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    messages = [
        {"role": "user", "content": "remember 42"},
        {"role": "assistant", "content": "ok, 42"},
        {"role": "user", "content": "what number?"},
    ]
    async for _ in executor.run_turn(messages, [], "SYS"):
        pass

    prompt = sent_prompts[0]
    assert prompt.startswith("SYS\n\n")
    assert "Conversation so far:" in prompt
    assert "user: remember 42" in prompt
    assert prompt.rstrip().endswith("user: what number?")


@pytest.mark.asyncio
async def test_run_turn_no_replay_on_continuing_session() -> None:
    executor = DroidExecutor()
    executor._initialized = True
    executor._session_id = "sess_cont"
    executor._system_prompt_sent = True
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()
    sent_prompts: list[str] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            sent_prompts.append(msg["params"]["prompt"][0]["text"])
            req_id = msg["id"]
            loop.call_soon(
                lambda: executor._pending[req_id].set_result(
                    {"id": req_id, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    messages = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "latest"},
    ]
    async for _ in executor.run_turn(messages, [], "SYS"):
        pass

    assert sent_prompts[0] == "latest"


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_session_sends_cancel_and_resets() -> None:
    executor = _ready_executor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    ok = await executor.interrupt_session("any")
    assert ok is True
    assert sent[0]["method"] == "session/cancel"
    assert sent[0]["params"]["sessionId"] == "sess_1"
    assert executor._session_id is None
    assert executor._system_prompt_sent is False


@pytest.mark.asyncio
async def test_interrupt_session_false_without_active_session() -> None:
    executor = DroidExecutor()  # no proc / session
    assert await executor.interrupt_session("any") is False


# ---------------------------------------------------------------------------
# close() / process lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_with_no_process_is_a_noop() -> None:
    await DroidExecutor().close()


@pytest.mark.asyncio
async def test_close_terminates_process() -> None:
    executor = DroidExecutor()
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.returncode = None

    async def fake_wait() -> int:
        return 0

    mock_proc.wait = fake_wait
    executor._proc = mock_proc
    await executor.close()
    mock_proc.terminate.assert_called_once()
    assert executor._proc is None


@pytest.mark.asyncio
async def test_close_kills_when_terminate_raises() -> None:
    executor = DroidExecutor()
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.terminate.side_effect = OSError("gone")
    mock_proc.returncode = None
    executor._proc = mock_proc
    await executor.close()
    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# ACP transport: _rpc / _read_stdout / _read_stderr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_id_increments_monotonically() -> None:
    executor = DroidExecutor()
    sent: list[dict] = []

    async def fake_send(msg: dict) -> None:
        sent.append(msg)
        fut = executor._pending.get(msg["id"])
        if fut and not fut.done():
            fut.set_result({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    executor._send = fake_send  # type: ignore[method-assign]
    await executor._rpc("initialize", {"protocolVersion": 1})
    await executor._rpc("session/new", {"cwd": "/", "mcpServers": []})
    assert [m["id"] for m in sent] == [1, 2]


def _stdout_proc(*lines: str) -> MagicMock:
    mock_stdout = AsyncMock()
    mock_stdout.readline = AsyncMock(
        side_effect=[(line + "\n").encode() for line in lines] + [b""]
    )
    proc = MagicMock()
    proc.stdout = mock_stdout
    return proc


@pytest.mark.asyncio
async def test_read_stdout_resolves_pending_future() -> None:
    executor = DroidExecutor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[42] = fut
    executor._proc = _stdout_proc(json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}))
    await executor._read_stdout()
    assert fut.done() and fut.result()["result"]["ok"] is True


@pytest.mark.asyncio
async def test_read_stdout_puts_notifications_on_queue() -> None:
    executor = DroidExecutor()
    executor._proc = _stdout_proc(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "agent_message_chunk"}},
            }
        )
    )
    await executor._read_stdout()
    assert executor._queue.get_nowait()["method"] == "session/update"


@pytest.mark.asyncio
async def test_read_stdout_colliding_request_is_queued_not_resolved() -> None:
    executor = DroidExecutor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[2] = fut
    executor._proc = _stdout_proc(
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "session/request_permission", "params": {}}
        )
    )
    await executor._read_stdout()
    assert isinstance(fut.exception(), EOFError)
    assert executor._queue.get_nowait()["method"] == "session/request_permission"


@pytest.mark.asyncio
async def test_read_stdout_wakes_pending_futures_on_eof() -> None:
    executor = DroidExecutor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[7] = fut
    executor._proc = _stdout_proc()
    await executor._read_stdout()
    assert isinstance(fut.exception(), EOFError)


@pytest.mark.asyncio
async def test_read_stderr_drains_without_raising() -> None:
    executor = DroidExecutor()
    mock_stderr = AsyncMock()
    mock_stderr.readline = AsyncMock(side_effect=[b"droid: warming up\n", b""])
    proc = MagicMock()
    proc.stderr = mock_stderr
    executor._proc = proc
    await executor._read_stderr()


# ---------------------------------------------------------------------------
# Handshake / session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_initialized_learns_image_capability() -> None:
    executor = DroidExecutor()
    executor._rpc = AsyncMock(  # type: ignore[method-assign]
        return_value={"result": {"agentCapabilities": {"promptCapabilities": {"image": True}}}}
    )
    await executor._ensure_initialized()
    assert executor._initialized is True
    assert executor._image_supported is True
    executor._rpc.reset_mock()
    await executor._ensure_initialized()
    executor._rpc.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_session_uses_server_assigned_id_and_caches() -> None:
    executor = DroidExecutor()
    executor._rpc = AsyncMock(return_value={"result": {"sessionId": "sess_7"}})  # type: ignore[method-assign]
    sid = await executor._ensure_session()
    assert sid == "sess_7" and executor._session_id == "sess_7"
    executor._rpc.reset_mock()
    assert await executor._ensure_session() == "sess_7"
    executor._rpc.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_session_raises_on_missing_session_id() -> None:
    executor = DroidExecutor()
    executor._rpc = AsyncMock(return_value={"result": {}})  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="session/new"):
        await executor._ensure_session()


@pytest.mark.asyncio
async def test_ensure_session_applies_model_via_set_model() -> None:
    # VERIFIED live: ``-m`` is ignored in acp mode, so the configured model is
    # applied over the wire with ``session/set_model {sessionId, modelId}``.
    executor = DroidExecutor(model="claude-opus-4-8")
    calls: list[tuple] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        if method == "session/new":
            return {"result": {"sessionId": "sess_m"}}
        return {"result": {}}

    executor._rpc = fake_rpc  # type: ignore[method-assign]
    sid = await executor._ensure_session()
    assert sid == "sess_m"
    assert ("session/new", {"cwd": executor._cwd, "mcpServers": []}) in calls
    assert ("session/set_model", {"sessionId": "sess_m", "modelId": "claude-opus-4-8"}) in calls


@pytest.mark.asyncio
async def test_ensure_session_no_set_model_without_model() -> None:
    executor = DroidExecutor()  # no model
    methods: list[str] = []

    async def fake_rpc(method, params, timeout=30.0):
        methods.append(method)
        return {"result": {"sessionId": "sess_n"}}

    executor._rpc = fake_rpc  # type: ignore[method-assign]
    await executor._ensure_session()
    assert "session/set_model" not in methods


@pytest.mark.asyncio
async def test_apply_model_swallows_rejection() -> None:
    # A rejected set_model must not fail the turn — session keeps the default.
    executor = DroidExecutor(model="bogus-model")
    executor._rpc = AsyncMock(  # type: ignore[method-assign]
        return_value={"error": {"message": "unknown model"}}
    )
    await executor._apply_model("sess_x")  # no raise


# ---------------------------------------------------------------------------
# Spawn / sandbox
# ---------------------------------------------------------------------------


def test_sandbox_launch_path_bare_when_no_sandbox() -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    assert DroidExecutor(droid_path="droid")._sandbox_launch_path(()) == "droid"
    disabled = DroidExecutor(
        droid_path="/usr/bin/droid", os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert disabled._sandbox_launch_path(("PATH",)) == "/usr/bin/droid"


def test_sandbox_launch_path_wraps_active_policy(monkeypatch, tmp_path) -> None:
    from omnigent.inner import sandbox as sandbox_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.sandbox import SandboxPolicy

    captured: dict = {}

    def _fake_resolve(_os_env, cwd) -> SandboxPolicy:
        return SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=[cwd.resolve(strict=False)],
            write_roots=[cwd.resolve(strict=False)],
            write_files=[],
            allow_network=True,
        )

    def _fake_launcher(target: str, sandbox: SandboxPolicy) -> str:
        captured["target"] = target
        captured["policy"] = sandbox
        return "/fake/launcher"

    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", _fake_resolve)
    monkeypatch.setattr(sandbox_mod, "create_exec_launcher", _fake_launcher)

    executor = DroidExecutor(
        cwd=str(tmp_path),
        droid_path="/usr/bin/droid",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
    )
    path = executor._sandbox_launch_path(("PATH", "FACTORY_API_KEY"))

    assert path == "/fake/launcher"
    assert captured["target"] == "/usr/bin/droid"
    policy = captured["policy"]
    assert any(str(p).endswith(".factory") for p in policy.write_roots)
    assert policy.spawn_env_allowlist is not None
    assert "PATH" in policy.spawn_env_allowlist
    assert "FACTORY_API_KEY" in policy.spawn_env_allowlist


def test_sandbox_launch_path_falls_back_when_backend_unavailable(monkeypatch, tmp_path) -> None:
    from omnigent.inner import sandbox as sandbox_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    def _boom(_os_env, _cwd) -> None:
        raise NotImplementedError("no bwrap here")

    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", _boom)
    executor = DroidExecutor(
        cwd=str(tmp_path),
        droid_path="/usr/bin/droid",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
    )
    assert executor._sandbox_launch_path(("PATH",)) == "/usr/bin/droid"


@pytest.mark.asyncio
async def test_start_process_resets_handshake_state(monkeypatch) -> None:
    executor = DroidExecutor(droid_path="droid", model="gpt-5.5", auto="high")
    executor._initialized = True
    executor._image_supported = True

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _stdout_proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await executor._start_process()
    try:
        assert executor._initialized is False
        assert executor._image_supported is False
        argv = captured["argv"]
        assert argv[1:4] == ("exec", "--output-format", "acp")
        assert "-m" in argv and "gpt-5.5" in argv
    finally:
        await executor.close()


# ---------------------------------------------------------------------------
# run_turn error + usage paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_boot_failure_yields_error(monkeypatch) -> None:
    executor = DroidExecutor()

    async def boom() -> None:
        raise FileNotFoundError("droid not found")

    monkeypatch.setattr(executor, "_start_process", boom)
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].retryable is False


@pytest.mark.asyncio
async def test_run_turn_acp_error_resets_session() -> None:
    executor = DroidExecutor()
    executor._initialized = True
    executor._session_id = "stale"
    executor._system_prompt_sent = True
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            rid = msg["id"]
            loop.call_soon(
                lambda: executor._pending[rid].set_result(
                    {"id": rid, "error": {"message": "Session not found: stale"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "sys")]
    assert any(isinstance(e, ExecutorError) and e.retryable for e in events)
    assert executor._session_id is None
    assert executor._system_prompt_sent is False


@pytest.mark.asyncio
async def test_run_turn_tracks_context_window_from_usage_update() -> None:
    executor = _ready_executor()
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            rid = msg["id"]
            await executor._queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {"sessionUpdate": "usage_update", "used": 5, "size": 200000}
                    },
                }
            )
            loop.call_soon(
                lambda: executor._pending[rid].set_result(
                    {"id": rid, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    assert executor.max_context_tokens() == 200000


# ---------------------------------------------------------------------------
# Attachment / image helpers
# ---------------------------------------------------------------------------


def test_inline_text_file_data_variants() -> None:
    from omnigent.inner.droid_executor import _inline_text_file_data

    assert _inline_text_file_data("plain text") == "plain text"
    assert _inline_text_file_data("") == ""
    assert _inline_text_file_data(123) == ""
    assert _inline_text_file_data("data:image/png;base64,AAA=") == ""
    assert _inline_text_file_data("data:text/plain;base64,aGk=") == "hi"


def test_image_blocks_from_content_parses_and_skips() -> None:
    blocks = [
        {"type": "input_text", "text": "ignore"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        {"type": "input_image", "image_url": "https://x/y.png"},
    ]
    assert DroidExecutor._image_blocks_from_content(blocks) == [
        {"type": "image", "mimeType": "image/png", "data": "AAAB"}
    ]
    assert DroidExecutor._image_blocks_from_content("not a list") == []


@pytest.mark.asyncio
async def test_run_turn_forwards_image_block_when_supported() -> None:
    executor = _ready_executor()
    executor._image_supported = True
    loop = asyncio.get_event_loop()
    prompts: list = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            prompts.append(msg["params"]["prompt"])
            rid = msg["id"]
            loop.call_soon(
                lambda: executor._pending[rid].set_result(
                    {"id": rid, "result": {"stopReason": "end_turn"}}
                )
            )

    executor._send = fake_send  # type: ignore[method-assign]
    content = [
        {"type": "input_text", "text": "look at this"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
    ]
    [e async for e in executor.run_turn([{"role": "user", "content": content}], [], "")]
    prompt = prompts[0]
    assert any(b.get("type") == "image" for b in prompt)
    assert any(b.get("type") == "text" for b in prompt)


# ---------------------------------------------------------------------------
# _decide_permission branch coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_permission_no_gates_allows() -> None:
    assert await DroidExecutor()._decide_permission(_perm_request()["params"]) is True


@pytest.mark.asyncio
async def test_decide_permission_policy_ask_without_handler_denies() -> None:
    executor = DroidExecutor()
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_ASK")
    )
    assert await executor._decide_permission(_perm_request()["params"]) is False


@pytest.mark.asyncio
async def test_decide_permission_policy_exception_falls_through_to_elicit() -> None:
    executor = DroidExecutor()

    async def _boom(*_a) -> object:
        raise RuntimeError("policy backend down")

    executor._policy_evaluator = _boom  # type: ignore[attr-defined]
    executor._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    assert await executor._decide_permission(_perm_request()["params"]) is True


@pytest.mark.asyncio
async def test_respond_to_agent_request_exception_yields_error_reply() -> None:
    executor = DroidExecutor()

    async def _boom(_params) -> bool:
        raise RuntimeError("kaboom")

    executor._decide_permission = _boom  # type: ignore[method-assign]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(_perm_request())
    assert sent[0]["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# Harness wrap (droid_harness)
# ---------------------------------------------------------------------------


def test_resolve_os_env_default(monkeypatch) -> None:
    from omnigent.inner import droid_harness

    monkeypatch.delenv("HARNESS_DROID_OS_ENV", raising=False)
    spec = droid_harness._resolve_os_env()
    assert spec.type == "caller_process"
    assert spec.sandbox is not None and spec.sandbox.type == "none"


def test_resolve_os_env_from_json(monkeypatch) -> None:
    from omnigent.inner import droid_harness

    monkeypatch.setenv(
        "HARNESS_DROID_OS_ENV",
        json.dumps(
            {
                "type": "caller_process",
                "cwd": "/w",
                "sandbox": {"type": "linux_bwrap"},
                "fork": True,
            }
        ),
    )
    spec = droid_harness._resolve_os_env()
    assert spec.cwd == "/w"
    assert spec.sandbox is not None and spec.sandbox.type == "linux_bwrap"
    assert spec.fork is True


def test_resolve_os_env_malformed_json_falls_back(monkeypatch) -> None:
    from omnigent.inner import droid_harness

    monkeypatch.setenv("HARNESS_DROID_OS_ENV", "{not valid json")
    spec = droid_harness._resolve_os_env()
    assert spec.type == "caller_process"
    assert spec.sandbox is not None and spec.sandbox.type == "none"


def test_build_droid_executor_reads_env(monkeypatch) -> None:
    from omnigent.inner import droid_harness

    monkeypatch.setenv("HARNESS_DROID_MODEL", "gpt-5.5")
    monkeypatch.setenv("HARNESS_DROID_CWD", "/work")
    monkeypatch.setenv("HARNESS_DROID_PATH", "/bin/droid")
    monkeypatch.setenv("HARNESS_DROID_AUTO", "medium")
    monkeypatch.setenv("HARNESS_DROID_REASONING", "xhigh")
    monkeypatch.delenv("HARNESS_DROID_OS_ENV", raising=False)
    ex = droid_harness._build_droid_executor()
    assert ex._model == "gpt-5.5"
    assert ex._cwd == "/work"
    assert ex._droid_path == "/bin/droid"
    assert ex._auto == "medium"
    assert ex._reasoning == "xhigh"


def test_build_droid_executor_defaults(monkeypatch) -> None:
    from omnigent.inner import droid_harness

    for var in (
        "HARNESS_DROID_MODEL",
        "HARNESS_DROID_CWD",
        "HARNESS_DROID_PATH",
        "HARNESS_DROID_AUTO",
        "HARNESS_DROID_REASONING",
        "OMNIGENT_RUNNER_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)
    ex = droid_harness._build_droid_executor()
    assert ex._model is None
    assert ex._droid_path == "droid"
    assert ex._auto == "high"
    assert ex._reasoning is None


def test_create_app_returns_fastapi() -> None:
    from fastapi import FastAPI

    from omnigent.inner import droid_harness

    assert isinstance(droid_harness.create_app(), FastAPI)
