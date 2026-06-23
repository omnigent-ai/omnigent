"""Unit tests for GooseExecutor (headless Goose ACP / JSON-RPC 2.0 mode).

Covers construction defaults, provider-env overrides, tool-call extraction and
permission-outcome mapping from Goose's ACP ``session/request_permission`` shape,
usage mapping, prompt-block folding, the permission → policy/elicitation
round-trip, run_turn streaming, and the harness wrap. Protocol shapes match a
verified Goose 1.38 ``goose acp`` session.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
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
    result = {
        "stopReason": "end_turn",
        "usage": {"totalTokens": 100, "inputTokens": 80, "outputTokens": 20},
    }
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
        {
            "type": "input_file",
            "filename": "b.pdf",
            "file_data": "data:application/pdf;base64,AAA=",
        },
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
    allow_exec._elicitation_handler.assert_awaited_once_with(
        "shell", {"command": "rm -f victim.txt"}
    )

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
async def test_close_with_no_process_is_a_noop() -> None:
    await GooseExecutor().close()  # must not raise


# ---------------------------------------------------------------------------
# close() / process lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_terminates_process() -> None:
    executor = GooseExecutor()
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
    executor = GooseExecutor()
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
    executor = GooseExecutor()
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
    executor = GooseExecutor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[42] = fut
    executor._proc = _stdout_proc(json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}))
    await executor._read_stdout()
    assert fut.done() and fut.result()["result"]["ok"] is True


@pytest.mark.asyncio
async def test_read_stdout_puts_notifications_on_queue() -> None:
    executor = GooseExecutor()
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
    executor = GooseExecutor()
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
    executor = GooseExecutor()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    executor._pending[7] = fut
    executor._proc = _stdout_proc()  # immediate EOF
    await executor._read_stdout()
    assert isinstance(fut.exception(), EOFError)


@pytest.mark.asyncio
async def test_read_stderr_drains_without_raising() -> None:
    executor = GooseExecutor()
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
    executor = GooseExecutor()
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
    executor = GooseExecutor()
    executor._rpc = AsyncMock(return_value={"result": {"sessionId": "20260623_7"}})  # type: ignore[method-assign]
    sid = await executor._ensure_session()
    assert sid == "20260623_7" and executor._session_id == "20260623_7"
    executor._rpc.reset_mock()
    assert await executor._ensure_session() == "20260623_7"
    executor._rpc.assert_not_called()  # cached


@pytest.mark.asyncio
async def test_ensure_session_raises_on_missing_session_id() -> None:
    executor = GooseExecutor()
    executor._rpc = AsyncMock(return_value={"result": {}})  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="session/new"):
        await executor._ensure_session()


# ---------------------------------------------------------------------------
# Spawn / sandbox
# ---------------------------------------------------------------------------


def test_sandbox_launch_path_bare_when_no_sandbox() -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    # os_env=None → bare binary.
    assert GooseExecutor(goose_path="goose")._sandbox_launch_path(()) == "goose"
    # os_env present but sandbox explicitly disabled → bare binary.
    disabled = GooseExecutor(
        goose_path="/usr/bin/goose", os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert disabled._sandbox_launch_path(("PATH",)) == "/usr/bin/goose"


def test_sandbox_launch_path_wraps_active_policy(monkeypatch, tmp_path) -> None:
    """An active sandbox wraps goose in a launcher with its config/state dirs as
    write roots and our spawn env names allowlisted."""
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

    executor = GooseExecutor(
        cwd=str(tmp_path),
        goose_path="/usr/bin/goose",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
    )
    path = executor._sandbox_launch_path(("PATH", "GOOSE_PROVIDER"))

    assert path == "/fake/launcher"
    assert captured["target"] == "/usr/bin/goose"
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
    executor = GooseExecutor(
        cwd=str(tmp_path),
        goose_path="/usr/bin/goose",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
    )
    assert executor._sandbox_launch_path(("PATH",)) == "/usr/bin/goose"


@pytest.mark.asyncio
async def test_start_process_resets_handshake_state(monkeypatch) -> None:
    """A (re)start clears the one-way handshake latch and spawns goose acp."""
    executor = GooseExecutor(goose_path="goose", builtins=("developer",))
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
    executor = GooseExecutor()

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
    executor = GooseExecutor()
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
    executor = GooseExecutor()
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


def test_inline_text_file_data_variants() -> None:
    from omnigent.inner.goose_executor import _inline_text_file_data

    assert _inline_text_file_data("plain text") == "plain text"  # non-data-URI passthrough
    assert _inline_text_file_data("") == ""
    assert _inline_text_file_data(123) == ""  # non-str
    assert _inline_text_file_data("data:image/png;base64,AAA=") == ""  # binary → not inlined
    assert _inline_text_file_data("data:text/plain;base64,aGk=") == "hi"  # text decoded


def test_image_blocks_from_content_parses_and_skips() -> None:
    blocks = [
        {"type": "input_text", "text": "ignore"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        {"type": "input_image", "image_url": "https://x/y.png"},  # external → skipped (SSRF)
    ]
    assert GooseExecutor._image_blocks_from_content(blocks) == [
        {"type": "image", "mimeType": "image/png", "data": "AAAB"}
    ]
    assert GooseExecutor._image_blocks_from_content("not a list") == []


def test_text_from_blocks_image_marker_toggle() -> None:
    blocks = [{"type": "input_image", "filename": "pic.png"}]
    assert "[attached image: pic.png]" in GooseExecutor._text_from_blocks(
        blocks, emit_image_marker=True
    )
    assert GooseExecutor._text_from_blocks(blocks, emit_image_marker=False) == ""


@pytest.mark.asyncio
async def test_run_turn_forwards_image_block_when_supported() -> None:
    """With image capability on, an input_image is sent as a real ACP image block
    alongside the text block."""
    executor = GooseExecutor()
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


# ---------------------------------------------------------------------------
# _decide_permission branch coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_permission_no_gates_allows() -> None:
    assert await GooseExecutor()._decide_permission(_perm_request()["params"]) is True


@pytest.mark.asyncio
async def test_decide_permission_policy_ask_without_handler_denies() -> None:
    executor = GooseExecutor()
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_ASK")
    )  # no elicitation handler wired → fail closed
    assert await executor._decide_permission(_perm_request()["params"]) is False


@pytest.mark.asyncio
async def test_decide_permission_policy_exception_falls_through_to_elicit() -> None:
    executor = GooseExecutor()

    async def _boom(*_a) -> object:
        raise RuntimeError("policy backend down")

    executor._policy_evaluator = _boom  # type: ignore[attr-defined]
    executor._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    assert await executor._decide_permission(_perm_request()["params"]) is True


@pytest.mark.asyncio
async def test_respond_to_agent_request_exception_yields_error_reply() -> None:
    executor = GooseExecutor()

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
    from omnigent.inner import goose_harness

    monkeypatch.delenv("HARNESS_GOOSE_OS_ENV", raising=False)
    spec = goose_harness._resolve_os_env()
    assert spec.type == "caller_process"
    assert spec.sandbox is not None and spec.sandbox.type == "none"


def test_resolve_os_env_from_json(monkeypatch) -> None:
    from omnigent.inner import goose_harness

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
    from omnigent.inner import goose_harness

    monkeypatch.setenv("HARNESS_GOOSE_OS_ENV", "{not valid json")
    spec = goose_harness._resolve_os_env()
    assert spec.type == "caller_process"
    assert spec.sandbox is not None and spec.sandbox.type == "none"


def test_build_goose_executor_reads_env(monkeypatch) -> None:
    from omnigent.inner import goose_harness

    monkeypatch.setenv("HARNESS_GOOSE_MODEL", "claude-x")
    monkeypatch.setenv("HARNESS_GOOSE_PROVIDER", "anthropic")
    monkeypatch.setenv("HARNESS_GOOSE_CWD", "/work")
    monkeypatch.setenv("HARNESS_GOOSE_PATH", "/bin/goose")
    monkeypatch.setenv("HARNESS_GOOSE_BUILTINS", "developer, computercontroller")
    monkeypatch.delenv("HARNESS_GOOSE_OS_ENV", raising=False)
    ex = goose_harness._build_goose_executor()
    assert ex._model == "claude-x"
    assert ex._provider == "anthropic"
    assert ex._cwd == "/work"
    assert ex._goose_path == "/bin/goose"
    assert ex._builtins == ("developer", "computercontroller")


def test_build_goose_executor_defaults(monkeypatch) -> None:
    from omnigent.inner import goose_harness

    for var in (
        "HARNESS_GOOSE_MODEL",
        "HARNESS_GOOSE_PROVIDER",
        "HARNESS_GOOSE_CWD",
        "HARNESS_GOOSE_PATH",
        "HARNESS_GOOSE_BUILTINS",
        "OMNIGENT_RUNNER_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)
    ex = goose_harness._build_goose_executor()
    assert ex._model is None and ex._provider is None
    assert ex._goose_path == "goose"
    assert ex._builtins == ("developer",)


def test_create_app_returns_fastapi() -> None:
    from fastapi import FastAPI

    from omnigent.inner import goose_harness

    assert isinstance(goose_harness.create_app(), FastAPI)
