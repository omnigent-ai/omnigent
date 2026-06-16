"""CmdExecutor: run agents through Command Code's ``cmd --print`` mode.

Command Code ([commandcode.ai](https://commandcode.ai), npm ``command-code``,
binary ``cmd``) does not ship an Agent Client Protocol server. Its headless
surface is the ``--print`` flag documented in the [CLI reference](https://
commandcode.ai/docs/reference/cli): ``cmd --print [--yolo] [--model <id>]
[--max-turns <n>] "<prompt>"`` runs one prompt to completion and prints the
assistant's final response on stdout, then exits. There is no persistent
session to drive and no event stream to translate; this executor spawns one
``cmd`` subprocess per turn, streams its stdout line-by-line as ``TextChunk``
events, and yields ``TurnComplete`` on EOF.

Authentication is owned by the CLI (``cmd login`` against CommandCode.ai);
Command Code does not accept an API-key env var, so the harness threads no
credentials — like cursor / mimo, this executor does not route through the
Databricks AI gateway. The launch passes ``--yolo`` so a headless worker
auto-approves permission prompts (parity with the cursor ``3c5474f fix:
pass --yolo for headless permission auto-approval`` commit), and an optional
``--model <id>`` when the spec / ``/model`` override pins one. The system
prompt is prepended to the first turn (the cursor / mimo convention, via
the shared :func:`_build_cursor_prompt` helper).

Omnigent spec-declared tools are not bridged into ``cmd``; it uses its own
native tools (``handles_tools_internally() -> True``). ACP exposes no
mid-turn steer, and Command Code has no equivalent, so
``supports_live_message_queue() -> False``. No token usage is reported —
``cmd --print`` does not surface a usage payload — so turns are left
unpriced (``usage=None``).

Requirements:
    The ``cmd`` CLI must be installed and on ``PATH`` (or
    ``HARNESS_CMD_PATH`` set).
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
from typing import Any

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

# ``--max-turns`` default mirrors Command Code's own default for ``-p`` mode
# (documented in the CLI reference: "default ``10``. Exits with code 8 if the
# cap is hit before completion"). Pinned so a runaway session can't pin a
# worker forever; the executor surfaces the non-zero exit as an
# ``ExecutorError`` so the runner can decide whether to retry.
_DEFAULT_MAX_TURNS = 10

# Tail length of stderr included in the surfaced error when ``cmd`` exits
# non-zero. Mirrors the cursor executor's ``stderr_tail`` slice so the
# observable error shape is consistent across the ACP and non-ACP harnesses.
_STDERR_TAIL_CHARS = 2000

# Prefix-matched env var names allowed into the ``cmd`` subprocess. Only
# known-safe categories pass: Command Code's own config knobs, proxy / TLS
# settings, Node.js runtime knobs (``cmd`` ships a Node-based CLI), and
# locale. Credential families (``DATABRICKS_*``, ``AWS_*``, provider API
# keys, ...) deliberately do NOT match — mirrors the cursor / mimo / agy
# deny-by-default posture.
_CMD_ENV_ALLOW_PREFIXES: tuple[str, ...] = (
    "CMD_",
    "COMMANDCODE_",
    "COMMAND_CODE_",
    "HTTP_",
    "HTTPS_",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_",
    "NODE_",
    "XDG_",
    "LANG",
    "LC_",
)

# Exact-matched env var names allowed into the ``cmd`` subprocess: the
# minimal set a POSIX CLI reasonably expects (``HOME`` carries ``~/.cmd``
# login state, ``PATH`` is needed to find the binary, etc.).
_CMD_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
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


def _find_cmd() -> str | None:
    """Find the ``cmd`` CLI on ``PATH``."""
    return shutil.which("cmd")


def _clean_cmd_env(extra_allowed: Sequence[str] | None = None) -> dict[str, str]:
    """Build a filtered copy of ``os.environ`` for the ``cmd`` subprocess.

    Deny-by-default allowlist mirroring :func:`pi_executor._clean_pi_env`
    and the cursor / mimo / agy ``_clean_*_env`` helpers: only the
    known-safe prefixes and exact names pass, so host secrets (cloud
    tokens, unrelated API keys) never reach the ``cmd`` process. The
    ``CMD_`` / ``COMMANDCODE_`` / ``COMMAND_CODE_`` prefixes let Command
    Code's own config knobs through; Command Code owns its auth, so no
    explicit API-key injection is needed.

    :param extra_allowed: Extra exact names to pass through, e.g. a
        spec's ``os_env.sandbox.env_passthrough`` entries. ``None`` means
        no extras.
    :returns: Filtered environment dict.
    """
    allow_exact = set(_CMD_ENV_ALLOW_EXACT)
    if extra_allowed is not None:
        allow_exact.update(extra_allowed)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allow_exact or key.startswith(_CMD_ENV_ALLOW_PREFIXES)
    }


def _resolve_model(config: ExecutorConfig | None, override: str | None) -> str | None:
    """Resolve the model id forwarded to ``cmd --model``.

    ``cmd`` accepts any model id it knows about (Command Code's own catalog:
    Claude, GPT, Kimi, DeepSeek, GLM, Qwen, MiniMax, plus BYO-provider keys
    per the [docs](https://commandcode.ai/docs)). No denylist is applied —
    unlike cursor, a ``databricks-*`` id is *not* a known-bad value here,
    because ``cmd`` can route through a user-configured provider. A
    :class:`ExecutorConfig` model beats the executor's stored override; an
    explicit ``None`` means "let ``cmd`` pick its default".

    :param config: Per-turn :class:`ExecutorConfig` (may carry a ``/model``
        override), or ``None``.
    :param override: The executor's stored model override (from
        ``HARNESS_CMD_MODEL``), or ``None``.
    :returns: The model id to pass to ``--model``, or ``None`` to omit
        ``--model`` entirely.
    """
    cfg = config or ExecutorConfig()
    return cfg.model or override


def _build_argv(
    *,
    cmd_path: str,
    model: str | None,
    max_turns: int,
    prompt: str,
) -> list[str]:
    """Build the ``cmd`` argv for one turn.

    ``--print`` selects non-interactive (one-shot) mode; ``--yolo`` bypasses
    permission prompts (``--dangerously-skip-permissions`` is the long
    alias — pick the short one for terseness, matching the cursor commit
    `3c5474f fix(cursor): pass --yolo for headless permission auto-approval`);
    ``--max-turns`` mirrors Command Code's documented default cap; and
    ``--model`` is added only when the executor resolved a model id. The
    prompt is appended last as a single argv element (no shell).

    :param cmd_path: Absolute path to the ``cmd`` binary.
    :param model: Optional model id; ``None`` omits ``--model``.
    :param max_turns: Cap forwarded to ``--max-turns``; ``cmd`` exits
        with code 8 if it hits the cap before completing.
    :param prompt: The full prompt text (system + user, joined by the
        shared cursor-style prompt builder).
    :returns: The argv to pass to ``asyncio.create_subprocess_exec``.
    """
    argv: list[str] = [
        cmd_path,
        "--print",
        "--yolo",
        "--max-turns",
        str(max_turns),
    ]
    if model:
        argv.extend(["--model", model])
    argv.append(prompt)
    return argv


@dataclass
class _CmdSubprocessState:
    """One in-flight ``cmd --print`` subprocess for the current turn.

    ``cmd --print`` is per-turn: the process exits on its own when the
    assistant finishes, so this struct exists only to let
    :meth:`CmdExecutor.interrupt_session` kill a runaway subprocess when
    the runner signals cancellation. The struct is discarded at the end
    of each :meth:`run_turn`.
    """

    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task[bytes]


async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:  # type: ignore[explicit-any]
    """Indirection point for ``asyncio.create_subprocess_exec``.

    Exists so tests can stub subprocess creation without patching
    ``asyncio.create_subprocess_exec`` globally (a global patch leaks
    the mock into every other test in the process). Mirrors the
    pi-executor / codex-executor ``_create_subprocess_exec`` seam.

    :param args: Positional argv components forwarded to
        ``asyncio.create_subprocess_exec``.
    :param kwargs: Keyword args (``stdout``, ``stderr``, ``env``,
        ``cwd``, ...) forwarded as-is.
    :returns: The spawned subprocess handle.
    """
    return await asyncio.create_subprocess_exec(*args, **kwargs)


class CmdExecutor(Executor):
    """Execute agent turns via a per-turn ``cmd --print`` subprocess."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        cmd_path: str | None = None,
        max_turns: int = _DEFAULT_MAX_TURNS,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """Create a CmdExecutor.

        :param cwd: Working directory the ``cmd`` subprocess operates in.
            ``None`` falls back to ``os_env.cwd`` then the process cwd.
        :param os_env: Optional OS environment / sandbox spec (its ``cwd``
            is used when *cwd* is unset).
        :param model: Command Code model id, e.g. ``"claude-sonnet-4-6"``
            or ``"deepseek-v4-pro"``. ``None`` uses ``cmd``'s own default.
        :param cmd_path: Absolute path to ``cmd``. ``None`` searches
            ``PATH``.
        :param max_turns: Cap forwarded to ``cmd --max-turns``; mirrors
            Command Code's own default (``10``). ``cmd`` exits with
            code 8 if the cap is hit before completion, which surfaces
            here as a retryable :class:`ExecutorError`.
        :param bundle_dir: Reserved for future skill wiring; unused in v1.
        :param agent_name: Reserved for future use.
        :param skills_filter: Accepted for parity; ``cmd`` has no in-v1
            skill wiring here.
        :raises ImportError: When ``cmd`` is not found.
        """
        resolved = cmd_path or _find_cmd()
        if not resolved:
            raise ImportError(
                "CmdExecutor requires the 'cmd' CLI on PATH. "
                "Install it with: npm install -g command-code"
            )
        self._cmd_path = resolved
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        self._model_override = model
        self._max_turns = max_turns
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        passthrough = (
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        self._env: dict[str, str] = _clean_cmd_env(passthrough)
        # Per-turn subprocess handle; cleared at the end of run_turn so a
        # subsequent turn starts fresh. ``None`` when no turn is running.
        self._state: _CmdSubprocessState | None = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        # ``cmd --print`` is one-shot and exposes no mid-turn steer.
        return False

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002 — no per-session state
        return

    async def interrupt_session(self, session_key: str) -> bool:
        # ``cmd --print`` is one-shot; the only interrupt path is killing
        # the in-flight subprocess. Returns ``True`` only when a process
        # was actually running and got a SIGTERM — the runner treats a
        # ``False`` return as a no-op interrupt and continues.
        del session_key
        return await self._kill_inflight()

    async def close(self) -> None:
        await self._kill_inflight()

    async def _kill_inflight(self) -> bool:
        """Terminate the current ``cmd`` subprocess, if one is running.

        :returns: ``True`` when a process was running and was signalled
            (``SIGTERM`` first, ``SIGKILL`` as a backstop), else ``False``.
        """
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
                logger.debug("CmdExecutor: terminate timed out, sending SIGKILL")
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(Exception):  # drain best-effort
                await stderr_task
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 — cmd uses its own native tools in v1
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn via ``cmd --print``.

        Builds the prompt via the shared cursor-style helper (system +
        latest user text on subsequent turns, full history serialized on
        the first turn), spawns one ``cmd --print`` subprocess, and
        streams its stdout as :class:`TextChunk` events. Yields
        :class:`TurnComplete` on EOF (response text + ``usage=None`` —
        ``cmd`` does not report token usage) or :class:`ExecutorError`
        on a non-zero exit / spawn failure.

        The first-turn / subsequent-turn split is a no-op for ``cmd`` —
        each turn is a fresh process with no history — but the helper
        still gives the right prompt shape, so the system prompt is
        included on every turn rather than only the first. That matches
        Command Code's interactive-mode behavior (the system prompt is
        baked into the persona of the session, not the message) and
        keeps the helper's contract simple.

        :param messages: Omnigent conversation history for the turn.
        :param tools: Tool schemas from Omnigent. Ignored — ``cmd`` uses
            its own native tools.
        :param system_prompt: The Omnigent system prompt. Prepended to
            the prompt text on every turn (see above).
        :param config: Per-turn executor config (carries the per-session
            ``/model`` override, if any).
        :yields: :class:`TextChunk` events for each stdout line, then
            :class:`TurnComplete` on a clean exit or
            :class:`ExecutorError` on failure.
        """
        # A previous turn's state must not leak into this one. ``run_turn``
        # is sequential per executor, but be defensive — if anything is
        # still inflight (e.g. a prior error left it alive), kill it first.
        await self._kill_inflight()

        model = _resolve_model(config, self._model_override)
        # ``cmd --print`` is one-shot: every turn is a fresh process with no
        # carried state, so each turn is effectively a first turn. Driving the
        # shared cursor helper with ``is_first_turn=True`` always prepends the
        # system prompt and serializes any prior history, which is exactly the
        # shape ``cmd`` needs (it bakes the system prompt into the headless
        # turn rather than a session field).
        prompt = _build_cursor_prompt(
            messages,
            is_first_turn=True,
            system_prompt=system_prompt,
        )
        if not prompt:
            yield TurnComplete(response=None)
            return

        argv = _build_argv(
            cmd_path=self._cmd_path,
            model=model,
            max_turns=self._max_turns,
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
            yield ExecutorError(message=f"Failed to start cmd --print: {exc}")
            return

        # Drain stderr in a sibling task so a chatty ``cmd`` never blocks
        # on a full pipe. The collected bytes are attached to the error
        # message on a non-zero exit, mirroring the cursor executor's
        # stderr-tail pattern.
        stderr_task = asyncio.create_task(_drain_stream(process.stderr))
        self._state = _CmdSubprocessState(process=process, stderr_task=stderr_task)

        response_text = ""
        assert process.stdout is not None
        try:
            async for line in process.stdout:
                decoded = line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                # ``cmd --print`` emits the assistant's final text on
                # stdout. The CLI does not separate reasoning from
                # answer, so every non-empty line is forwarded as a
                # text delta. Blank lines are kept out of the response
                # text (we'd otherwise see spurious leading whitespace
                # when the CLI emits a leading newline) but still count
                # as a chunk boundary, so the streaming UX is preserved.
                if decoded:
                    response_text += decoded + "\n"
                    yield TextChunk(text=decoded)
        except asyncio.CancelledError:
            # Runner asked us to stop. Kill the subprocess and let the
            # adapter surface the cancellation; do not yield a final
            # event (the runner owns the cancel marker).
            await self._kill_inflight()
            raise
        finally:
            # ``_kill_inflight`` clears ``self._state``; if the turn
            # ended cleanly (no interrupt), the state is still pointing
            # at the just-finished process — clear it now so the next
            # turn starts from ``None``.
            if self._state is not None and self._state.process is process:
                self._state = None

        exit_code = await process.wait()
        # Drain stderr now that the process is gone so we can attach the
        # tail to a non-zero-exit error.
        try:
            stderr_bytes = await stderr_task
        except Exception:  # noqa: BLE001 — drain failures must not mask the real error
            stderr_bytes = b""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if exit_code != 0:
            suffix = f" Stderr: {stderr_text[-_STDERR_TAIL_CHARS:]}" if stderr_text else ""
            # Exit code 8 is Command Code's documented "max-turns hit"
            # signal (per the CLI reference); treat as retryable so the
            # runner can decide whether to bump the cap and try again.
            retryable = exit_code == 8
            yield ExecutorError(
                message=f"cmd --print exited with code {exit_code}.{suffix}",
                retryable=retryable,
            )
            return

        # Strip the trailing newline we appended after each non-empty
        # line so the rendered transcript doesn't end with a blank.
        response = response_text.rstrip("\n") or None
        yield TurnComplete(response=response, usage=None)


async def _drain_stream(
    stream: asyncio.streams.StreamReader | None,
) -> bytes:
    """Read a subprocess stream to EOF and return the bytes.

    :param stream: The :class:`asyncio.StreamReader` to drain, or
        ``None`` when the subprocess was created without piping this
        stream (treated as empty).
    :returns: The accumulated bytes (empty when *stream* is ``None``).
    """
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
