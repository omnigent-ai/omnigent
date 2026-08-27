"""Unit tests for the Goose harness (``goose acp`` over ACP).

Goose is :class:`~omnigent.inner.acp_executor.AcpExecutor` plus a Goose-shaped
config, so this file covers what *Goose* adds: its config contract, its
``_meta`` tool-name mapping, its sandbox roots, and the harness wrap, plus
integration-level checks that a real Goose session drives the inherited wire
(permission round-trip, run_turn streaming, usage). The generic protocol surface
is owned by ``tests/inner/test_acp_executor.py``; duplicating it here is what let
the two implementations drift. Protocol shapes match a verified Goose 1.38
``goose acp`` session.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.acp_executor import AcpExecutor
from omnigent.inner.executor import (
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.inner.goose import GOOSE_ACP_EXTENSION, build_goose_executor

# ---------------------------------------------------------------------------
# Construction / attribute defaults
# ---------------------------------------------------------------------------


def test_executor_starts_unspawned_with_no_session() -> None:
    executor = build_goose_executor(goose_path="goose")
    assert executor._proc is None
    assert executor._session_id is None
    assert executor._initialized is False
    assert executor._rpc_id == 0
    assert executor.max_context_tokens() is None


def test_executor_cwd_defaults_and_explicit() -> None:
    assert build_goose_executor()._cwd == os.getcwd()
    assert build_goose_executor(cwd="/tmp")._cwd == "/tmp"


def test_provider_env_only_sets_what_was_named() -> None:
    """An unnamed provider/model leaves Goose's own configuration untouched."""
    from omnigent.inner.goose import goose_provider_env

    assert goose_provider_env(None, None) == {}
    assert goose_provider_env("anthropic", None) == {"GOOSE_PROVIDER": "anthropic"}
    assert goose_provider_env(None, "claude-x") == {"GOOSE_MODEL": "claude-x"}


