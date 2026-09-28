"""Drive the ZCode CLI in non-interactive print mode."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from omnigent.inner import _proc
from omnigent.inner._subprocess_lifecycle import close_subprocess_transport
from omnigent.inner.agent_env import clean_agent_env, declared_passthrough
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnCancelled,
    TurnComplete,
)
from omnigent.inner.native_attachments import attachment_cache_dir, materialize_attachment
from omnigent.inner.zcode_models import ZCodeModelError, normalize_mode
from omnigent.inner.zcode_stream import (
    PermissionNotice,
    ReasoningDelta,
    ResultSummary,
    StreamError,
    TextDelta,
    ToolUpdate,
    parse_stream_line,
)

_logger = logging.getLogger(__name__)

_TURN_TIMEOUT_S = 600.0
_EXIT_TIMEOUT_S = 2.0
_STDERR_DRAIN_TIMEOUT_S = 1.0
_STDERR_LIMIT = 64 * 1024
_STREAM_LIMIT = 16 * 1024 * 1024
_ATTACHMENT_PROMPT = "(see attachments)"
_CANCEL_EXIT_CODES = {129, 130, 143, -1, -2, -15}
_ZCODE_EXACT_ENV = (
    "ZAI_OAUTH_ORIGIN",
    "ZAI_BUSINESS_BASE_URL",
    "ZAI_OAUTH_CLIENT_ID",
    "BIGMODEL_API_BASE_URL",
)


def build_zcode_args(
    zcode_path: str,
    prompt: str,
    *,
    cwd: str,
    session_id: str | None = None,
    mode: str = "yolo",
    attachments: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
) -> list[str]:
    """Build argv for one headless ZCode turn."""
    args = [zcode_path, "--cwd", cwd]
    if session_id:
        args.extend(["--resume", session_id])
    args.extend(["--mode", mode, "--output-format", "stream-json", "-p", prompt])
    for path in attachments or []:
        args.extend(["--attach", path])
    if disallowed_tools:
        args.append("--disallowed-tools")
        args.extend(disallowed_tools)
    return args


def _last_user_content(messages: list[Message]) -> object:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def _session_key(messages: list[Message]) -> str:
    for message in messages:
        value = message.get("session_id")
        if isinstance(value, str) and value:
            return value
    return str(hash(tuple((m.get("role"), str(m.get("content"))[:200]) for m in messages)))


def _text_and_blocks(content: object) -> tuple[str, list[Mapping[str, object]]]:
    if isinstance(content, str):
        return content, []
    text: list[str] = []
    attachments: list[Mapping[str, object]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            value = block.get("text")
            if isinstance(value, str):
                text.append(value)
            if block.get("type") in {
                "image",
                "file",
                "attachment",
                "input_image",
                "input_file",
                "input_audio",
            }:
                attachments.append(block)
    return "\n".join(text), attachments


class ZCodeExecutor(Executor):
    """Executor that runs ``zcode -p`` once per turn."""

    def __init__(
        self,
        zcode_path: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        os_env: OSEnvSpec | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> None:
        self._zcode_path = zcode_path or shutil.which("zcode") or "zcode"
        self._cwd = str(Path(cwd or os.getcwd()).resolve())
        self._model = model
        self._mode = mode
        self._os_env = os_env or OSEnvSpec(
            type="caller_process", sandbox=OSEnvSandboxSpec(type="none")
        )
        self._disallowed_tools = list(disallowed_tools or [])
        self._session_map: dict[str, str] = {}
        self._system_prompt_sent: set[str] = set()
        self._attachment_keys: dict[str, Path] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._active_session_key: str | None = None
        self._cancel_requested = False

    def supports_streaming(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def _build_spawn_env(self) -> dict[str, str]:
        return clean_agent_env(
            allow_prefixes=("ZCODE_",),
            allow_exact=_ZCODE_EXACT_ENV,
            extra_allowed=declared_passthrough(self._os_env),
        )

    def _sandbox_launch_path(
        self, env_names: Sequence[str], extra_read_roots: Sequence[Path] = ()
    ) -> str:
        sandbox_spec = self._os_env.sandbox or OSEnvSandboxSpec()
        if sandbox_spec.type == "none":
            return self._zcode_path
        try:
            from omnigent.inner.sandbox import (
                create_exec_launcher,
                resolve_sandbox,
                with_additional_read_roots,
                with_spawn_env_allowlist,
            )

            sandbox = resolve_sandbox(self._os_env, Path(self._cwd))
            if not sandbox.active:
                return self._zcode_path
            binary = Path(self._zcode_path)
            if binary.parent != Path("."):
                sandbox = with_additional_read_roots(sandbox, [binary.resolve().parent])
            if extra_read_roots:
                sandbox = with_additional_read_roots(sandbox, extra_read_roots)
            # The sandbox cannot hide bootstrap credentials from ZCode's tools.
            # Do not widen it to ~/.zcode or /tmp; callers may grant a narrow path.
            sandbox = with_spawn_env_allowlist(sandbox, env_names)
            return create_exec_launcher(self._zcode_path, sandbox)
        except (ImportError, NotImplementedError, OSError) as exc:
            raise ZCodeModelError(f"Could not apply the configured ZCode sandbox: {exc}") from exc

    def _attachment_key(self, session_key: str) -> Path:
        # Identifies this session's cache under ~/.omnigent/attachments/; never created.
        key = self._attachment_keys.get(session_key)
        if key is None:
            key = Path(self._cwd, f".omnigent-zcode-{uuid.uuid4().hex}")
            self._attachment_keys[session_key] = key
        return key

    def _attachment_paths(self, session_key: str, blocks: list[Mapping[str, object]]) -> list[str]:
        if not blocks:
            return []
        key = self._attachment_key(session_key)
        owned_dir = attachment_cache_dir(key)
        cwd = Path(self._cwd)
        paths: list[str] = []
        for block in blocks:
            raw = (
                block.get("path")
                or block.get("url")
                or block.get("image_url")
                or block.get("file_data")
            )
            if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                raise ValueError(
                    "Remote ZCode attachments are unsupported; upload the file to Omnigent first"
                )
            if isinstance(raw, str) and raw.startswith("data:"):
                path = materialize_attachment(block, key)
                if path is None:
                    raise ValueError("Could not decode ZCode attachment data URI")
                paths.append(str(path.resolve()))
                continue
            if not isinstance(raw, str) or not raw:
                raise ValueError("ZCode attachment has no local path or data URI")
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            resolved = candidate.resolve()
            if not (resolved.is_relative_to(cwd) or resolved.is_relative_to(owned_dir)):
                raise ValueError(
                    "ZCode attachment path must be inside cwd or its session attachment cache"
                )
            paths.append(str(resolved))
        return paths

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        del tools
        content = _last_user_content(messages)
        text, blocks = _text_and_blocks(content)
        if not text and not blocks:
            yield TurnComplete(response=None)
            return
        model = (config.model if config else None) or self._model
        if model:
            yield ExecutorError(
                message=(
                    "ZCode print mode does not support model overrides because "
                    "/model is unsupported"
                ),
                retryable=False,
            )
            return
        try:
            extra_mode = config.extra.get("mode") if config else None
            mode = normalize_mode(extra_mode or self._mode)
            session_key = _session_key(messages)
            attachments = self._attachment_paths(session_key, blocks)
        except (ValueError, ZCodeModelError) as exc:
            yield ExecutorError(message=str(exc), retryable=False)
            return

        prompt = text or _ATTACHMENT_PROMPT
        if session_key not in self._system_prompt_sent and system_prompt:
            prompt = f"{system_prompt}\n\n{prompt}"
        async for event in self._run_prompt(
            prompt,
            session_key=session_key,
            session_id=self._session_map.get(session_key),
            mode=mode,
            attachments=attachments,
        ):
            if isinstance(event, TurnComplete):
                self._system_prompt_sent.add(session_key)
            yield event

    async def _run_prompt(
        self,
        prompt: str,
        *,
        session_key: str,
        session_id: str | None,
        mode: str,
        attachments: list[str],
    ) -> AsyncIterator[ExecutorEvent]:
        env = self._build_spawn_env()
        key = self._attachment_keys.get(session_key)
        cache_roots = [attachment_cache_dir(key)] if attachments and key is not None else []
        try:
            launch_path = self._sandbox_launch_path(tuple(env), cache_roots)
        except ZCodeModelError as exc:
            yield ExecutorError(message=str(exc), retryable=False)
            return
        args = build_zcode_args(
            launch_path,
            prompt,
            cwd=self._cwd,
            session_id=session_id,
            mode=mode,
            attachments=attachments,
            disallowed_tools=self._disallowed_tools,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
                limit=_STREAM_LIMIT,
                **_proc.spawn_kwargs(),
            )
        except FileNotFoundError:
            _cleanup_launcher(launch_path, self._zcode_path)
            yield ExecutorError(
                message=(
                    f"ZCode CLI not found at {self._zcode_path!r}. Install ZCode so `zcode` "
                    "is on PATH, or set OMNIGENT_ZCODE_PATH."
                ),
                retryable=False,
            )
            return
        except OSError as exc:
            _cleanup_launcher(launch_path, self._zcode_path)
            yield ExecutorError(message=f"Failed to spawn ZCode: {exc}", retryable=True)
            return

        self._proc = proc
        self._active_session_key = session_key
        self._cancel_requested = False
        assert proc.stdout is not None and proc.stderr is not None
        stderr = bytearray()
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr))
        streamed = ""
        summary: ResultSummary | None = None
        stream_error: StreamError | None = None
        tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}  # type: ignore[explicit-any]
        timed_out = False
        try:
            async with asyncio.timeout(_TURN_TIMEOUT_S):
                while line := await proc.stdout.readline():
                    event = parse_stream_line(line.decode("utf-8", errors="replace"))
                    if event is None:
                        continue
                    if isinstance(event, PermissionNotice):
                        _logger.info(
                            "ZCode headless permission decision: tool=%s request_id=%s",
                            event.tool_name,
                            event.request_id,
                        )
                        continue
                    if isinstance(event, TextDelta):
                        streamed += event.text
                        yield TextChunk(text=event.text)
                    elif isinstance(event, ReasoningDelta):
                        yield ReasoningChunk(delta=event.text, event_type="reasoning_text")
                    elif isinstance(event, ToolUpdate):
                        tool_event = _tool_event(event, tool_calls)
                        if tool_event is not None:
                            yield tool_event
                    elif isinstance(event, StreamError):
                        stream_error = event
                    elif isinstance(event, ResultSummary):
                        summary = event
                await proc.wait()
        except TimeoutError:
            timed_out = True
        finally:
            if proc.returncode is None:
                _proc.terminate_tree(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_EXIT_TIMEOUT_S)
                except TimeoutError:
                    _proc.kill_tree(proc)
                    with contextlib.suppress(Exception):
                        await proc.wait()
            try:
                await asyncio.wait_for(
                    asyncio.shield(stderr_task), timeout=_STDERR_DRAIN_TIMEOUT_S
                )
            except TimeoutError:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            if self._proc is proc:
                self._proc = None
                self._active_session_key = None
            close_subprocess_transport(proc)
            _cleanup_launcher(launch_path, self._zcode_path)

        if self._cancel_requested:
            yield TurnCancelled()
            return
        if timed_out:
            yield ExecutorError(
                message=f"ZCode subprocess timed out after {_TURN_TIMEOUT_S}s", retryable=True
            )
            return
        if proc.returncode in _CANCEL_EXIT_CODES:
            yield TurnCancelled()
            return
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stream_error is not None or proc.returncode not in {0, None}:
            detail = stream_error.message if stream_error is not None else stderr_text
            if not detail and summary is not None:
                detail = summary.response
            yield ExecutorError(
                message=f"ZCode exited with code {proc.returncode}: {detail[-500:]}",
                retryable=True,
            )
            return
        if summary is not None and summary.session_id:
            self._session_map[session_key] = summary.session_id
        response = summary.response if summary and summary.response else streamed
        if response and not streamed:
            yield TextChunk(text=response)
        yield TurnComplete(
            response=response or None,
            usage=summary.usage if summary and summary.usage else None,
        )

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        _proc.terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_EXIT_TIMEOUT_S)
        except TimeoutError:
            _proc.kill_tree(proc)
            with contextlib.suppress(Exception):
                await proc.wait()

    async def interrupt_session(self, session_key: str) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None or self._active_session_key != session_key:
            return False
        self._cancel_requested = True
        await self._stop_process(proc)
        return True

    async def close_session(self, session_key: str) -> None:
        if self._active_session_key == session_key and self._proc is not None:
            self._cancel_requested = True
            await self._stop_process(self._proc)
        self._session_map.pop(session_key, None)
        self._system_prompt_sent.discard(session_key)
        key = self._attachment_keys.pop(session_key, None)
        if key is not None:
            shutil.rmtree(attachment_cache_dir(key), ignore_errors=True)
        await super().close_session(session_key)

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._cancel_requested = True
            await self._stop_process(self._proc)
        self._session_map.clear()
        self._system_prompt_sent.clear()
        for key in self._attachment_keys.values():
            shutil.rmtree(attachment_cache_dir(key), ignore_errors=True)
        self._attachment_keys.clear()
        await super().close()


def _tool_event(
    update: ToolUpdate,
    calls: dict[str, tuple[str, dict[str, Any]]],  # type: ignore[explicit-any]
) -> ExecutorEvent | None:
    metadata: dict[str, Any] = {"internally_executed": True}  # type: ignore[explicit-any]
    if update.call_id:
        metadata["call_id"] = update.call_id
    if update.phase == "scheduled":
        if update.call_id:
            calls[update.call_id] = (update.name, update.args)
        return ToolCallRequest(name=update.name, args=update.args, metadata=metadata)
    if update.phase == "started":
        return None
    name = update.name
    if update.call_id and update.call_id in calls:
        name = calls.pop(update.call_id)[0]
    if update.phase == "result":
        return ToolCallComplete(
            name=name,
            status=ToolCallStatus.SUCCESS,
            result=update.result,
            metadata=metadata,
        )
    if update.phase == "error":
        return ToolCallComplete(
            name=name,
            status=ToolCallStatus.ERROR,
            error=update.error,
            metadata=metadata,
        )
    return None


async def _drain_stderr(stream: asyncio.StreamReader, output: bytearray) -> None:
    while chunk := await stream.read(4096):
        output.extend(chunk)
        if len(output) > _STDERR_LIMIT:
            del output[: len(output) - _STDERR_LIMIT]


def _cleanup_launcher(launch_path: str, zcode_path: str) -> None:
    if launch_path == zcode_path:
        return
    with contextlib.suppress(OSError):
        Path(launch_path).unlink()
