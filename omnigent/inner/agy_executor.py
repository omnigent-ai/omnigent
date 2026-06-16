"""AgyExecutor: run agents through Antigravity CLI's ``agy --print`` mode.

Antigravity CLI (binary ``agy``) does not expose an ACP server, so this
executor uses its one-shot print surface: one ``agy --print`` subprocess per
turn, stdout streamed back as text chunks, and no persistent session state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .cmd_executor import _STDERR_TAIL_CHARS, _create_subprocess_exec, _drain_stream
from .cursor_executor import _build_cursor_prompt
from .datamodel import OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)

AGY_DEFAULT_MODEL = "Gemini 3.1 Pro (High)"
_DEFAULT_PRINT_TIMEOUT = "30m"

_AGY_ENV_ALLOW_PREFIXES: tuple[str, ...] = (
    "AGY_",
    "ANTIGRAVITY_",
    "GEMINI_",
    "GOOGLE_",
    "HTTP_",
    "HTTPS_",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_",
    "XDG_",
    "LANG",
    "LC_",
)

_AGY_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "TZ",
    }
)


def _find_agy() -> str | None:
    """Find the ``agy`` CLI on ``PATH``."""
    return shutil.which("agy")


def _clean_agy_env(extra_allowed: Sequence[str] | None = None) -> dict[str, str]:
    """Build a filtered copy of ``os.environ`` for the ``agy`` subprocess."""
    allow_exact = set(_AGY_ENV_ALLOW_EXACT)
    if extra_allowed is not None:
        allow_exact.update(extra_allowed)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allow_exact or key.startswith(_AGY_ENV_ALLOW_PREFIXES)
    }


def _sandbox_enabled(os_env: OSEnvSpec | None) -> bool:
    sandbox = os_env.sandbox if os_env is not None else None
    return not (sandbox is None or sandbox.type == "none")


def _build_argv(
    *,
    agy_path: str,
    model: str,
    print_timeout: str,
    sandbox: bool,
    prompt: str,
) -> list[str]:
    """Build the ``agy`` argv for one non-interactive turn."""
    argv = [
        agy_path,
        "--print",
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--print-timeout",
        print_timeout,
    ]
    if sandbox:
        argv.append("--sandbox")
    argv.append(prompt)
    return argv


@dataclass
class _AgySubprocessState:
    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task[bytes]


class AgyExecutor(Executor):
    """Execute agent turns via a per-turn ``agy --print`` subprocess."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        agy_path: str | None = None,
        print_timeout: str = _DEFAULT_PRINT_TIMEOUT,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        resolved = agy_path or _find_agy()
        if not resolved:
            raise ImportError(
                "AgyExecutor requires the 'agy' CLI on PATH. "
                "Install it with: curl -fsSL https://antigravity.google/cli/install.sh | bash"
            )
        self._agy_path = resolved
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        self._model = model or AGY_DEFAULT_MODEL
        self._print_timeout = print_timeout or _DEFAULT_PRINT_TIMEOUT
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        passthrough = (
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        self._env = _clean_agy_env(passthrough)
        self._state: _AgySubprocessState | None = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return False

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002
        return

    async def interrupt_session(self, session_key: str) -> bool:
        del session_key
        return await self._kill_inflight()

    async def close(self) -> None:
        await self._kill_inflight()

    async def _kill_inflight(self) -> bool:
        state = self._state
        if state is None:
            return False
        process = state.process
        stderr_task = state.stderr_task
        self._state = None
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.debug("AgyExecutor: terminate timed out, sending SIGKILL")
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(Exception):
                await stderr_task
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 - agy uses its own native tools
        system_prompt: str,
        config: ExecutorConfig | None = None,  # noqa: ARG002 - model is fixed at process launch
    ) -> AsyncIterator[ExecutorEvent]:
        await self._kill_inflight()

        prompt = _build_cursor_prompt(messages, is_first_turn=True, system_prompt=system_prompt)
        if not prompt:
            yield TurnComplete(response=None)
            return

        argv = _build_argv(
            agy_path=self._agy_path,
            model=self._model,
            print_timeout=self._print_timeout,
            sandbox=_sandbox_enabled(self._os_env_spec),
            prompt=prompt,
        )
        cwd = self._cwd or os.getcwd()

        try:
            process = await _create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=self._env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            yield ExecutorError(message=f"Failed to start agy --print: {exc}")
            return

        stderr_task = asyncio.create_task(_drain_stream(process.stderr))
        self._state = _AgySubprocessState(process=process, stderr_task=stderr_task)

        response_text = ""
        assert process.stdout is not None
        try:
            async for line in process.stdout:
                decoded = line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if decoded:
                    response_text += decoded + "\n"
                    yield TextChunk(text=decoded)
        except asyncio.CancelledError:
            await self._kill_inflight()
            raise
        finally:
            if self._state is not None and self._state.process is process:
                self._state = None

        exit_code = await process.wait()
        try:
            stderr_bytes = await stderr_task
        except Exception:  # noqa: BLE001
            stderr_bytes = b""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if exit_code != 0:
            suffix = f" Stderr: {stderr_text[-_STDERR_TAIL_CHARS:]}" if stderr_text else ""
            yield ExecutorError(
                message=f"agy --print exited with code {exit_code}.{suffix}",
                retryable=False,
            )
            return

        yield TurnComplete(response=response_text.rstrip("\n") or None, usage=None)
