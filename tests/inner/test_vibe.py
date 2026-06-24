"""Tests for the vibe harness and executor."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import vibe_executor, vibe_harness
from omnigent.inner.executor import (
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
)
from omnigent.inner.vibe_executor import VibeExecutor
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESS_ALIASES


def test_vibe_in_module_registry() -> None:
    assert _HARNESS_MODULES.get("vibe") == "omnigent.inner.vibe_harness"
    assert "mistral-vibe" in OMNIGENT_HARNESS_ALIASES


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_VIBE_AGENT", "auto-approve")
    monkeypatch.setenv("HARNESS_VIBE_CWD", "/tmp/vibe-cwd")
    monkeypatch.setenv("HARNESS_VIBE_PATH", "/custom/bin/vibe")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("omnigent.inner.vibe_harness.VibeExecutor.__init__", _fake_init):
        vibe_harness._build_vibe_executor()

    assert captured["agent"] == "auto-approve"
    assert captured["cwd"] == "/tmp/vibe-cwd"
    assert captured["binary_path"] == "/custom/bin/vibe"


def test_build_argv_first_turn_no_resume() -> None:
    ex = VibeExecutor(binary_path="vibe")
    argv = ex._build_argv(prompt_text="hello")
    assert "-c" not in argv
    assert "--resume" not in argv
    assert argv[-2:] == ["-p", "hello"]


def test_build_argv_with_session_id() -> None:
    ex = VibeExecutor(binary_path="vibe")
    argv = ex._build_argv(prompt_text="hello", session_id="session_123")
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "session_123"


def test_translate_event_assistant_text() -> None:
    ex = VibeExecutor(binary_path="vibe")
    events = ex._translate_event({"role": "assistant", "content": "Hi there!"}, "key_1")
    assert len(events) == 1
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "Hi there!"


def test_translate_event_tool_call() -> None:
    ex = VibeExecutor(binary_path="vibe")
    events = ex._translate_event({
        "role": "assistant",
        "tool_calls": [{
            "id": "call_123",
            "function": {"name": "Bash", "arguments": '{"command": "ls"}'}
        }]
    }, "key_1")
    assert len(events) == 1
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].name == "Bash"
    assert events[0].args == {"command": "ls"}
    assert events[0].metadata == {"call_id": "call_123"}


def test_translate_event_tool_result() -> None:
    ex = VibeExecutor(binary_path="vibe")
    events = ex._translate_event({
        "role": "tool",
        "tool_call_id": "call_123",
        "content": "some output"
    }, "key_1")
    assert len(events) == 1
    assert isinstance(events[0], ToolCallComplete)
    assert events[0].result == "some output"
    assert events[0].metadata == {"call_id": "call_123"}
    assert events[0].status == "success"


def test_translate_event_session_id_capture() -> None:
    ex = VibeExecutor(binary_path="vibe")
    events = ex._translate_event({
        "role": "assistant",
        "content": "Hi",
        "session_id": "session_abc"
    }, "key_1")
    assert len(events) == 1
    assert ex._session_map["key_1"] == "session_abc"


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]
    def __aiter__(self) -> _FakeStdout:
        return self
    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStderr:
    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self._done = False
    async def read(self, _n: int) -> bytes:
        if self._done:
            return b""
        self._done = True
        return self._blob


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_blob: bytes, returncode: int = 0) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr(stderr_blob)
        self._returncode = returncode
    @property
    def returncode(self) -> int | None:
        return self._returncode
    async def wait(self) -> int:
        return self._returncode
    def terminate(self) -> None:
        pass
    def kill(self) -> None:
        pass


async def _collect(ex: VibeExecutor, messages: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    async for evt in ex.run_turn(messages=messages, tools=[], system_prompt=""):
        out.append(evt)
    return out


def test_run_turn_streams_text_and_emits_turn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout_lines = [
        json.dumps({"role": "assistant", "content": "Hi there!"}),
    ]
    fake = _FakeProcess(stdout_lines, b"", returncode=0)

    captured_argv: list[str] = []

    async def _fake_spawn(*args: Any, **_kwargs: Any) -> _FakeProcess:
        captured_argv.extend(args)
        return fake

    monkeypatch.setattr("omnigent.inner.vibe_executor._create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(shutil, "which", lambda _binary: "/usr/local/bin/vibe")
    monkeypatch.setattr("omnigent.inner.vibe_executor.Path.exists", lambda _self: True)

    ex = VibeExecutor(binary_path="vibe")
    events = asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    assert [c.text for c in text_chunks] == ["Hi there!"]
    assert captured_argv[0] == "vibe"
    assert "-c" not in captured_argv
    assert "--output" in captured_argv
    assert "streaming" in captured_argv


def test_run_turn_nonzero_exit_yields_executor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProcess([], b"boom\n", returncode=2)
    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return fake
    monkeypatch.setattr("omnigent.inner.vibe_executor._create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(shutil, "which", lambda _binary: "/usr/local/bin/vibe")
    monkeypatch.setattr("omnigent.inner.vibe_executor.Path.exists", lambda _self: True)

    ex = VibeExecutor(binary_path="vibe")
    events = asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors and "exited with code 2" in errors[0].message
