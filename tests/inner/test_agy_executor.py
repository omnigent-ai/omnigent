"""Tests for :class:`omnigent.inner.agy_executor.AgyExecutor`."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import agy_executor as ae
from omnigent.inner.agy_executor import AGY_DEFAULT_MODEL, AgyExecutor
from omnigent.inner.executor import TextChunk, TurnComplete


def _make_executor(**kwargs: Any) -> AgyExecutor:
    with patch("omnigent.inner.agy_executor._find_agy", return_value="/usr/bin/agy"):
        return AgyExecutor(**kwargs)


def test_missing_agy_raises_import_error() -> None:
    with patch("omnigent.inner.agy_executor._find_agy", return_value=None):
        with pytest.raises(ImportError, match="agy"):
            AgyExecutor()


def test_default_model_is_gemini_31_pro_high() -> None:
    assert AGY_DEFAULT_MODEL == "Gemini 3.1 Pro (High)"


def test_clean_agy_env_allows_agy_google_prefixes_denies_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGY_CONFIG", "1")
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "ag-key")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = ae._clean_agy_env()

    assert env["AGY_CONFIG"] == "1"
    assert env["ANTIGRAVITY_API_KEY"] == "ag-key"
    assert env["GEMINI_API_KEY"] == "g-key"
    assert env["GOOGLE_API_KEY"] == "google-key"
    assert env["PATH"] == "/usr/bin"
    assert "DATABRICKS_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_argv_uses_print_permissions_timeout_and_model() -> None:
    argv = ae._build_argv(
        agy_path="/usr/bin/agy",
        model=AGY_DEFAULT_MODEL,
        print_timeout="30m",
        sandbox=False,
        prompt="hello",
    )
    assert argv[:5] == [
        "/usr/bin/agy",
        "--print",
        "--model",
        "Gemini 3.1 Pro (High)",
        "--dangerously-skip-permissions",
    ]
    assert argv[argv.index("--print-timeout") + 1] == "30m"
    assert argv[-1] == "hello"


class _FakeProcess:
    def __init__(self, stdout_lines: list[bytes], returncode: int = 0) -> None:
        self._stdout_lines = stdout_lines
        self.returncode: int | None = None
        self._final_returncode = returncode
        self.stderr = None

    @property
    def stdout(self) -> _FakeStdoutReader:
        return _FakeStdoutReader(self._stdout_lines)

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class _FakeStdoutReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self._idx = 0

    def __aiter__(self) -> _FakeStdoutReader:
        return self

    async def __anext__(self) -> bytes:
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


async def _run_one_turn(executor: AgyExecutor) -> list[Any]:
    return [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello", "session_id": "conv1"}],
            [],
            "system",
            None,
        )
    ]


@pytest.mark.asyncio
async def test_run_turn_spawns_agy_print_with_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["argv"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(stdout_lines=[b"hello back\n"], returncode=0)

    monkeypatch.setattr(ae, "_create_subprocess_exec", _spawn)
    executor = _make_executor(cwd="/repo")

    events = await _run_one_turn(executor)

    argv = captured["argv"]
    assert argv[0] == "/usr/bin/agy"
    assert "--print" in argv
    assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (High)"
    assert "--dangerously-skip-permissions" in argv
    assert argv[-1] == "system\n\nhello"
    assert captured["kwargs"]["cwd"] == "/repo"
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["hello back"]
    complete = [e for e in events if isinstance(e, TurnComplete)]
    assert len(complete) == 1
    assert complete[0].response == "hello back"
    assert complete[0].usage is None
