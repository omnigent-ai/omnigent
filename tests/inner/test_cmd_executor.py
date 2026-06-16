"""Tests for :class:`omnigent.inner.cmd_executor.CmdExecutor`."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import cmd_executor as ce
from omnigent.inner.cmd_executor import CmdExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TextChunk, TurnComplete


def _make_executor(**kwargs: Any) -> CmdExecutor:
    with patch("omnigent.inner.cmd_executor._find_cmd", return_value="/usr/bin/cmd"):
        return CmdExecutor(**kwargs)


def test_missing_cmd_raises_import_error() -> None:
    with patch("omnigent.inner.cmd_executor._find_cmd", return_value=None):
        with pytest.raises(ImportError, match="cmd"):
            CmdExecutor()


def test_clean_cmd_env_allows_cmd_prefix_denies_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny-by-default allowlist mirrors cursor / mimo: only known-safe categories pass.

    ``CMD_`` / ``COMMANDCODE_`` / ``COMMAND_CODE_`` prefixes pass through
    Command Code's own config knobs; ``DATABRICKS_TOKEN`` and
    ``OPENAI_API_KEY`` (unrelated API credentials) must NOT reach the
    child process.
    """
    monkeypatch.setenv("CMD_CONFIG", "1")
    monkeypatch.setenv("COMMANDCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = ce._clean_cmd_env()

    assert env["CMD_CONFIG"] == "1"
    assert env["COMMANDCODE_SERVER_PASSWORD"] == "pw"
    assert env["PATH"] == "/usr/bin"
    assert "DATABRICKS_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_resolve_model_prefers_executor_config() -> None:
    """``ExecutorConfig.model`` (per-session /model override) wins."""
    assert (
        ce._resolve_model(ExecutorConfig(model="openai/gpt-5"), "anthropic/claude-sonnet-4")
        == "openai/gpt-5"
    )


def test_resolve_model_falls_back_to_executor_override() -> None:
    assert ce._resolve_model(None, "anthropic/claude-sonnet-4") == "anthropic/claude-sonnet-4"


def test_resolve_model_none_omits_cmd_model_flag() -> None:
    """``None`` means "let ``cmd`` pick its default" — the executor omits ``--model``."""
    assert ce._resolve_model(None, None) is None
    assert ce._resolve_model(ExecutorConfig(model=None), None) is None


def test_build_argv_includes_print_yolo_and_max_turns() -> None:
    argv = ce._build_argv(
        cmd_path="/usr/bin/cmd",
        model=None,
        max_turns=10,
        prompt="hello",
    )
    assert argv[:4] == ["/usr/bin/cmd", "--print", "--yolo", "--max-turns"]
    assert argv[4] == "10"
    assert argv[-1] == "hello"
    # No ``--model`` when model is None.
    assert "--model" not in argv


def test_build_argv_adds_model_flag_when_present() -> None:
    argv = ce._build_argv(
        cmd_path="/usr/bin/cmd",
        model="claude-sonnet-4-6",
        max_turns=10,
        prompt="hello",
    )
    # ``--model`` sits between ``--max-turns`` and the prompt.
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv[-1] == "hello"


# ---------------------------------------------------------------------------
# run_turn subprocess integration
# ---------------------------------------------------------------------------


class _FakeProcess:
    """A minimal stand-in for :class:`asyncio.subprocess.Process`.

    The cmd executor only touches ``stdout`` (async-iterated line by
    line), ``wait()``, ``terminate()``/``kill()`` (interrupt path), and
    ``returncode``. The class is intentionally tiny — a run_turn test
    just feeds a sequence of stdout lines and asserts the yielded events.
    """

    def __init__(self, stdout_lines: list[bytes], returncode: int = 0) -> None:
        self._stdout_lines = stdout_lines
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._writer_index = 0
        self.stderr = None
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    @property
    def stdout(self) -> _FakeStdoutReader:
        return _FakeStdoutReader(self._stdout_lines, self)

    async def wait(self) -> int:
        self.wait_calls += 1
        # First wait sets the final returncode; subsequent waits (e.g. the
        # SIGKILL backstop) just return it.
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _FakeStdoutReader:
    def __init__(self, lines: list[bytes], process: _FakeProcess) -> None:
        self._lines = lines
        self._process = process
        self._idx = 0

    def __aiter__(self) -> _FakeStdoutReader:
        return self

    async def __anext__(self) -> bytes:
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


async def _run_one_turn(executor: CmdExecutor, config: ExecutorConfig | None) -> list[Any]:
    return [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello", "session_id": "conv1"}],
            [],
            "system",
            config,
        )
    ]


@pytest.mark.asyncio
async def test_run_turn_spawns_cmd_with_expected_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["argv"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(stdout_lines=[b"hello back\n"], returncode=0)

    monkeypatch.setattr(ce, "_create_subprocess_exec", _spawn)
    executor = _make_executor(cwd="/repo", model="claude-sonnet-4-6", max_turns=5)

    events = await _run_one_turn(executor, None)

    argv = captured["argv"]
    # ``argv`` is a positional tuple from asyncio.create_subprocess_exec.
    assert argv[0] == "/usr/bin/cmd"
    assert "--print" in argv
    assert "--yolo" in argv
    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    # The prompt is the final argv element (system + user text).
    assert argv[-1] == "system\n\nhello"
    # Spawn kwargs: cwd, env, stdout/stderr pipes.
    import asyncio as _aio

    assert captured["kwargs"]["cwd"] == "/repo"
    assert captured["kwargs"]["stdout"] == _aio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == _aio.subprocess.PIPE
    # Single TextChunk for the one non-empty stdout line + TurnComplete.
    text_events = [e for e in events if isinstance(e, TextChunk)]
    assert len(text_events) == 1
    assert text_events[0].text == "hello back"
    complete = [e for e in events if isinstance(e, TurnComplete)]
    assert len(complete) == 1
    assert complete[0].response == "hello back"
    assert complete[0].usage is None


@pytest.mark.asyncio
async def test_run_turn_omits_model_flag_when_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["argv"] = args
        return _FakeProcess(stdout_lines=[b"ok\n"], returncode=0)

    monkeypatch.setattr(ce, "_create_subprocess_exec", _spawn)
    executor = _make_executor(cwd="/repo")  # no model override

    await _run_one_turn(executor, None)

    assert "--model" not in captured["argv"]


@pytest.mark.asyncio
async def test_run_turn_yields_executor_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero exit surfaces as ExecutorError; max-turns (exit 8) is retryable."""

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=[b"partial\n"], returncode=1)

    monkeypatch.setattr(ce, "_create_subprocess_exec", _spawn)
    executor = _make_executor(cwd="/repo")

    events = await _run_one_turn(executor, None)

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "exited with code 1" in errors[0].message
    assert errors[0].retryable is False


@pytest.mark.asyncio
async def test_run_turn_marks_max_turns_exhausted_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command Code exits with code 8 when ``--max-turns`` is hit; mark as retryable."""

    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=[b""], returncode=8)

    monkeypatch.setattr(ce, "_create_subprocess_exec", _spawn)
    executor = _make_executor(cwd="/repo")

    events = await _run_one_turn(executor, None)

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].retryable is True