def test_spawn_env_forces_an_approval_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess always gets an asking ``GOOSE_MODE``, overriding the host's.

    Goose defaults to Auto, which sends no ``session/request_permission``, so
    without this the TOOL_CALL policy gate in :meth:`_decide_permission` never
    runs and every tool executes ungated. An ambient ``GOOSE_MODE=auto`` must
    not be able to switch enforcement back off.
    """
    monkeypatch.setenv("GOOSE_MODE", "auto")
    assert build_goose_executor()._build_spawn_env()["GOOSE_MODE"] == "smart_approve"


def test_goose_agent_config_carries_every_goose_quirk() -> None:
    """The Goose config is the whole vendor contract; pin it as one shape.

    Each field here is a Goose behavior the generic ACP executor cannot infer,
    and each one silently breaks something if dropped: no ``acp`` subcommand and
    it speaks the wrong protocol; no ``--with-builtin`` and Goose has no tools;
    no ``GOOSE_MODE`` and the TOOL_CALL policy gate never fires; no ``GOOSE_``
    prefix and Goose cannot read its own configuration; no config/state roots and
    it cannot start inside a sandbox.
    """
    from omnigent.inner.goose import goose_agent_config

    config = goose_agent_config(
        goose_path="/usr/bin/goose", provider="anthropic", model="claude-x"
    )

    assert shlex.split(config.command) == [
        "/usr/bin/goose",
        "acp",
        "--with-builtin",
        "developer",
    ]
    assert config.name == "Goose"
    # Goose assigns the session id itself and has no session/new model field.
    assert config.session_id_mode == "server"
    assert config.send_model_in_session_new is False
    assert config.env_allow_prefixes == ("GOOSE_",)
    assert dict(config.spawn_env) == {
        "GOOSE_MODE": "smart_approve",
        "GOOSE_PROVIDER": "anthropic",
        "GOOSE_MODEL": "claude-x",
    }
    assert config.sandbox_write_roots == (
        Path.home() / ".config" / "goose",
        Path.home() / ".local" / "share" / "goose",
    )
    assert config.sandbox_read_roots == (Path.home() / ".config" / "goose",)


def test_goose_agent_config_builtins_override() -> None:
    """``HARNESS_GOOSE_BUILTINS`` reaches the argv as repeated flags."""
    from omnigent.inner.goose import goose_agent_config

    config = goose_agent_config(builtins=("developer", "computercontroller"))
    assert shlex.split(config.command) == [
        "goose",
        "acp",
        "--with-builtin",
        "developer",
        "--with-builtin",
        "computercontroller",
    ]


def test_goose_composes_onto_the_generic_acp_wire() -> None:
    """Goose IS the generic ACP executor: a config plus an extension, no subclass.

    Guards the pattern's point. A Goose-only subclass would mean the protocol code
    had been forked again, so every ACP fix would need applying twice; and a
    subclass is not liftable the way a package plus an extension is.
    """
    executor = build_goose_executor()

    assert type(executor) is AcpExecutor, "Goose must not subclass the wire"
    assert executor._extension is GOOSE_ACP_EXTENSION
    assert executor._extension.name == "goose"
    # Streaming + its own tool loop come from the base and match the declared
    # capability row, which goose-native's executor contradicted.
    assert executor.supports_streaming() is True
    assert executor.handles_tools_internally() is True


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
    name, args = build_goose_executor()._extract_tool_call(params)
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
    name, args = build_goose_executor()._extract_tool_call(params)
    assert name == "developer__shell"
    assert args == {"command": "echo hi"}


def test_extract_tool_call_falls_back_to_kind_then_tool() -> None:
    executor = build_goose_executor()
    assert executor._extract_tool_call({"toolCall": {"kind": "execute"}}) == ("execute", {})
    assert executor._extract_tool_call({}) == ("tool", {})


# ---------------------------------------------------------------------------
# Usage mapping
# ---------------------------------------------------------------------------


def test_usage_from_result_maps_goose_keys() -> None:
    result = {
        "stopReason": "end_turn",
        "usage": {"totalTokens": 100, "inputTokens": 80, "outputTokens": 20},
    }
    assert AcpExecutor._usage_from_result(result) == {
        "input_tokens": 80,
        "output_tokens": 20,
        "total_tokens": 100,
    }


def test_usage_from_result_none_when_absent() -> None:
    assert AcpExecutor._usage_from_result({"stopReason": "end_turn"}) is None
    assert AcpExecutor._usage_from_result({"usage": "nope"}) is None


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
    text = AcpExecutor._text_from_blocks(blocks)
    assert "do the thing" in text
    assert "--- attached file: a.txt ---\nhi\n--- end of a.txt ---" in text
    assert "[attached file: b.pdf]" in text  # binary → marker, not inlined


# ---------------------------------------------------------------------------
# Permission round-trip (agent → client request)
# ---------------------------------------------------------------------------


# The four option kinds Goose offers on a permission request.
_GOOSE_OPTIONS = [
    {"optionId": "allow_always", "name": "allow_always", "kind": "allow_always"},
    {"optionId": "allow_once", "name": "allow_once", "kind": "allow_once"},
    {"optionId": "reject_once", "name": "reject_once", "kind": "reject_once"},
    {"optionId": "reject_always", "name": "reject_always", "kind": "reject_always"},
]


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
    executor = build_goose_executor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(_perm_request())
    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}


@pytest.mark.asyncio
async def test_respond_to_permission_denied_by_policy() -> None:
    executor = build_goose_executor()
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
    allow_exec = build_goose_executor()
    allow_exec._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent_a: list[dict] = []
    allow_exec._send = AsyncMock(side_effect=lambda m: sent_a.append(m))  # type: ignore[method-assign]
    await allow_exec._respond_to_agent_request(_perm_request())
    assert sent_a[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}
    allow_exec._elicitation_handler.assert_awaited_once_with(
        "shell", {"command": "rm -f victim.txt"}
    )

    # Deny → reject_once.
    deny_exec = build_goose_executor()
    deny_exec._elicitation_handler = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    sent_d: list[dict] = []
    deny_exec._send = AsyncMock(side_effect=lambda m: sent_d.append(m))  # type: ignore[method-assign]
    await deny_exec._respond_to_agent_request(_perm_request())
    assert sent_d[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}


@pytest.mark.asyncio
async def test_respond_to_unknown_method_returns_jsonrpc_error() -> None:
    executor = build_goose_executor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 11, "method": "terminal/create", "params": {}}
    )
    assert sent[0]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Filesystem delegation (fs/read_text_file, fs/write_text_file)
# ---------------------------------------------------------------------------


class _FakeOSEnv:
    """Minimal OSEnvironment stand-in capturing read/write calls."""

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
    """Delegation is on with an os_env, off without one or for a fork env."""
    from omnigent.inner.datamodel import OSEnvSpec

    assert build_goose_executor()._fs_delegation is False
    assert build_goose_executor(os_env=OSEnvSpec(type="caller_process"))._fs_delegation is True
    assert (
        build_goose_executor(os_env=OSEnvSpec(type="caller_process", fork=True))._fs_delegation
        is False
    )


@pytest.mark.asyncio
async def test_initialize_advertises_fs_capability_per_delegation() -> None:
    """initialize advertises clientCapabilities.fs matching the delegation flag."""
    from omnigent.inner.datamodel import OSEnvSpec

    init_result = {"result": {"agentCapabilities": {"promptCapabilities": {}}}}

    on = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    on._rpc = AsyncMock(return_value=init_result)  # type: ignore[method-assign]
    await on._ensure_initialized()
    assert on._rpc.call_args.args[1]["clientCapabilities"]["fs"] == {
        "readTextFile": True,
        "writeTextFile": True,
    }

    off = build_goose_executor()
    off._rpc = AsyncMock(return_value=init_result)  # type: ignore[method-assign]
    await off._ensure_initialized()
    assert off._rpc.call_args.args[1]["clientCapabilities"]["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }


@pytest.mark.asyncio
async def test_fs_read_returns_content_and_maps_window() -> None:
    """fs/read_text_file reads through the OSEnvironment; line/limit → offset/limit."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
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
    """A 'no such file' read error maps to the ENOENT code (-32002)."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
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
    """A non-utf-8 (binary) file is refused rather than returned as bytes."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
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
    """fs/write_text_file writes via the OSEnvironment and returns an empty result."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
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
    """Without an os_env, fs/* is method-not-found (delegation not advertised)."""
    executor = build_goose_executor()  # no os_env
    assert executor._fs_delegation is False
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 7, "method": "fs/read_text_file", "params": {"path": "/x"}}
    )

    assert sent[0]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# fs delegation — event recording + TOOL_RESULT-phase content policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_read_records_tool_call_events() -> None:
    """A delegated read buffers a paired ToolCallRequest + ToolCallComplete."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    executor._os_environment = _FakeOSEnv(  # type: ignore[assignment]
        read_result={"content": "hello", "encoding": "utf-8"}
    )
    executor._send = AsyncMock()  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 3, "method": "fs/read_text_file", "params": {"path": "a.txt"}}
    )

    req, done = executor._fs_events
    assert isinstance(req, ToolCallRequest)
    assert req.name == "read_text_file"
    assert req.args == {"path": "a.txt"}
    assert isinstance(done, ToolCallComplete)
    assert done.status is ToolCallStatus.SUCCESS
    assert done.result == {"content": "hello"}
    assert req.metadata["call_id"] == done.metadata["call_id"]


@pytest.mark.asyncio
async def test_fs_write_records_tool_call_events() -> None:
    """A delegated write buffers a paired ToolCallRequest + ToolCallComplete."""
    from omnigent.inner.datamodel import OSEnvSpec

    write_result = {"bytes": 3}
    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    executor._os_environment = _FakeOSEnv(write_result=write_result)  # type: ignore[assignment]
    executor._send = AsyncMock()  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "fs/write_text_file",
            "params": {"path": "out.txt", "content": "abc"},
        }
    )

    req, done = executor._fs_events
    assert req.name == "write_text_file"
    assert req.args == {"path": "out.txt", "content": "abc"}
    assert done.status is ToolCallStatus.SUCCESS
    assert done.result == write_result
    assert req.metadata["call_id"] == done.metadata["call_id"]


@pytest.mark.asyncio
async def test_fs_read_blocked_by_call_policy() -> None:
    """A TOOL_CALL-phase DENY refuses the read by path before it runs."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv(read_result={"content": "secret", "encoding": "utf-8"})
    executor._os_environment = fake  # type: ignore[assignment]
    policy = AsyncMock(return_value=MagicMock(action="POLICY_ACTION_DENY"))
    executor._policy_evaluator = policy  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 5, "method": "fs/read_text_file", "params": {"path": "a.txt"}}
    )

    assert policy.await_args.args == (
        "PHASE_TOOL_CALL",
        {"name": "read_text_file", "arguments": {"path": "a.txt"}},
    )
    assert fake.read_calls == []  # denied before the read ran
    assert "error" in sent[0]
    assert executor._fs_events[-1].status is ToolCallStatus.BLOCKED


@pytest.mark.asyncio
async def test_fs_read_blocked_by_result_policy() -> None:
    """A TOOL_RESULT-phase DENY on read content refuses delivery and records BLOCKED."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    executor._os_environment = _FakeOSEnv(  # type: ignore[assignment]
        read_result={"content": "secret", "encoding": "utf-8"}
    )

    async def _policy(phase: str, data: dict) -> MagicMock:  # type: ignore[type-arg]
        # Allow the path at the call phase; deny the content at the result phase.
        action = "POLICY_ACTION_DENY" if phase == "PHASE_TOOL_RESULT" else "POLICY_ACTION_ALLOW"
        return MagicMock(action=action)

    policy = AsyncMock(side_effect=_policy)
    executor._policy_evaluator = policy  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 5, "method": "fs/read_text_file", "params": {"path": "a.txt"}}
    )

    assert policy.await_args.args == ("PHASE_TOOL_RESULT", {"result": "secret"})
    assert "error" in sent[0]
    assert executor._fs_events[-1].status is ToolCallStatus.BLOCKED


@pytest.mark.asyncio
async def test_fs_write_blocked_by_result_policy() -> None:
    """A TOOL_RESULT-phase DENY on the write result records BLOCKED."""
    from omnigent.inner.datamodel import OSEnvSpec

    write_result = {"path": "out.txt", "bytes": 3, "marker": "actual"}
    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    executor._os_environment = _FakeOSEnv(write_result=write_result)  # type: ignore[attr-defined]

    async def _policy(phase: str, data: dict) -> MagicMock:  # type: ignore[type-arg]
        action = "POLICY_ACTION_DENY" if phase == "PHASE_TOOL_RESULT" else "POLICY_ACTION_ALLOW"
        return MagicMock(action=action)

    policy = AsyncMock(side_effect=_policy)
    executor._policy_evaluator = policy  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "fs/write_text_file",
            "params": {"path": "out.txt", "content": "abc"},
        }
    )

    assert policy.await_args_list[-1].args == ("PHASE_TOOL_RESULT", {"result": write_result})
    assert "error" in sent[0]
    assert executor._fs_events[-1].status is ToolCallStatus.BLOCKED


@pytest.mark.asyncio
async def test_fs_write_blocked_by_call_policy_prevents_write() -> None:
    """A TOOL_CALL-phase DENY (path + content) prevents the write."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv(write_result={})
    executor._os_environment = fake  # type: ignore[assignment]
    policy = AsyncMock(return_value=MagicMock(action="POLICY_ACTION_DENY"))
    executor._policy_evaluator = policy  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "fs/write_text_file",
            "params": {"path": "out.txt", "content": "secret"},
        }
    )

    assert policy.await_args.args == (
        "PHASE_TOOL_CALL",
        {"name": "write_text_file", "arguments": {"path": "out.txt", "content": "secret"}},
    )
    assert fake.write_calls == []  # the write never happened
    assert "error" in sent[0]
    assert executor._fs_events[-1].status is ToolCallStatus.BLOCKED


@pytest.mark.asyncio
async def test_fs_write_call_policy_fails_closed() -> None:
    """A TOOL_CALL-phase eval error prevents the write (fail closed for side effects)."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv(write_result={})
    executor._os_environment = fake  # type: ignore[assignment]
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("policy server unreachable")
    )
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "fs/write_text_file",
            "params": {"path": "out.txt", "content": "secret"},
        }
    )

    assert fake.write_calls == []  # an eval failure blocks the write, it does not allow it
    assert "error" in sent[0]
    assert executor._fs_events[-1].status is ToolCallStatus.BLOCKED


@pytest.mark.asyncio
async def test_fs_write_call_policy_ask_blocks() -> None:
    """A TOOL_CALL-phase ASK blocks the write (delegated fs has no elicitation path)."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv(write_result={})
    executor._os_environment = fake  # type: ignore[assignment]
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_ASK")
    )
    executor._send = AsyncMock()  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "fs/write_text_file",
            "params": {"path": "out.txt", "content": "secret"},
        }
    )

    assert fake.write_calls == []  # ASK with no elicitation blocks the write
    assert executor._fs_events[-1].status is ToolCallStatus.BLOCKED


@pytest.mark.asyncio
async def test_run_turn_surfaces_delegated_fs_ops() -> None:
    """run_turn drains recorded fs ops onto the turn stream as ToolCall events."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    executor._initialized = True
    executor._session_id = "s"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    executor._os_environment = _FakeOSEnv(  # type: ignore[assignment]
        read_result={"content": "hi", "encoding": "utf-8"}
    )
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            req_id = msg["id"]
            await executor._queue.put(
                {
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "fs/read_text_file",
                    "params": {"path": "a.txt"},
                }
            )

            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
                    )

            loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]

    events = [e async for e in executor.run_turn([{"role": "user", "content": "go"}], [], "")]

    assert [e.name for e in events if isinstance(e, ToolCallRequest)] == ["read_text_file"]
    assert [e.status for e in events if isinstance(e, ToolCallComplete)] == [
        ToolCallStatus.SUCCESS
    ]


@pytest.mark.asyncio
async def test_run_turn_records_stale_fs_op_from_prior_turn() -> None:
    """A stale server fs request answered at turn start is still audited, not dropped."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    executor._initialized = True
    executor._session_id = "s"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    executor._os_environment = _FakeOSEnv(  # type: ignore[assignment]
        read_result={"content": "hi", "encoding": "utf-8"}
    )
    loop = asyncio.get_event_loop()

    # A leftover fs request from a prior turn, sitting on the queue before this
    # turn starts. It runs real I/O when answered, so its audit events must survive.
    await executor._queue.put(
        {"jsonrpc": "2.0", "id": 77, "method": "fs/read_text_file", "params": {"path": "a.txt"}}
    )

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            req_id = msg["id"]

            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
                    )

            loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]

    events = [e async for e in executor.run_turn([{"role": "user", "content": "go"}], [], "")]

    assert [e.name for e in events if isinstance(e, ToolCallRequest)] == ["read_text_file"]
    assert [e.status for e in events if isinstance(e, ToolCallComplete)] == [
        ToolCallStatus.SUCCESS
    ]


@pytest.mark.asyncio
async def test_close_releases_fs_os_environment() -> None:
    """close() tears down a lazily-created fs-delegation OSEnvironment."""
    from omnigent.inner.datamodel import OSEnvSpec

    executor = build_goose_executor(os_env=OSEnvSpec(type="caller_process"))
    fake = _FakeOSEnv()
    executor._os_environment = fake  # type: ignore[assignment]

    await executor.close()

    assert fake.closed is True
    assert executor._os_environment is None


# ---------------------------------------------------------------------------
# run_turn streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_streams_text_and_usage() -> None:
    """run_turn yields TextChunk for agent_message_chunk and a TurnComplete with
    usage parsed from the final session/prompt result."""
    executor = build_goose_executor()
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

    events = [
        e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "be nice")
    ]
    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]

    assert [c.text for c in text_chunks] == ["Done"]
    assert len(completes) == 1
    assert completes[0].response == "Done"
    assert completes[0].usage == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


def test_history_prefix_serializes_prior_turns() -> None:
    """_history_prefix renders prior turns as labeled role: content lines."""
    prior = [
        {"role": "user", "content": "what is 2+2"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "4"}]},
    ]
    out = AcpExecutor._history_prefix(prior)
    assert out.startswith("Conversation so far:")
    assert "user: what is 2+2" in out
    assert "assistant: 4" in out
    assert out.rstrip().endswith("using the conversation above as context.")


@pytest.mark.asyncio
async def test_run_turn_replays_history_on_fresh_session() -> None:
    """A fresh Goose session folds prior turns into the prompt (e.g. /model respawn).

    Goose normally only sees the latest user turn; on a brand-new subprocess
    that would drop everything before the switch, so the first turn replays
    the transcript to keep context.
    """
    executor = build_goose_executor()
    executor._initialized = True
    executor._session_id = "20260623_fresh"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()

    sent_prompts: list[str] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            sent_prompts.append(msg["params"]["prompt"][0]["text"])
            req_id = msg["id"]

            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
                    )

            loop.call_soon(_resolve)

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
    assert "assistant: ok, 42" in prompt
    assert prompt.rstrip().endswith("user: what number?")


@pytest.mark.asyncio
async def test_run_turn_no_replay_on_continuing_session() -> None:
    """A continuing Goose session sends only the latest turn (it retains context)."""
    executor = build_goose_executor()
    executor._initialized = True
    executor._session_id = "20260623_cont"
    executor._system_prompt_sent = True  # not a fresh session
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()

    sent_prompts: list[str] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            sent_prompts.append(msg["params"]["prompt"][0]["text"])
            req_id = msg["id"]

            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
                    )

            loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]

    messages = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "latest"},
    ]
    async for _ in executor.run_turn(messages, [], "SYS"):
        pass

    assert sent_prompts[0] == "latest"


@pytest.mark.asyncio
async def test_close_with_no_process_is_a_noop() -> None:
    await build_goose_executor().close()  # must not raise


# ---------------------------------------------------------------------------
# close() / process lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_terminates_process() -> None:
    executor = build_goose_executor()
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
    executor = build_goose_executor()
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.terminate.side_effect = OSError("gone")
    mock_proc.returncode = None
    executor._proc = mock_proc
    await executor.close()  # must not propagate
    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# ACP transport: _rpc / _read_stdout / _read_stderr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_id_increments_monotonically() -> None:
    executor = build_goose_executor()
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
    """A fake proc whose stdout yields *lines* then EOF."""
    mock_stdout = AsyncMock()
    mock_stdout.readline = AsyncMock(
        side_effect=[(line + "\n").encode() for line in lines] + [b""]
    )
    proc = MagicMock()
    proc.stdout = mock_stdout
    return proc


@pytest.mark.asyncio
async def test_read_stdout_resolves_pending_future() -> None:
    executor = build_goose_executor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[42] = fut
    executor._proc = _stdout_proc(json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}))
    await executor._read_stdout()
    assert fut.done() and fut.result()["result"]["ok"] is True


@pytest.mark.asyncio
async def test_read_stdout_puts_notifications_on_queue() -> None:
    executor = build_goose_executor()
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
    """A server request (has ``method``) whose id collides with a pending _rpc
    routes to the queue, never resolving our future with a result."""
    executor = build_goose_executor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[2] = fut
    executor._proc = _stdout_proc(
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "session/request_permission", "params": {}}
        )
    )
    await executor._read_stdout()
    # EOF wakes the still-pending future with EOFError (never a result).
    assert isinstance(fut.exception(), EOFError)
    assert executor._queue.get_nowait()["method"] == "session/request_permission"


@pytest.mark.asyncio
async def test_read_stdout_wakes_pending_futures_on_eof() -> None:
    executor = build_goose_executor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[7] = fut
    executor._proc = _stdout_proc()  # immediate EOF
    await executor._read_stdout()
    assert isinstance(fut.exception(), EOFError)


@pytest.mark.asyncio
async def test_read_stderr_drains_without_raising() -> None:
    executor = build_goose_executor()
    mock_stderr = AsyncMock()
    mock_stderr.readline = AsyncMock(side_effect=[b"goose: warming up\n", b""])
    proc = MagicMock()
    proc.stderr = mock_stderr
    executor._proc = proc
    await executor._read_stderr()  # must drain to EOF without raising


# ---------------------------------------------------------------------------
# Handshake / session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_initialized_learns_image_capability() -> None:
    executor = build_goose_executor()
    executor._rpc = AsyncMock(  # type: ignore[method-assign]
        return_value={"result": {"agentCapabilities": {"promptCapabilities": {"image": True}}}}
    )
    await executor._ensure_initialized()
    assert executor._initialized is True
    assert executor._image_supported is True
    # Second call is a no-op (latched).
    executor._rpc.reset_mock()
    await executor._ensure_initialized()
    executor._rpc.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_session_uses_server_assigned_id_and_caches() -> None:
    executor = build_goose_executor()
    executor._rpc = AsyncMock(return_value={"result": {"sessionId": "20260623_7"}})  # type: ignore[method-assign]
    sid = await executor._ensure_session()
    assert sid == "20260623_7" and executor._session_id == "20260623_7"
    executor._rpc.reset_mock()
    assert await executor._ensure_session() == "20260623_7"
    executor._rpc.assert_not_called()  # cached


@pytest.mark.asyncio
async def test_ensure_session_raises_on_missing_session_id() -> None:
    executor = build_goose_executor()
    executor._rpc = AsyncMock(return_value={"result": {}})  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="session/new"):
        await executor._ensure_session()


# ---------------------------------------------------------------------------
# Spawn / sandbox
# ---------------------------------------------------------------------------


def test_sandbox_launch_path_bare_when_no_sandbox() -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    # os_env=None → bare binary.
    assert build_goose_executor(goose_path="goose")._sandbox_launch(())[0] == "goose"
    # os_env present but sandbox explicitly disabled → bare binary.
    disabled = build_goose_executor(
        goose_path="/usr/bin/goose", os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert disabled._sandbox_launch(("PATH",))[0] == "/usr/bin/goose"


def test_sandbox_launch_path_wraps_active_policy(monkeypatch, tmp_path) -> None:
    """An active sandbox wraps goose in a launcher with the config/state dirs from
    :func:`goose_agent_config` as write roots and our spawn env names allowlisted."""
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

    executor = build_goose_executor(
        cwd=str(tmp_path),
        goose_path="/usr/bin/goose",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
    )
    path, argv = executor._sandbox_launch(("PATH", "GOOSE_PROVIDER"))

    assert path == "/fake/launcher"
    assert captured["target"] == "/usr/bin/goose"
    # The binary is replaced by the launcher; goose's own argv rides after it.
    assert argv == ["acp", "--with-builtin", "developer"]
    policy = captured["policy"]
    # goose's config dir is a write root so it can start inside the jail.
    assert any(str(p).endswith(".config/goose") for p in policy.write_roots)
    assert policy.spawn_env_allowlist is not None
    assert "PATH" in policy.spawn_env_allowlist
    assert "GOOSE_PROVIDER" in policy.spawn_env_allowlist


def test_sandbox_launch_path_falls_back_when_backend_unavailable(monkeypatch, tmp_path) -> None:
    """A backend failure degrades to the bare binary, never blocks startup."""
    from omnigent.inner import sandbox as sandbox_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    def _boom(_os_env, _cwd) -> None:
        raise NotImplementedError("no bwrap here")

    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", _boom)
    executor = build_goose_executor(
        cwd=str(tmp_path),
        goose_path="/usr/bin/goose",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
    )
    assert executor._sandbox_launch(("PATH",))[0] == "/usr/bin/goose"


@pytest.mark.asyncio
async def test_start_process_resets_handshake_state(monkeypatch) -> None:
    """A (re)start clears the one-way handshake latch and spawns goose acp."""
    executor = build_goose_executor(goose_path="goose", builtins=("developer",))
    executor._initialized = True
    executor._image_supported = True

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _stdout_proc()  # stdout EOF immediately so the reader exits fast

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await executor._start_process()
    try:
        assert executor._initialized is False  # latch reset
        assert executor._image_supported is False
        assert captured["argv"][1:] == ("acp", "--with-builtin", "developer")
    finally:
        await executor.close()


# ---------------------------------------------------------------------------
# run_turn error + usage paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_boot_failure_yields_error(monkeypatch) -> None:
    executor = build_goose_executor()

    async def boom() -> None:
        raise FileNotFoundError("goose not found")

    monkeypatch.setattr(executor, "_start_process", boom)
    events = [e async for e in executor.run_turn([{"role": "user", "content": "hi"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].retryable is False


@pytest.mark.asyncio
async def test_run_turn_acp_error_resets_session(monkeypatch) -> None:
    """An ACP ``Session not found`` error resets the session and yields a
    retryable error (next turn re-creates the session + re-sends system prompt)."""
    executor = build_goose_executor()
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
    assert executor._session_id is None  # reset
    assert executor._system_prompt_sent is False


@pytest.mark.asyncio
async def test_run_turn_tracks_context_window_from_usage_update() -> None:
    executor = build_goose_executor()
    executor._initialized = True
    executor._session_id = "s"
    executor._proc = MagicMock()
    executor._proc.returncode = None
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


def test_image_blocks_from_content_parses_and_skips() -> None:
    blocks = [
        {"type": "input_text", "text": "ignore"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        {"type": "input_image", "image_url": "https://x/y.png"},  # external → skipped (SSRF)
    ]
    assert AcpExecutor._image_blocks_from_content(blocks) == [
        {"type": "image", "mimeType": "image/png", "data": "AAAB"}
    ]
    assert AcpExecutor._image_blocks_from_content("not a list") == []


def test_text_from_blocks_image_marker_toggle() -> None:
    blocks = [{"type": "input_image", "filename": "pic.png"}]
    assert "[attached image: pic.png]" in AcpExecutor._text_from_blocks(
        blocks, emit_image_marker=True
    )
    assert AcpExecutor._text_from_blocks(blocks, emit_image_marker=False) == ""


@pytest.mark.asyncio
async def test_run_turn_forwards_image_block_when_supported() -> None:
    """With image capability on, an input_image is sent as a real ACP image block
    alongside the text block."""
    executor = build_goose_executor()
    executor._initialized = True
    executor._session_id = "s"
    executor._image_supported = True
    executor._proc = MagicMock()
    executor._proc.returncode = None
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


@pytest.mark.asyncio
async def test_respond_to_agent_request_exception_yields_error_reply() -> None:
    executor = build_goose_executor()

    async def _boom(_params) -> bool:
        raise RuntimeError("kaboom")

    executor._decide_permission = _boom  # type: ignore[method-assign]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    await executor._respond_to_agent_request(_perm_request())
    assert sent[0]["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# Harness wrap (goose_harness)
# ---------------------------------------------------------------------------


def test_resolve_os_env_default(monkeypatch) -> None:
    from omnigent.inner.goose import harness as goose_harness

    monkeypatch.delenv("HARNESS_GOOSE_OS_ENV", raising=False)
    spec = goose_harness._resolve_os_env()
    assert spec.type == "caller_process"
    assert spec.sandbox is not None and spec.sandbox.type == "none"


def test_resolve_os_env_from_json(monkeypatch) -> None:
    from omnigent.inner.goose import harness as goose_harness

    monkeypatch.setenv(
        "HARNESS_GOOSE_OS_ENV",
        json.dumps(
            {
                "type": "caller_process",
                "cwd": "/w",
                "sandbox": {"type": "linux_bwrap"},
                "fork": True,
            }
        ),
    )
    spec = goose_harness._resolve_os_env()
    assert spec.cwd == "/w"
    assert spec.sandbox is not None and spec.sandbox.type == "linux_bwrap"
    assert spec.fork is True


def test_resolve_os_env_malformed_json_falls_back(monkeypatch) -> None:
    from omnigent.inner.goose import harness as goose_harness

    monkeypatch.setenv("HARNESS_GOOSE_OS_ENV", "{not valid json")
    spec = goose_harness._resolve_os_env()
    assert spec.type == "caller_process"
    assert spec.sandbox is not None and spec.sandbox.type == "none"


def test_build_goose_executor_reads_env(monkeypatch) -> None:
    from omnigent.inner.goose import harness as goose_harness

    monkeypatch.setenv("HARNESS_GOOSE_MODEL", "claude-x")
    monkeypatch.setenv("HARNESS_GOOSE_PROVIDER", "anthropic")
    monkeypatch.setenv("HARNESS_GOOSE_CWD", "/work")
    monkeypatch.setenv("HARNESS_GOOSE_PATH", "/bin/goose")
    monkeypatch.delenv("OMNIGENT_GOOSE_PATH", raising=False)
    monkeypatch.setenv("HARNESS_GOOSE_BUILTINS", "developer, computercontroller")
    monkeypatch.delenv("HARNESS_GOOSE_OS_ENV", raising=False)
    ex = goose_harness._build_executor()
    # Asserted on the config, which is where those env values actually land and
    # what the spawn reads; an intermediate attribute could agree and still not
    # reach the subprocess.
    assert ex._cwd == "/work"
    assert ex._config.model == "claude-x"
    assert ex._config.spawn_env["GOOSE_PROVIDER"] == "anthropic"
    assert shlex.split(ex._config.command) == [
        "/bin/goose",
        "acp",
        "--with-builtin",
        "developer",
        "--with-builtin",
        "computercontroller",
    ]


def test_build_goose_executor_defaults(monkeypatch) -> None:
    from omnigent.inner.goose import harness as goose_harness

    for var in (
        "HARNESS_GOOSE_MODEL",
        "HARNESS_GOOSE_PROVIDER",
        "HARNESS_GOOSE_CWD",
        "HARNESS_GOOSE_PATH",
        "HARNESS_GOOSE_BUILTINS",
        "OMNIGENT_RUNNER_WORKSPACE",
        "OMNIGENT_GOOSE_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    ex = goose_harness._build_executor()
    assert ex._config.model is None
    assert "GOOSE_PROVIDER" not in ex._config.spawn_env
    assert shlex.split(ex._config.command) == ["goose", "acp", "--with-builtin", "developer"]


def test_create_app_returns_fastapi() -> None:
    from fastapi import FastAPI

    from omnigent.inner.goose import harness as goose_harness

    assert isinstance(goose_harness.create_app(), FastAPI)
