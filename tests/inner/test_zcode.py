"""ZCode print-mode executor tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnCancelled,
    TurnComplete,
)
from omnigent.inner.zcode_executor import ZCodeExecutor, build_zcode_args
from omnigent.inner.zcode_models import ZCodeModelError, normalize_mode
from omnigent.inner.zcode_stream import StreamError, ToolUpdate, parse_stream_line
from omnigent.onboarding.zcode_auth import zcode_credentials_path, zcode_login_configured
from omnigent.process_logging import data_dir
from omnigent.runtime.workflow import _build_zcode_spawn_env
from omnigent.spec.types import AgentSpec, ApiKeyAuth, ExecutorSpec

_PNG = b"\x89PNG\r\n\x1a\n"


def _write_fake_zcode(path: Path) -> None:
    path.write_text(
        r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
log = os.environ["ZCODE_TEST_LOG"]
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": args, "pid": os.getpid()}) + "\n")

def flag(name):
    return args[args.index(name) + 1] if name in args else None

def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

prompt = flag("-p") or ""
if "scenario:hang" in prompt:
    while True:
        time.sleep(1)
if "scenario:tree" in prompt:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    while True:
        time.sleep(1)
if "scenario:stderr" in prompt:
    sys.stderr.write("x" * 70000 + "FINAL STDERR\n")
    sys.stderr.flush()
    sys.exit(23)
if "scenario:error" in prompt:
    emit({"type": "error", "payload": {
        "error": {"code": "provider_failure", "message": "provider exploded"}
    }})
    sys.exit(1)
if "scenario:permission" in prompt:
    emit({"type": "permission.requested", "payload": {
        "toolName": "shell", "requestId": "permission-1", "toolCallId": "call-1"
    }})

emit({"type": "model.streaming", "payload": {"kind": "reasoning_delta", "delta": "think"}})
emit({"type": "model.streaming", "payload": {"kind": "text_delta", "delta": "pong"}})
if "scenario:tools" in prompt:
    emit({"type": "tool.updated", "payload": {"kind": "scheduled", "toolName": "shell",
          "toolCallId": "call-ok", "input": {"command": "pwd"}}})
    emit({"type": "tool.updated", "payload": {"kind": "started", "toolName": "shell",
          "toolCallId": "call-ok"}})
    emit({"type": "tool.updated", "payload": {"kind": "result", "toolCallId": "call-ok",
          "result": {"success": True, "content": [{"type": "text", "text": "/work"}]}}})
    emit({"type": "tool.updated", "payload": {"kind": "scheduled", "toolName": "read",
          "toolCallId": "call-bad", "input": {"path": "missing"}}})
    emit({"type": "tool.updated", "payload": {"kind": "result", "toolCallId": "call-bad",
          "result": {"success": False, "error": {"code": "ENOENT", "message": "missing"}}}})
emit({"type": "result", "sessionId": flag("--resume") or "sess-1", "response": "pong",
      "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
      "projection": {"contextUsed": 9, "contextWindow": 100}})
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_zcode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    binary = tmp_path / "zcode"
    log = tmp_path / "argv.jsonl"
    _write_fake_zcode(binary)
    monkeypatch.setenv("ZCODE_TEST_LOG", str(log))
    return binary, log


async def _events(
    executor: ZCodeExecutor,
    content: object,
    *,
    system_prompt: str = "",
    model: str | None = None,
    mode: str | None = None,
) -> list[object]:
    events: list[object] = []
    config = ExecutorConfig(model=model, extra={"mode": mode} if mode else {})
    async for event in executor.run_turn(
        [{"role": "user", "content": content, "session_id": "omni-1"}],
        [],
        system_prompt,
        config,
    ):
        events.append(event)
    return events


def _argv(log: Path) -> list[list[str]]:
    return [json.loads(line)["argv"] for line in log.read_text().splitlines()]


def _spec(auth: ApiKeyAuth | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="zcode-agent",
        instructions="test",
        executor=ExecutorSpec(type="omnigent", auth=auth, config={"harness": "zcode"}),
    )


def test_build_args_uses_print_mode_order() -> None:
    assert build_zcode_args(
        "zcode",
        "hi",
        cwd="/work",
        session_id="sess-1",
        attachments=["/work/a.png"],
        disallowed_tools=["web", "shell"],
    ) == [
        "zcode",
        "--cwd",
        "/work",
        "--resume",
        "sess-1",
        "--mode",
        "yolo",
        "--output-format",
        "stream-json",
        "-p",
        "hi",
        "--attach",
        "/work/a.png",
        "--disallowed-tools",
        "web",
        "shell",
    ]


def test_print_stream_parses_top_level_events_and_nested_results() -> None:
    text = parse_stream_line(
        json.dumps({"type": "model.streaming", "payload": {"kind": "text_delta", "delta": "pong"}})
    )
    success = parse_stream_line(
        json.dumps(
            {
                "type": "tool.updated",
                "payload": {
                    "kind": "result",
                    "toolCallId": "c1",
                    "result": {"success": True, "content": ["ok"]},
                },
            }
        )
    )
    failure = parse_stream_line(
        json.dumps(
            {
                "type": "tool.updated",
                "payload": {
                    "kind": "result",
                    "toolCallId": "c1",
                    "result": {"success": False, "error": {"message": "denied"}},
                },
            }
        )
    )
    structured = parse_stream_line(
        json.dumps({"type": "error", "payload": {"error": {"code": "bad", "message": "broken"}}})
    )
    assert getattr(text, "text", None) == "pong"
    assert isinstance(success, ToolUpdate) and success.result == ["ok"]
    assert isinstance(failure, ToolUpdate) and failure.error == "denied"
    assert isinstance(structured, StreamError) and structured.message == "broken"


async def test_executor_streams_resumes_and_marks_tools_internal(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, log = fake_zcode
    executor = ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path))
    try:
        events = await _events(executor, "scenario:tools", system_prompt="SYSTEM")
        await _events(executor, "again", system_prompt="SYSTEM")
    finally:
        await executor.close()
    assert [event.delta for event in events if isinstance(event, ReasoningChunk)] == ["think"]
    assert [event.text for event in events if isinstance(event, TextChunk)] == ["pong"]
    requests = [event for event in events if isinstance(event, ToolCallRequest)]
    completions = [event for event in events if isinstance(event, ToolCallComplete)]
    assert [(event.name, event.metadata) for event in requests] == [
        ("shell", {"internally_executed": True, "call_id": "call-ok"}),
        ("read", {"internally_executed": True, "call_id": "call-bad"}),
    ]
    assert [(event.name, event.status, event.error) for event in completions] == [
        ("shell", ToolCallStatus.SUCCESS, None),
        ("read", ToolCallStatus.ERROR, "missing"),
    ]
    complete = next(event for event in events if isinstance(event, TurnComplete))
    assert complete.usage == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "context_tokens": 9,
    }
    calls = _argv(log)
    assert calls[0][calls[0].index("-p") + 1] == "SYSTEM\n\nscenario:tools"
    assert calls[1][calls[1].index("-p") + 1] == "again"
    assert calls[1][calls[1].index("--resume") + 1] == "sess-1"


async def test_internal_marker_survives_executor_adapter(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import CreateResponseRequest

    binary, _ = fake_zcode
    executor = ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path))
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="omni-1")
    ctx = TurnContext(
        response_id="resp-zcode", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
    )
    await adapter.run_turn(CreateResponseRequest(model="zcode", input="scenario:tools"), ctx)
    await adapter.on_shutdown()
    items = []
    while not ctx._event_queue.empty():
        item = getattr(ctx._event_queue.get_nowait(), "item", None)
        if isinstance(item, dict):
            items.append(item)
    assert [item["call_id"] for item in items if item["type"] == "function_call"] == [
        "call-ok",
        "call-ok",
        "call-bad",
        "call-bad",
    ]
    assert list(adapter._pending_mcp_call_ids) == []


@pytest.mark.parametrize("mode", ["ask", "build", "edit", "plan"])
async def test_non_yolo_mode_is_rejected_before_spawn(
    fake_zcode: tuple[Path, Path], tmp_path: Path, mode: str
) -> None:
    binary, log = fake_zcode
    events = await _events(
        ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path)), "hi", mode=mode
    )
    assert isinstance(events[0], ExecutorError) and events[0].retryable is False
    assert "supports only yolo" in events[0].message
    assert not log.exists()


async def test_model_override_is_rejected_before_spawn(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, log = fake_zcode
    executor = ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path), model="GLM-5.3")
    events = await _events(executor, "hi")
    assert isinstance(events[0], ExecutorError) and events[0].retryable is False
    assert "/model is unsupported" in events[0].message
    assert not log.exists()


@pytest.mark.parametrize(
    ("block", "contents"),
    [
        (
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + base64.b64encode(_PNG).decode(),
                "filename": "pixel.png",
            },
            _PNG,
        ),
        (
            {
                "type": "input_file",
                "file_data": "data:text/plain;base64,aGVsbG8=",
                "filename": "notes.txt",
            },
            b"hello",
        ),
    ],
)
async def test_data_uri_attachments_are_materialized_and_cleaned(
    fake_zcode: tuple[Path, Path],
    tmp_path: Path,
    block: dict[str, str],
    contents: bytes,
) -> None:
    binary, log = fake_zcode
    executor = ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path))
    await _events(executor, [block], system_prompt="SYSTEM")
    args = _argv(log)[0]
    attached = Path(args[args.index("--attach") + 1])
    assert attached.read_bytes() == contents
    assert attached.is_relative_to(data_dir().resolve() / "attachments")
    assert not attached.is_relative_to(tmp_path)
    assert args[args.index("-p") + 1] == "SYSTEM\n\n(see attachments)"
    await executor.close_session("omni-1")
    assert not attached.exists()
    assert not attached.parent.exists()
    assert not any(tmp_path.glob(".omnigent-zcode-*"))


async def test_local_attachment_must_resolve_inside_cwd(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, log = fake_zcode
    local = tmp_path / "local.txt"
    local.write_text("ok")
    executor = ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path))
    await _events(executor, [{"type": "input_file", "path": "local.txt"}])
    args = _argv(log)[0]
    assert args[args.index("--attach") + 1] == str(local)
    events = await _events(executor, [{"type": "input_file", "path": "../outside.txt"}])
    assert isinstance(events[0], ExecutorError) and events[0].retryable is False


async def test_remote_attachment_is_rejected_explicitly(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, log = fake_zcode
    events = await _events(
        ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path)),
        [{"type": "input_image", "image_url": "https://example.com/private.png"}],
    )
    assert isinstance(events[0], ExecutorError) and "Remote" in events[0].message
    assert not log.exists()


async def test_stderr_drain_finishes_after_process_exit(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, _ = fake_zcode
    events = await _events(
        ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path)), "scenario:stderr"
    )
    error = next(event for event in events if isinstance(event, ExecutorError))
    assert "FINAL STDERR" in error.message
    assert len(error.message) <= 550


async def test_structured_error_is_reported_once(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, _ = fake_zcode
    events = await _events(
        ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path)), "scenario:error"
    )
    terminal = [
        event
        for event in events
        if isinstance(event, (ExecutorError, TurnCancelled, TurnComplete))
    ]
    assert len(terminal) == 1
    assert isinstance(terminal[0], ExecutorError) and "provider exploded" in terminal[0].message


async def test_permission_event_defers_to_headless_broker_result(
    fake_zcode: tuple[Path, Path], tmp_path: Path
) -> None:
    binary, _ = fake_zcode
    events = await _events(
        ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path)), "scenario:permission"
    )
    terminal = [
        event
        for event in events
        if isinstance(event, (ExecutorError, TurnCancelled, TurnComplete))
    ]
    assert len(terminal) == 1
    assert isinstance(terminal[0], TurnComplete)


async def test_missing_binary_is_non_retryable(tmp_path: Path) -> None:
    missing = tmp_path / "missing-zcode"
    events = await _events(ZCodeExecutor(zcode_path=str(missing), cwd=str(tmp_path)), "hi")
    assert isinstance(events[0], ExecutorError)
    assert events[0].retryable is False
    assert str(missing) in events[0].message


async def test_timeout_terminates_tree_and_emits_one_terminal_event(
    fake_zcode: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.inner import zcode_executor as zcode_module

    binary, _ = fake_zcode
    terminated: list[int | None] = []
    original = zcode_module._proc.terminate_tree
    monkeypatch.setattr(zcode_module, "_TURN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        zcode_module._proc,
        "terminate_tree",
        lambda proc: (terminated.append(proc.pid), original(proc))[1],
    )
    events = await _events(
        ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path)), "scenario:tree"
    )
    terminal = [
        event
        for event in events
        if isinstance(event, (ExecutorError, TurnCancelled, TurnComplete))
    ]
    assert len(terminal) == 1 and isinstance(terminal[0], ExecutorError)
    assert terminated


async def test_interrupt_terminates_tree_and_emits_one_cancelled(
    fake_zcode: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import zcode_executor as zcode_module

    binary, log = fake_zcode
    executor = ZCodeExecutor(zcode_path=str(binary), cwd=str(tmp_path))
    terminated: list[int | None] = []
    original = zcode_module._proc.terminate_tree
    monkeypatch.setattr(
        zcode_module._proc,
        "terminate_tree",
        lambda proc: (terminated.append(proc.pid), original(proc))[1],
    )
    task = asyncio.create_task(_events(executor, "scenario:hang"))
    for _ in range(100):
        if log.exists():
            break
        await asyncio.sleep(0.01)
    assert await executor.interrupt_session("omni-1") is True
    events = await asyncio.wait_for(task, timeout=2)
    terminal = [
        event
        for event in events
        if isinstance(event, (ExecutorError, TurnCancelled, TurnComplete))
    ]
    assert len(terminal) == 1 and isinstance(terminal[0], TurnCancelled)
    assert terminated


async def test_stop_process_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import zcode_executor as zcode_module

    class StubbornProcess:
        def __init__(self) -> None:
            self.pid = 123
            self.returncode: int | None = None
            self.exited = asyncio.Event()

        async def wait(self) -> int:
            await self.exited.wait()
            return self.returncode or 0

    process = StubbornProcess()
    terminated: list[object] = []
    killed: list[object] = []
    monkeypatch.setattr(zcode_module, "_EXIT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(zcode_module._proc, "terminate_tree", terminated.append)

    def kill(proc: object) -> None:
        killed.append(proc)
        process.returncode = -9
        process.exited.set()

    monkeypatch.setattr(zcode_module._proc, "kill_tree", kill)
    executor = ZCodeExecutor()
    await executor._stop_process(process)  # type: ignore[arg-type]
    assert terminated == [process]
    assert killed == [process]


def test_sandbox_does_not_grant_credentials_or_global_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import sandbox as sandbox_module
    from omnigent.inner.sandbox import SandboxPolicy

    captured: dict[str, Any] = {}

    def resolve(_os_env: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        return SandboxPolicy(
            backend_type="darwin_seatbelt",
            active=True,
            read_roots=[cwd],
            write_roots=[cwd],
            write_files=[],
            allow_network=True,
        )

    def launcher(target: str, policy: SandboxPolicy) -> str:
        captured.update(target=target, policy=policy)
        return "/tmp/zcode-launcher"

    monkeypatch.setattr(sandbox_module, "resolve_sandbox", resolve)
    monkeypatch.setattr(sandbox_module, "create_exec_launcher", launcher)
    executor = ZCodeExecutor(
        zcode_path="/opt/zcode/bin/zcode",
        cwd=str(tmp_path),
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
    )
    assert executor._sandbox_launch_path(("PATH",)) == "/tmp/zcode-launcher"
    policy = captured["policy"]
    assert Path("/opt/zcode/bin") in policy.read_roots
    assert Path.home() / ".zcode" not in policy.read_roots
    assert Path.home() / ".zcode" not in policy.write_roots
    assert Path("/tmp") not in policy.write_roots
    cache = tmp_path / "attachments" / "abc"
    executor._sandbox_launch_path(("PATH",), [cache])
    assert cache.resolve() in captured["policy"].read_roots
    assert cache.resolve() not in captured["policy"].write_roots


def test_sandbox_launcher_is_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omnigent.inner.zcode_executor import _cleanup_launcher

    launcher = tmp_path / "launcher"
    launcher.write_text("temporary")
    _cleanup_launcher(str(launcher), "/opt/zcode")
    assert not launcher.exists()
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: None)
    _cleanup_launcher("/opt/zcode", "/opt/zcode")


async def test_configured_sandbox_failure_is_non_retryable(
    fake_zcode: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import sandbox as sandbox_module

    binary, log = fake_zcode
    monkeypatch.setattr(
        sandbox_module,
        "resolve_sandbox",
        lambda *_args: (_ for _ in ()).throw(OSError("backend unavailable")),
    )
    executor = ZCodeExecutor(
        zcode_path=str(binary),
        cwd=str(tmp_path),
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
    )
    events = await _events(executor, "hi")
    assert isinstance(events[0], ExecutorError)
    assert events[0].retryable is False
    assert "configured ZCode sandbox" in events[0].message
    assert not log.exists()


def test_normalize_mode_is_fail_closed() -> None:
    assert normalize_mode(None) == "yolo"
    assert normalize_mode("bypassPermissions") == "yolo"
    with pytest.raises(ZCodeModelError, match="supports only yolo"):
        normalize_mode("build")


def test_spawn_env_rejects_omnigent_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    with pytest.raises(OmnigentError, match=r"executor\.auth"):
        _build_zcode_spawn_env(_spec(auth=ApiKeyAuth(api_key="sk-test")))


def test_login_probe_checks_size_not_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    assert zcode_login_configured() is False
    path = zcode_credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text("encrypted-blob")
    assert zcode_login_configured() is True
    assert os.environ["ZCODE_DATA_BASE_DIR"] == str(tmp_path)
