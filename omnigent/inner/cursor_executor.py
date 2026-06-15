"""Executor for Cursor Agent CLI headless mode."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict

from ._subprocess_lifecycle import close_subprocess_transport
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
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

_STREAM_READ_CHUNK_SIZE = 4096


async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*args, **kwargs)


def _find_cursor_cli() -> str | None:
    return shutil.which("cursor-agent")


def _clean_cursor_env(env_passthrough: Sequence[str] | None = None) -> dict[str, str]:
    allowed = {
        "CURSOR_API_KEY",
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "USER",
        "SHELL",
    }
    if env_passthrough:
        allowed.update(env_passthrough)
    return {key: value for key, value in os.environ.items() if key in allowed}


@dataclass(frozen=True)
class SandboxedCursorCli:
    launch_path: str
    sandboxed: bool


def _try_sandbox_cursor(
    cursor_path: str,
    os_env: OSEnvSpec | None,
    cwd: str | None,
    spawn_env_names: Sequence[str] | None = None,
) -> SandboxedCursorCli:
    if os_env is None:
        return SandboxedCursorCli(launch_path=cursor_path, sandboxed=False)
    sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
    if sandbox_spec.type == "none":
        return SandboxedCursorCli(launch_path=cursor_path, sandboxed=False)
    try:
        import pathlib

        from .sandbox import (
            create_exec_launcher,
            resolve_sandbox,
            with_additional_read_roots,
            with_additional_write_roots,
            with_spawn_env_allowlist,
        )

        resolved_cwd = pathlib.Path(cwd or os.getcwd()).resolve(strict=False)
        sandbox = resolve_sandbox(os_env, resolved_cwd)
        if not sandbox.active:
            return SandboxedCursorCli(launch_path=cursor_path, sandboxed=False)
        cursor_dir = pathlib.Path(cursor_path).resolve().parent.parent
        sandbox = with_additional_read_roots(sandbox, [cursor_dir])
        sandbox = with_additional_write_roots(
            sandbox,
            [pathlib.Path(os.path.expanduser("~/.cursor")), pathlib.Path("/tmp")],
        )
        sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
        launcher = create_exec_launcher(cursor_path, sandbox)
        return SandboxedCursorCli(launch_path=launcher, sandboxed=True)
    except (OSError, ImportError, NotImplementedError) as exc:
        logger.warning("Could not apply sandbox for Cursor: %s", exc)
        return SandboxedCursorCli(launch_path=cursor_path, sandboxed=False)


def _extract_latest_user_text(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return " ".join(parts)
        if content is not None:
            return str(content)
    return ""


def _text_from_cursor_message(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _cursor_usage(event: dict[str, Any], fallback_model: str | None) -> dict[str, Any] | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("inputTokens") or 0)
    output_tokens = int(usage.get("outputTokens") or 0)
    cache_read = int(usage.get("cacheReadTokens") or 0)
    cache_write = int(usage.get("cacheWriteTokens") or 0)
    if not (input_tokens or output_tokens):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens + cache_read + cache_write,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "context_tokens": input_tokens + cache_read + cache_write,
        "model": fallback_model,
    }


class CursorExecutor(Executor):
    """Execute turns via ``cursor-agent --print --output-format stream-json``."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        cursor_path: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        resolved_cursor = cursor_path or _find_cursor_cli()
        if not resolved_cursor:
            raise ImportError(
                "CursorExecutor requires the 'cursor-agent' CLI on PATH. "
                "Install Cursor Agent and authenticate with `cursor-agent login`."
            )
        self._cursor_path = resolved_cursor
        self._cwd = cwd
        self._model = model
        self._agent_name = agent_name
        self._os_env_spec = os_env
        self._env = _clean_cursor_env(
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        sandboxed = _try_sandbox_cursor(
            self._cursor_path,
            os_env,
            cwd,
            spawn_env_names=[*self._env],
        )
        self._cursor_launch_path = sandboxed.launch_path
        self._sandboxed = sandboxed.sandboxed

    def supports_streaming(self) -> bool:
        return True

    def _resolve_model(self, config: ExecutorConfig | None) -> str | None:
        if config is not None and config.model:
            return config.model
        return self._model

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        del tools
        prompt = _extract_latest_user_text(messages)
        if not prompt:
            yield TurnComplete(response=None)
            return

        model = self._resolve_model(config)
        args = [
            self._cursor_launch_path,
            "--print",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--trust",
        ]
        if model:
            args.extend(["--model", model])
        workspace = self._cwd or os.getcwd()
        args.extend(["--workspace", workspace])
        if system_prompt:
            prompt = f"{system_prompt}\n\n{prompt}"
        args.append(prompt)

        process = await _create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=self._env,
        )

        stderr_task = asyncio.create_task(process.stderr.read() if process.stderr else _empty_bytes())
        response_text = ""
        streamed_any = False
        usage: dict[str, Any] | None = None
        observed_model = model
        error_message: str | None = None
        try:
            assert process.stdout is not None
            async for raw in _iter_stream_lines(process.stdout):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if line:
                        yield TextChunk(text=line)
                        response_text += line
                        streamed_any = True
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "system":
                    raw_model = event.get("model")
                    if isinstance(raw_model, str) and raw_model:
                        observed_model = raw_model
                elif event_type == "assistant":
                    text = _text_from_cursor_message(event.get("message"))
                    if text:
                        # With --stream-partial-output, Cursor emits timestamped
                        # assistant chunks followed by one non-timestamped
                        # final assistant message containing the full text.
                        # Without partial streaming, only that final message is
                        # emitted. Stream chunks as deltas; treat the final as
                        # reconciliation to avoid duplicating text.
                        if "timestamp_ms" in event:
                            yield TextChunk(text=text)
                            response_text += text
                            streamed_any = True
                        elif not streamed_any:
                            yield TextChunk(text=text)
                            response_text = text
                            streamed_any = True
                        else:
                            response_text = text
                elif event_type == "text":
                    text = event.get("text") or event.get("delta")
                    if isinstance(text, str) and text:
                        yield TextChunk(text=text)
                        response_text += text
                        streamed_any = True
                elif event_type == "result":
                    result_text = event.get("result")
                    if isinstance(result_text, str) and result_text and not response_text:
                        response_text = result_text
                    usage = _cursor_usage(event, observed_model)
                    if event.get("is_error"):
                        error_message = (
                            result_text if isinstance(result_text, str) and result_text else None
                        )
                        break
        finally:
            await process.wait()
            close_subprocess_transport(process)

        stderr = await stderr_task
        if error_message is not None:
            yield ExecutorError(message=error_message or "Cursor agent returned an error")
            return
        if process.returncode not in (0, None):
            detail = stderr.decode("utf-8", errors="replace").strip()
            yield ExecutorError(message=f"Cursor agent failed: {detail or process.returncode}")
            return
        _notify_usage_from_dict(model=observed_model, usage=usage)
        yield TurnComplete(response=response_text, usage=usage)


async def _empty_bytes() -> bytes:
    return b""


async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    buffer = bytearray()
    while True:
        chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            line = bytes(buffer[: newline_index + 1])
            del buffer[: newline_index + 1]
            yield line
    if buffer:
        yield bytes(buffer)
