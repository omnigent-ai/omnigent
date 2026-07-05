"""Tests for the ``harness: forge`` wrap and executor."""

from __future__ import annotations

import errno
import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner import forge_harness
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.forge_executor import (
    ForgeExecutor,
    _create_pty_subprocess_exec,
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
    raw = (
        "\x1b[2K⠋ Migrating credentials 00s · Ctrl+C to interrupt\r"
        "\x1b[2K● [16:56:51] Initialize f220280d-a20b-4f3a-b186-36111df3ee43\r"
        "\x1b[2KHello\n"
        "\x1b[2K● [16:56:53] Finished f220280d-a20b-4f3a-b186-36111df3ee43\r"
    )
    assert _strip_terminal_metadata(raw) == "Hello"
    assert _strip_terminal_metadata("● [05:05:11] ERROR: nope") == ""
    assert (
        _strip_terminal_metadata("Here is a literal line: ● [05:05:11] ERROR: nope")
        == "Here is a literal line: ● [05:05:11] ERROR: nope"
    )


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
        "omnigent.inner.forge_executor._create_pty_subprocess_exec",
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
    assert "stdin" not in create_proc.await_args.kwargs
    assert "stdout" not in create_proc.await_args.kwargs
    assert "stderr" not in create_proc.await_args.kwargs
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
        "omnigent.inner.forge_executor._create_pty_subprocess_exec",
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
        "omnigent.inner.forge_executor._create_pty_subprocess_exec",
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
async def test_run_turn_writes_forge_config_for_bare_model_with_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr("omnigent.inner.forge_executor.shutil.which", lambda _binary: "/bin/forge")
    monkeypatch.setattr(
        "omnigent.inner.forge_executor._configured_provider_id", lambda _dir: "codex"
    )
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"ok", b""))

    with patch(
        "omnigent.inner.forge_executor._create_pty_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        executor = ForgeExecutor(
            binary_path="forge", model="gpt-5.3-codex-spark", config_dir=str(tmp_path)
        )
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    config = (tmp_path / ".forge.toml").read_text()
    assert 'provider_id = "codex"' in config
    assert 'model_id = "gpt-5.3-codex-spark"' in config
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


def test_sandbox_launch_path_bare_binary_when_no_sandbox() -> None:
    os_env = OSEnvSpec(
        type="caller_process", cwd=None, sandbox=OSEnvSandboxSpec(type="none"), fork=False
    )
    assert ForgeExecutor(binary_path="forge")._sandbox_launch_path(()) == "forge"
    assert ForgeExecutor(binary_path="forge", os_env=os_env)._sandbox_launch_path(()) == "forge"


def test_sandbox_launch_path_wraps_when_sandbox_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.inner import sandbox as sandbox_mod

    class _ActivePolicy:
        active = True

    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", lambda *_a, **_k: _ActivePolicy())
    monkeypatch.setattr(sandbox_mod, "with_additional_read_roots", lambda s, _roots: s)
    monkeypatch.setattr(sandbox_mod, "with_additional_write_roots", lambda s, _roots: s)
    monkeypatch.setattr(sandbox_mod, "with_spawn_env_allowlist", lambda s, _names: s)
    monkeypatch.setattr(
        sandbox_mod, "create_exec_launcher", lambda target, _policy: f"LAUNCHER::{target}"
    )
    monkeypatch.setattr("omnigent.inner.forge_executor.shutil.which", lambda _binary: "/bin/forge")

    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
        fork=False,
    )
    launch = ForgeExecutor(binary_path="forge", os_env=os_env)._sandbox_launch_path(("PATH",))

    assert launch == "LAUNCHER::/bin/forge"


