"""Tests for the ``harness: forge`` wrap and executor."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner import forge_harness
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.forge_executor import (
    ForgeExecutor,
    _latest_user_text,
    _resolve_forge_binary,
    _split_model_override,
    _strip_terminal_metadata,
)
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_forge_in_module_registry() -> None:
    assert _HARNESS_MODULES.get("forge") == "omnigent.inner.forge_harness"


def test_forge_in_omnigent_harnesses_allowlist() -> None:
    assert "forge" in OMNIGENT_HARNESSES


def test_create_app_returns_fastapi_with_required_routes() -> None:
    app = forge_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FORGE_MODEL", "openai:gpt-5")
    monkeypatch.setenv("HARNESS_FORGE_CWD", "/tmp/forge-cwd")
    monkeypatch.setenv("HARNESS_FORGE_PATH", "/custom/bin/forge")
    monkeypatch.setenv("HARNESS_FORGE_AGENT", "forge")
    monkeypatch.setenv("HARNESS_FORGE_CONFIG_DIR", "/tmp/forge-config")

    captured: dict[str, Any] = {}

    with patch(
        "omnigent.inner.forge_harness.ForgeExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        forge_harness._build_forge_executor()

    assert captured["model"] == "openai:gpt-5"
    assert captured["cwd"] == "/tmp/forge-cwd"
    assert captured["binary_path"] == "/custom/bin/forge"
    assert captured["agent"] == "forge"
    assert captured["config_dir"] == "/tmp/forge-config"


def test_executor_factory_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HARNESS_FORGE_MODEL",
        "HARNESS_FORGE_CWD",
        "HARNESS_FORGE_PATH",
        "HARNESS_FORGE_AGENT",
        "HARNESS_FORGE_CONFIG_DIR",
        "OMNIGENT_RUNNER_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)

    captured: dict[str, Any] = {}

    with patch(
        "omnigent.inner.forge_harness.ForgeExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        forge_harness._build_forge_executor()

    assert captured["model"] is None
    assert captured["cwd"] is None
    assert captured["binary_path"] is None
    assert captured["agent"] is None
    assert captured["config_dir"] is None


def test_executor_factory_falls_back_to_runner_workspace_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARNESS_FORGE_CWD", raising=False)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/home/me/project")

    captured: dict[str, Any] = {}
    with patch(
        "omnigent.inner.forge_harness.ForgeExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        forge_harness._build_forge_executor()
    assert captured["cwd"] == "/home/me/project"


def test_malformed_os_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FORGE_OS_ENV", "{not-json")
    captured: dict[str, Any] = {}

    with patch(
        "omnigent.inner.forge_harness.ForgeExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        forge_harness._build_forge_executor()

    assert captured["os_env"].type == "caller_process"
    assert captured["os_env"].sandbox.type == "none"


def test_resolve_forge_binary_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_FORGE_PATH", raising=False)
    assert _resolve_forge_binary() == "forge"


def test_resolve_forge_binary_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FORGE_PATH", "/opt/bin/forge")
    assert _resolve_forge_binary() == "/opt/bin/forge"


def test_latest_user_text_content_blocks() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "input_text", "text": "second"}]},
    ]
    assert _latest_user_text(messages) == "second"


def test_strip_terminal_metadata_removes_ansi_spinner_and_errors() -> None:
    raw = "\x1b[2K⠋ Migrating credentials 00s · Ctrl+C to interrupt\r\x1b[2KHello\n"
    assert _strip_terminal_metadata(raw) == "Hello"
    assert _strip_terminal_metadata("● [05:05:11] ERROR: nope") == ""


def test_split_model_override() -> None:
    assert _split_model_override("openai:gpt-5") == ("openai", "gpt-5")
    assert _split_model_override("anthropic/claude-sonnet-4") == (
        "anthropic",
        "claude-sonnet-4",
    )
    assert _split_model_override("gpt-5") == (None, "gpt-5")


def test_build_argv_includes_prompt_directory_and_agent() -> None:
    executor = ForgeExecutor(binary_path="forge", cwd="/repo", agent="forge")
    assert executor._build_argv(prompt_text="Hi") == [
        "forge",
        "-p",
        "Hi",
        "-C",
        "/repo",
        "--agent",
        "forge",
    ]


@pytest.mark.asyncio
async def test_run_turn_returns_text_chunk_and_turn_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.inner.forge_executor.shutil.which", lambda _binary: "/bin/forge")
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"Hello, world!\n", b""))

    with patch(
        "omnigent.inner.forge_executor._create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ) as create_proc:
        executor = ForgeExecutor(binary_path="forge", cwd="/tmp")
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert create_proc.await_args.args[:5] == ("forge", "-p", "Hi", "-C", "/tmp")
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "Hello, world!"
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Hello, world!"


@pytest.mark.asyncio
async def test_run_turn_empty_output_is_error_even_with_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.inner.forge_executor.shutil.which", lambda _binary: "/bin/forge")
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(
        return_value=(
            b"",
            "\x1b[2K● [05:05:11] ERROR: No such device or address".encode(),
        )
    )

    with patch(
        "omnigent.inner.forge_executor._create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        executor = ForgeExecutor(binary_path="forge")
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no assistant output" in events[0].message


@pytest.mark.asyncio
async def test_run_turn_writes_forge_config_for_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr("omnigent.inner.forge_executor.shutil.which", lambda _binary: "/bin/forge")
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"ok", b""))

    with patch(
        "omnigent.inner.forge_executor._create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ) as create_proc:
        executor = ForgeExecutor(
            binary_path="forge", model="openai:gpt-5", config_dir=str(tmp_path)
        )
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    env = create_proc.await_args.kwargs["env"]
    assert env["FORGE_CONFIG"] == str(tmp_path)
    config = (tmp_path / ".forge.toml").read_text()
    assert 'provider_id = "openai"' in config
    assert 'model_id = "gpt-5"' in config
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_turn_empty_message_yields_none() -> None:
    executor = ForgeExecutor(binary_path="forge")
    events = []
    async for event in executor.run_turn(
        messages=[{"role": "assistant", "content": "Hello"}],
        tools=[],
        system_prompt="",
    ):
        events.append(event)
    assert events == [TurnComplete(response=None)]


def test_os_env_json_roundtrip_in_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HARNESS_FORGE_OS_ENV",
        json.dumps({"type": "caller_process", "sandbox": {"type": "none"}}),
    )
    captured: dict[str, Any] = {}
    with patch(
        "omnigent.inner.forge_harness.ForgeExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        forge_harness._build_forge_executor()
    assert captured["os_env"].sandbox.type == "none"
