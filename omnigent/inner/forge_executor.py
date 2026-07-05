"""ForgeCode CLI executor.

Drives the upstream ``forge`` CLI from https://forgecode.dev with one
``forge -p <prompt> -C <cwd>`` subprocess per Omnigent turn.

Verified in the local spike:

- ``-p/--prompt`` is the headless one-shot prompt mode.
- ``-C/--directory`` selects the working directory.
- ``--agent`` selects an agent.
- ``--conversation-id`` exists and accepts UUIDs, but semantic two-turn
  resume could not be verified without provider credentials.
- ``FORGE_CONFIG`` points to a config directory containing
  ``.forge.toml``.

Forge returned exit code 0 while printing credential/auth errors in the
spike, so this executor treats empty cleaned stdout as an error even when
the subprocess exits successfully.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import re
import shutil
import signal
import stat
import tempfile
import termios
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import tomllib

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)

_logger = logging.getLogger(__name__)

_FORGE_TURN_TIMEOUT_S = 600.0
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FORGE_STATUS_RE = re.compile(
    r"^[●!]\s+\[\d{2}:\d{2}:\d{2}\]\s+(?:Initialize|Finished)\s+"
    r"[0-9a-fA-F-]{36}$"
)
_FORGE_ERROR_RE = re.compile(r"^[●!]\s+\[\d{2}:\d{2}:\d{2}\]\s+ERROR:", re.IGNORECASE)


class SandboxLaunchError(RuntimeError):
    """Raised when a requested OS sandbox cannot wrap Forge."""


def _resolve_forge_binary() -> str:
    explicit = os.environ.get("HARNESS_FORGE_PATH", "").strip()
    if explicit:
        return explicit
    return "forge"


def _latest_user_text(messages: list[Message]) -> str:
    dropped_blocks = 0
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in ("text", "input_text") and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type in ("input_image", "input_file", "input_audio"):
                    dropped_blocks += 1
            if dropped_blocks:
                _logger.warning(
                    "forge harness: dropped %d non-text content block(s) on the latest user "
                    "message (multimodal input not yet wired)",
                    dropped_blocks,
                )
            return "".join(text_parts)
    return ""


def _strip_terminal_metadata(output: str) -> str:
    text = _ANSI_RE.sub("", output).replace("\r", "\n")
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "Ctrl+C to interrupt" in stripped:
            continue
        if _FORGE_STATUS_RE.match(stripped):
            continue
        if _FORGE_ERROR_RE.match(stripped):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _split_model_override(model: str) -> tuple[str | None, str]:
    """Split ``provider:model`` or ``provider/model`` into config fields."""
    for separator in (":", "/"):
        provider, sep, model_id = model.partition(separator)
        if sep and provider and model_id:
            return provider, model_id
    return None, model


def _configured_provider_id(config_dir: Path) -> str:
    for path in (config_dir / ".forge.toml", Path.home() / ".forge" / ".forge.toml"):
        with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            session = payload.get("session")
            if isinstance(session, dict):
                provider_id = session.get("provider_id")
                if isinstance(provider_id, str) and provider_id.strip():
                    return provider_id.strip()
    return "codex"


def _write_model_config(config_dir: Path, model: str) -> None:
    """Write the minimal Forge session model config.

    Forge requires both ``[session].provider_id`` and ``model_id``; a
    bare model id is paired with the configured default provider.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    provider_id, model_id = _split_model_override(model)
    provider_id = provider_id or _configured_provider_id(config_dir)
    lines = ["[session]", f'model_id = "{model_id}"']
    lines.insert(1, f'provider_id = "{provider_id}"')
    (config_dir / ".forge.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class ForgeExecutor(Executor):
    """Drive ``forge -p`` per Omnigent turn."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        binary_path: str | None = None,
        agent: str | None = None,
        config_dir: str | None = None,
    ) -> None:
        self._cwd = cwd
        self._os_env = os_env
        self._model = model
        self._binary_path = binary_path or _resolve_forge_binary()
        self._agent = agent
        self._config_dir = Path(config_dir) if config_dir else None
        self._temp_config_dir: Path | None = None
        self._active_process: _PtyProcess | None = None
        self._warned_tools_without_bridge = False

    def handles_tools_internally(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return False

    def _build_argv(self, *, prompt_text: str) -> list[str]:
        argv = [self._binary_path, "-p", prompt_text]
        if self._cwd:
            argv.extend(["-C", self._cwd])
        if self._agent:
            argv.extend(["--agent", self._agent])
        return argv

    def _sandbox_launch_path(self, spawn_env_names: Sequence[str]) -> str:
        """Return the forge binary or a sandbox launcher wrapping it."""
        os_env = self._os_env
        if os_env is None:
            return self._binary_path
        sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
        if sandbox_spec.type == "none":
            return self._binary_path
        try:
            from .sandbox import (
                create_exec_launcher,
                resolve_sandbox,
                with_additional_read_roots,
                with_additional_write_roots,
                with_spawn_env_allowlist,
            )

            cwd = Path(self._cwd or os.getcwd()).resolve(strict=False)
            sandbox = resolve_sandbox(os_env, cwd)
            if not sandbox.active:
                raise SandboxLaunchError("requested sandbox resolved to an inactive policy")
            resolved_bin = shutil.which(self._binary_path) or self._binary_path
            bin_dir = Path(resolved_bin).resolve(strict=False).parent
            sandbox = with_additional_read_roots(sandbox, [bin_dir])
            sandbox = with_additional_write_roots(sandbox, [Path.home() / ".forge", Path("/tmp")])
            sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
            return create_exec_launcher(resolved_bin, sandbox)
        except (OSError, ImportError, NotImplementedError, ValueError) as exc:
            raise SandboxLaunchError(
                f"could not apply requested sandbox for forge: {exc}"
            ) from exc

    def _config_path(self) -> Path | None:
        if self._config_dir is not None:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            return self._config_dir
        if self._model:
            if self._temp_config_dir is None:
                self._temp_config_dir = Path(tempfile.mkdtemp(prefix="forge_config_"))
            return self._temp_config_dir
        return None

    def _build_spawn_env(self) -> dict[str, str]:
        env = os.environ.copy()
        config_dir = self._config_path()
        if config_dir is not None:
            if self._model:
                _write_model_config(config_dir, self._model)
            env["FORGE_CONFIG"] = str(config_dir)
        return env

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,  # noqa: ARG002 - Forge owns its agent instructions
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        if tools and not self._warned_tools_without_bridge:
            _logger.warning(
                "forge executor received %d declared tool(s) but Omnigent has no "
                "tool-injection bridge for ForgeCode yet. Forge will use its native tools only.",
                len(tools),
            )
            self._warned_tools_without_bridge = True

        if shutil.which(self._binary_path) is None and not Path(self._binary_path).exists():
            yield ExecutorError(
                message=(
                    f"forge harness: binary {self._binary_path!r} not found on PATH. "
                    "Install via `curl -fsSL https://forgecode.dev/cli | sh` or set "
                    "HARNESS_FORGE_PATH to its absolute location."
                ),
                retryable=False,
            )
            return

        prompt_text = _latest_user_text(messages)
        if not prompt_text:
            yield TurnComplete(response=None)
            return

        model = (config.model if config else None) or self._model
        old_model = self._model
        self._model = model
        try:
            argv = self._build_argv(prompt_text=prompt_text)
            env = self._build_spawn_env()
            argv[0] = self._sandbox_launch_path(tuple(env.keys()))
        except SandboxLaunchError as exc:
            yield ExecutorError(message=str(exc), retryable=False)
            return
        finally:
            self._model = old_model

        process: _PtyProcess | None = None
        try:
            process = await _create_pty_subprocess_exec(
                *argv,
                cwd=self._cwd or None,
                env=env,
            )
            self._active_process = process
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=_FORGE_TURN_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            yield ExecutorError(
                message=f"forge subprocess timed out after {_FORGE_TURN_TIMEOUT_S}s",
                retryable=True,
            )
            return
        except FileNotFoundError:
            yield ExecutorError(
                message=(
                    f"forge harness: binary {self._binary_path!r} not found. "
                    "Install via `curl -fsSL https://forgecode.dev/cli | sh`."
                ),
                retryable=False,
            )
            return
        except OSError as exc:
            yield ExecutorError(message=f"failed to spawn forge subprocess: {exc}", retryable=True)
            return
        finally:
            self._active_process = None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        cleaned = _strip_terminal_metadata(stdout)
        raw_error = stderr.strip() or stdout.strip()

        if process.returncode not in (None, 0):
            yield ExecutorError(
                message=f"forge exited with code {process.returncode}: {raw_error[:500]}",
                retryable=False,
            )
            return
        if not cleaned:
            yield ExecutorError(
                message=f"forge produced no assistant output. stderr/stdout: {raw_error[:500]}",
                retryable=False,
            )
            return

        yield TextChunk(text=cleaned)
        yield TurnComplete(response=cleaned)

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        if self._temp_config_dir is not None:
            shutil.rmtree(self._temp_config_dir, ignore_errors=True)
            self._temp_config_dir = None
        await super().close()

    async def interrupt_session(self, session_key: str) -> bool:  # noqa: ARG002
        process = self._active_process
        if process is None or process.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
            return True
        return False

    async def enqueue_session_message(
        self,
        session_key: str,  # noqa: ARG002
        content: EnqueuedContent,  # noqa: ARG002
    ) -> bool:
        return False


class _PtyProcess:
    """Small process facade for a child spawned with ``forkpty``."""

    def __init__(self, pid: int, master_fd: int) -> None:
        self.pid = pid
        self.master_fd = master_fd
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        loop = asyncio.get_running_loop()
        output = bytearray()
        read_done = loop.create_future()

        def _read_ready() -> None:
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    if not read_done.done():
                        read_done.set_exception(exc)
                    return
                chunk = b""
            if chunk:
                output.extend(chunk)
                return
            loop.remove_reader(self.master_fd)
            if not read_done.done():
                read_done.set_result(None)

        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        loop.add_reader(self.master_fd, _read_ready)
        wait_task = asyncio.create_task(asyncio.to_thread(os.waitpid, self.pid, 0))
        try:
            pid, status_value = await wait_task
            if pid == self.pid:
                self.returncode = _returncode_from_wait_status(status_value)
            await read_done
        finally:
            loop.remove_reader(self.master_fd)
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
        return bytes(output), b""

    def terminate(self) -> None:
        os.kill(self.pid, signal.SIGTERM)


def _returncode_from_wait_status(status_value: int) -> int:
    if os.WIFEXITED(status_value):
        return os.WEXITSTATUS(status_value)
    if os.WIFSIGNALED(status_value):
        return -os.WTERMSIG(status_value)
    return 1


async def _create_pty_subprocess_exec(
    *args: Any,  # type: ignore[explicit-any]
    **kwargs: Any,  # type: ignore[explicit-any]
) -> _PtyProcess:
    """Spawn a process under a controlling PTY.

    Forge's ``-p`` path can stall when run without a terminal. ``forkpty``
    creates a session with the slave side as the child process's controlling
    terminal, while the parent asynchronously drains the master side.
    """
    cwd = kwargs.pop("cwd", None)
    env = kwargs.pop("env", None)
    if kwargs:
        raise TypeError(f"unsupported PTY spawn kwargs: {', '.join(sorted(kwargs))}")
    argv = [str(arg) for arg in args]
    pid, master_fd = os.forkpty()
    if pid == 0:
        try:
            if cwd is not None:
                os.chdir(cwd)
            with contextlib.suppress(AttributeError, OSError):
                termios.tcsetwinsize(0, (24, 120))
            executable = argv[0]
            exec_env = env if env is not None else os.environ.copy()
            os.execvpe(executable, argv, exec_env)
        except OSError:
            os._exit(127)
    mode = os.fstat(master_fd).st_mode
    if not stat.S_ISCHR(mode):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(ChildProcessError):
            await asyncio.to_thread(os.waitpid, pid, 0)
        os.close(master_fd)
        raise OSError("forkpty did not return a character device")
    return _PtyProcess(pid, master_fd)
