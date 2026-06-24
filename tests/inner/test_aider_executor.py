"""Unit tests for AiderExecutor (one-shot ``aider --message`` CLI mode).

Tests cover:
- Executor construction and attribute defaults + capability flags
- Prompt construction (_text_from_blocks, _latest_user_text)
- run_turn event translation (stdout lines -> TextChunk, then TurnComplete)
- run_turn argv (flags, --model, --restore-chat-history, system-prompt fold)
- run_turn error paths (missing binary, non-zero exit)
- Harness wrap env plumbing (_build_aider_executor)
- Harness registry / allowlist / install / readiness wiring
- FastAPI app shape
"""

from __future__ import annotations

import asyncio
import os

import pytest

from omnigent.inner import aider_executor
from omnigent.inner.aider_executor import AiderExecutor
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete

# ---------------------------------------------------------------------------
# Fakes: stand in for the aider subprocess
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal async stream: yields queued byte-lines then EOF (``b""``)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        data = b"".join(self._lines)
        self._lines = []
        return data


class _FakeProc:
    """Stand-in for an ``asyncio`` subprocess for the one-shot aider run."""

    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_lines: list[bytes] | None = None,
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines or [])
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _install_fake_aider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout_lines: list[bytes],
    stderr_lines: list[bytes] | None = None,
    returncode: int = 0,
) -> list[list[str]]:
    """Patch the binary probe + subprocess spawn; capture each call's argv."""
    captured_argv: list[list[str]] = []

    async def _fake_exec(*args: str, **_kwargs: object) -> _FakeProc:
        captured_argv.append([str(a) for a in args])
        return _FakeProc(stdout_lines, stderr_lines, returncode)

    monkeypatch.setattr(aider_executor.shutil, "which", lambda _name: "/usr/bin/aider")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured_argv


def _user(text: str) -> dict[str, object]:
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------
# Construction / attribute defaults / capabilities
# ---------------------------------------------------------------------------


def test_executor_default_attributes() -> None:
    executor = AiderExecutor()
    assert executor._aider_path == "aider"
    assert executor._model is None
    assert executor._cwd == os.getcwd()
    assert executor._system_prompt_sent is False
    assert executor._has_history is False


def test_executor_stores_model_and_cwd() -> None:
    executor = AiderExecutor(cwd="/tmp", model="gpt-4o", aider_path="/opt/aider")
    assert executor._cwd == "/tmp"
    assert executor._model == "gpt-4o"
    assert executor._aider_path == "/opt/aider"


def test_capability_flags() -> None:
    executor = AiderExecutor()
    assert executor.supports_streaming() is True
    # Aider runs its own agent loop and edits files itself.
    assert executor.handles_tools_internally() is True
    assert executor.supports_tool_calling() is False


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_latest_user_text_plain_string() -> None:
    executor = AiderExecutor()
    messages = [
        {"role": "assistant", "content": "earlier"},
        _user("hello there"),
    ]
    assert executor._latest_user_text(messages) == "hello there"


def test_text_from_blocks_folds_text_and_file() -> None:
    blocks = [
        {"type": "input_text", "text": "summarize"},
        {
            "type": "input_file",
            "filename": "a.txt",
            "file_data": "data:text/plain;base64,aGk=",  # "hi"
        },
    ]
    folded = AiderExecutor._text_from_blocks(blocks)
    assert "summarize" in folded
    assert "--- attached file: a.txt ---" in folded
    assert "hi" in folded


def test_text_from_blocks_image_marker() -> None:
    blocks = [{"type": "input_image", "filename": "pic.png"}]
    assert AiderExecutor._text_from_blocks(blocks) == "[attached image: pic.png]"


# ---------------------------------------------------------------------------
# run_turn — happy path
# ---------------------------------------------------------------------------