@pytest.mark.asyncio
async def test_run_turn_fails_closed_when_requested_sandbox_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.inner import sandbox as sandbox_mod

    class _ActivePolicy:
        active = True

    monkeypatch.setattr("omnigent.inner.forge_executor.shutil.which", lambda _binary: "/bin/forge")
    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", lambda *_a, **_k: _ActivePolicy())
    monkeypatch.setattr(sandbox_mod, "with_additional_read_roots", lambda s, _roots: s)
    monkeypatch.setattr(sandbox_mod, "with_additional_write_roots", lambda s, _roots: s)
    monkeypatch.setattr(sandbox_mod, "with_spawn_env_allowlist", lambda s, _names: s)
    monkeypatch.setattr(
        sandbox_mod,
        "create_exec_launcher",
        lambda _target, _policy: (_ for _ in ()).throw(OSError("launcher failed")),
    )

    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
        fork=False,
    )
    spawn = AsyncMock()
    with patch("omnigent.inner.forge_executor._create_pty_subprocess_exec", new=spawn):
        executor = ForgeExecutor(binary_path="forge", os_env=os_env)
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "could not apply requested sandbox" in events[0].message
    spawn.assert_not_awaited()


_FORKPTY_AVAILABLE = hasattr(os, "forkpty")


@pytest.mark.skipif(not _FORKPTY_AVAILABLE, reason="os.forkpty is unavailable")
@pytest.mark.asyncio
async def test_pty_spawn_drains_output_reaps_child_closes_fd_and_applies_cwd_env(
    tmp_path: Any,
) -> None:
    env = os.environ.copy()
    env["FORGE_PTY_TEST_ENV"] = "env-ok"
    code = (
        "import os, sys; "
        "print('cwd=' + os.getcwd()); "
        "print('env=' + os.environ['FORGE_PTY_TEST_ENV']); "
        "sys.stderr.write('stderr-line\\n'); "
        "print('stdout-line')"
    )

    process = await _create_pty_subprocess_exec(
        sys.executable,
        "-c",
        code,
        cwd=str(tmp_path),
        env=env,
    )
    master_fd = process.master_fd
    pid = process.pid

    stdout, stderr = await process.communicate()

    text = stdout.decode("utf-8", errors="replace")
    assert stderr == b""
    assert f"cwd={tmp_path}" in text
    assert "env=env-ok" in text
    assert "stdout-line" in text
    assert "stderr-line" in text
    assert process.returncode == 0
    with pytest.raises(OSError) as fd_error:
        os.fstat(master_fd)
    assert fd_error.value.errno == errno.EBADF
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.skipif(not _FORKPTY_AVAILABLE, reason="os.forkpty is unavailable")
@pytest.mark.asyncio
async def test_pty_linux_eio_on_master_read_is_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await _create_pty_subprocess_exec(
        sys.executable,
        "-c",
        "print('eio-eof-ok')",
    )
    original_read = os.read
    saw_eio = False

    def _tracking_read(fd: int, size: int) -> bytes:
        nonlocal saw_eio
        try:
            return original_read(fd, size)
        except OSError as exc:
            if fd == process.master_fd and exc.errno == errno.EIO:
                saw_eio = True
            raise

    monkeypatch.setattr(os, "read", _tracking_read)

    stdout, stderr = await process.communicate()

    assert stderr == b""
    assert b"eio-eof-ok" in stdout
    assert saw_eio
    assert process.returncode == 0


@pytest.mark.skipif(not _FORKPTY_AVAILABLE, reason="os.forkpty is unavailable")
@pytest.mark.asyncio
async def test_pty_spawn_reaps_child_when_parent_fd_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_forkpty = os.forkpty
    child_pid: int | None = None

    def _capturing_forkpty() -> tuple[int, int]:
        nonlocal child_pid
        pid, master_fd = original_forkpty()
        if pid != 0:
            child_pid = pid
        return pid, master_fd

    monkeypatch.setattr(os, "forkpty", _capturing_forkpty)
    monkeypatch.setattr("omnigent.inner.forge_executor.stat.S_ISCHR", lambda _mode: False)

    with pytest.raises(OSError, match="forkpty did not return a character device"):
        await _create_pty_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")

    assert child_pid is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pid, os.WNOHANG)
