"""Mistral Vibe CLI executor.

Drives the upstream ``vibe`` CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolArgs,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)

_logger = logging.getLogger(__name__)

_STREAM_LIMIT = 16 * 1024 * 1024

def _resolve_vibe_binary() -> str:
    explicit = os.environ.get("HARNESS_VIBE_PATH", "").strip()
    if explicit:
        return explicit
    return "vibe"

def _latest_user_text(messages: list[Message]) -> str:
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
            return "".join(text_parts)
    return ""

class VibeExecutor(Executor):
    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        agent: str | None = None,
        binary_path: str | None = None,
    ) -> None:
        self._cwd = cwd
        self._os_env = os_env
        self._agent = agent
        self._binary_path = binary_path or _resolve_vibe_binary()

        self._session_id: str | None = None
        self._is_first_turn: bool = True
        self._warned_tools_without_bridge: bool = False
        self._active_process: asyncio.subprocess.Process | None = None

    def handles_tools_internally(self) -> bool:
        return True

    def forwards_observed_tool_results(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def _build_spawn_env(self) -> dict[str, str]:
        return dict(os.environ)

    def _sandbox_launch_path(self, spawn_env_names: Sequence[str]) -> str:
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
                return self._binary_path
            resolved_bin = shutil.which(self._binary_path) or self._binary_path
            bin_dir = Path(resolved_bin).resolve(strict=False).parent
            sandbox = with_additional_read_roots(sandbox, [bin_dir])
            sandbox = with_additional_write_roots(sandbox, [Path.home() / ".vibe", Path("/tmp")])
            sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
            return create_exec_launcher(resolved_bin, sandbox)
        except (OSError, ImportError, NotImplementedError) as exc:
            _logger.warning("Could not apply sandbox for vibe; running unsandboxed: %s", exc)
            return self._binary_path

    def _build_argv(self, *, prompt_text: str) -> list[str]:
        argv: list[str] = [
            self._binary_path,
            "--output",
            "streaming",
        ]

        if self._agent:
            argv.extend(["--agent", self._agent])

        if self._session_id:
            argv.extend(["--resume", self._session_id])
        elif not self._is_first_turn:
            argv.append("-c")

        self._is_first_turn = False

        argv.extend(["-p", prompt_text])
        return argv

    def _translate_event(self, payload: dict[str, Any]) -> list[ExecutorEvent]:  # type: ignore[explicit-any]
        events: list[ExecutorEvent] = []
        # Schema verified against Mistral Vibe's vibe.core.types.LLMMessage
        # (Vibe does not emit session_id in its streaming JSON payload)
        role = payload.get("role")
        if role == "assistant":
            content = payload.get("content")
            if isinstance(content, str) and content:
                events.append(TextChunk(text=content))
                
            tool_calls = payload.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    if not isinstance(fn, dict):
                        continue
                    name = fn.get("name") or ""
                    raw_args = fn.get("arguments")
                    args: ToolArgs = {}
                    if isinstance(raw_args, str):
                        with contextlib.suppress(json.JSONDecodeError):
                            parsed = json.loads(raw_args)
                            if isinstance(parsed, dict):
                                args = parsed
                    elif isinstance(raw_args, dict):
                        args = raw_args
                    call_id = call.get("id") or ""
                    if name:
                        events.append(
                            ToolCallRequest(
                                name=name,
                                args=args,
                                metadata={"call_id": call_id} if call_id else {},
                            )
                        )
        elif role == "tool" or ("tool_call_id" in payload):
            result = payload.get("content")
            call_id = payload.get("tool_call_id") or ""
            events.append(
                ToolCallComplete(
                    name="",
                    status=ToolCallStatus.SUCCESS,
                    result=result,
                    metadata={"call_id": call_id} if call_id else {},
                )
            )

        # Look for session_id in any message
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id

        return events

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,  # noqa: ARG002
        config: ExecutorConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[ExecutorEvent]:

        if tools and not self._warned_tools_without_bridge:
            _logger.warning(
                "vibe executor received %d declared tool(s) but Omnigent has no "
                "tool-injection bridge for the upstream vibe binary. The tools "
                "will not be exposed to vibe for this session.",
                len(tools),
            )
            self._warned_tools_without_bridge = True

        if shutil.which(self._binary_path) is None and not Path(self._binary_path).exists():
            yield ExecutorError(
                message=f"vibe harness: binary {self._binary_path!r} not found on PATH.",
                retryable=False,
            )
            return

        prompt_text = _latest_user_text(messages)
        if not prompt_text:
            yield TurnComplete(response=None)
            return

        argv = self._build_argv(prompt_text=prompt_text)
        env = self._build_spawn_env()
        argv[0] = self._sandbox_launch_path(tuple(env.keys()))

        started_at = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        stderr_buf = bytearray()
        any_text_emitted = False
        final_text_parts: list[str] = []
        try:
            process = await _create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd or None,
                env=env,
                limit=_STREAM_LIMIT,
            )
            self._active_process = process

            assert process.stdout is not None
            assert process.stderr is not None

            async def _drain_stderr() -> None:
                assert process is not None and process.stderr is not None
                while True:
                    chunk = await process.stderr.read(4096)
                    if not chunk:
                        return
                    stderr_buf.extend(chunk)

            stderr_task = asyncio.create_task(_drain_stderr())
            try:
                async for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        _logger.debug("vibe executor: non-JSON stdout line: %s", line[:200])
                        continue
                    if not isinstance(payload, dict):
                        continue
                    for event in self._translate_event(payload):
                        if isinstance(event, TextChunk):
                            any_text_emitted = True
                            final_text_parts.append(event.text)
                        yield event
            finally:
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
        except asyncio.CancelledError:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            raise
        finally:
            self._active_process = None
            if process is not None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()

        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        if process is not None and process.returncode not in (None, 0):
            stderr_text = stderr_buf.decode("utf-8", errors="replace")
            yield ExecutorError(
                message=(
                    f"vibe exited with code {process.returncode} after "
                    f"{elapsed_ms:.0f}ms. stderr: {stderr_text.strip()[:500]}"
                ),
                retryable=False,
            )
            return

        yield TurnComplete(
            response="".join(final_text_parts) if any_text_emitted else None,
        )

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002
        self._session_id = None
        self._is_first_turn = True

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

async def _create_subprocess_exec(  # type: ignore[explicit-any]
    *args: Any,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*args, **kwargs)