async def test_run_turn_streams_text_then_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_aider(monkeypatch, stdout_lines=[b"Hello\n", b"world\n"])
    executor = AiderExecutor(model="gpt-4o")
    events = [e async for e in executor.run_turn([_user("hi")], [], "")]

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert [c.text for c in text_chunks] == ["Hello\n", "world\n"]
    assert len(completes) == 1
    assert completes[0].response == "Hello\nworld"
    assert completes[0].usage is None


async def test_run_turn_builds_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    argv_calls = _install_fake_aider(monkeypatch, stdout_lines=[b"ok\n"])
    executor = AiderExecutor(model="claude-3-5-sonnet")
    _ = [e async for e in executor.run_turn([_user("do it")], [], "")]

    argv = argv_calls[0]
    assert argv[0] == "aider"
    assert "--message" in argv
    assert "--yes-always" in argv
    assert "--no-stream" in argv
    assert "--no-pretty" in argv
    assert "--no-auto-commits" in argv
    assert "--no-check-update" in argv
    assert "--model" in argv and "claude-3-5-sonnet" in argv
    # First turn does not restore history.
    assert "--restore-chat-history" not in argv


async def test_system_prompt_folded_into_first_turn_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_calls = _install_fake_aider(monkeypatch, stdout_lines=[b"ok\n"])
    executor = AiderExecutor()

    _ = [e async for e in executor.run_turn([_user("first")], [], "BE HELPFUL")]
    _ = [e async for e in executor.run_turn([_user("second")], [], "BE HELPFUL")]

    first_msg = argv_calls[0][argv_calls[0].index("--message") + 1]
    second_msg = argv_calls[1][argv_calls[1].index("--message") + 1]
    assert "BE HELPFUL" in first_msg and "first" in first_msg
    # System prompt is not re-sent; second turn restores prior history instead.
    assert "BE HELPFUL" not in second_msg
    assert second_msg.strip() == "second"
    assert "--restore-chat-history" not in argv_calls[0]
    assert "--restore-chat-history" in argv_calls[1]


async def test_run_turn_empty_user_text_completes_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_aider(monkeypatch, stdout_lines=[b"unused\n"])
    executor = AiderExecutor()
    events = [e async for e in executor.run_turn([_user("   ")], [], "")]
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response == ""


# ---------------------------------------------------------------------------
# run_turn — error paths
# ---------------------------------------------------------------------------


async def test_run_turn_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aider_executor.shutil, "which", lambda _name: None)
    executor = AiderExecutor(aider_path="aider")
    events = [e async for e in executor.run_turn([_user("hi")], [], "")]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert events[0].retryable is False
    assert "aider-chat" in events[0].message


async def test_run_turn_nonzero_exit_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_aider(
        monkeypatch,
        stdout_lines=[b"partial\n"],
        stderr_lines=[b"boom: bad api key\n"],
        returncode=2,
    )
    executor = AiderExecutor()
    events = [e async for e in executor.run_turn([_user("hi")], [], "")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].retryable is False
    assert "exited with code 2" in errors[0].message
    assert "bad api key" in errors[0].message


# ---------------------------------------------------------------------------
# Harness wrap env plumbing
# ---------------------------------------------------------------------------


def test_build_aider_executor_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import aider_harness

    monkeypatch.setenv("HARNESS_AIDER_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("HARNESS_AIDER_PATH", "/opt/aider")
    monkeypatch.setenv("HARNESS_AIDER_GATEWAY_BASE_URL", "https://gw.example/v1")
    monkeypatch.setenv("HARNESS_AIDER_GATEWAY_AUTH_COMMAND", "printf '%s' sk-x")

    executor = aider_harness._build_aider_executor()
    assert isinstance(executor, AiderExecutor)
    assert executor._model == "gpt-4o-mini"
    assert executor._aider_path == "/opt/aider"
    assert executor._gateway_base_url == "https://gw.example/v1"
    assert executor._gateway_auth_command == "printf '%s' sk-x"


# ---------------------------------------------------------------------------
# Registry / allowlist / install / readiness wiring
# ---------------------------------------------------------------------------


def test_aider_in_harness_registry() -> None:
    from omnigent.runtime.harnesses import _HARNESS_MODULES

    assert _HARNESS_MODULES.get("aider") == "omnigent.inner.aider_harness"


def test_aider_in_harness_allowlist() -> None:
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

    assert "aider" in OMNIGENT_HARNESSES


def test_aider_install_spec_is_pip_not_npm() -> None:
    from omnigent.onboarding.harness_install import required_cli_for_harness

    spec = required_cli_for_harness("aider")
    assert spec is not None
    assert spec.binary == "aider"
    # pip-installed: no npm package, an install_hint instead (like cursor).
    assert spec.package is None
    assert "aider-chat" in (spec.install_hint or "")


def test_aider_provider_family_is_openai() -> None:
    from omnigent.onboarding.provider_config import OPENAI_FAMILY, provider_family_for_harness

    assert provider_family_for_harness("aider") == OPENAI_FAMILY


def test_aider_readiness_gates_on_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.onboarding import harness_readiness

    monkeypatch.setattr(harness_readiness, "harness_cli_installed", lambda _key: True)
    assert harness_readiness.harness_is_configured("aider") is True

    monkeypatch.setattr(harness_readiness, "harness_cli_installed", lambda _key: False)
    assert harness_readiness.harness_is_configured("aider") is False


# ---------------------------------------------------------------------------
# FastAPI app shape
# ---------------------------------------------------------------------------


def test_aider_harness_creates_fastapi_app() -> None:
    from omnigent.inner.aider_harness import create_app

    app = create_app()
    assert app is not None
    assert hasattr(app, "routes")
    health_routes = [r for r in app.routes if hasattr(r, "path") and "/health" in r.path]
    assert len(health_routes) > 0


def test_aider_harness_module_importable() -> None:
    from omnigent.inner import aider_harness

    assert hasattr(aider_harness, "create_app")


# ---------------------------------------------------------------------------
# Spawn-env dispatch wiring (workflow.py / runner.app / model_override)
# ---------------------------------------------------------------------------


def test_aider_in_workflow_spawn_env_dispatch() -> None:
    """aider must be wired into every workflow spawn-env routing table."""
    from omnigent.runtime import workflow

    assert workflow._PROVIDER_HARNESS_FAMILY["aider"] == "openai"
    assert workflow._HARNESS_GATEWAY_FLAG["aider"] == "HARNESS_AIDER_GATEWAY"
    assert workflow._HARNESS_DATABRICKS_PROFILE["aider"] == "HARNESS_AIDER_DATABRICKS_PROFILE"
    assert "aider" in workflow._UCODE_HARNESS_CONFIGS
    assert workflow._UCODE_HARNESS_CONFIGS["aider"].model_key == "HARNESS_AIDER_MODEL"
    assert callable(workflow._build_aider_spawn_env)


def test_aider_in_sdk_model_override_harnesses() -> None:
    """A per-session /model override must apply to the aider spawn-env harness."""
    from omnigent.model_override import _SDK_MODEL_OVERRIDE_HARNESSES

    assert "aider" in _SDK_MODEL_OVERRIDE_HARNESSES


def test_aider_model_env_key_in_runner_app() -> None:
    """runner.app maps aider -> HARNESS_AIDER_MODEL.

    Skipped where the runner module can't import (e.g. Windows lacks ``fcntl``);
    CI on Linux exercises it.
    """
    try:
        from omnigent.runner.app import _HARNESS_MODEL_ENV_KEY
    except (ImportError, AttributeError) as exc:  # pragma: no cover - platform-dependent
        pytest.skip(f"omnigent.runner.app not importable here: {exc}")
    assert _HARNESS_MODEL_ENV_KEY["aider"] == "HARNESS_AIDER_MODEL"
