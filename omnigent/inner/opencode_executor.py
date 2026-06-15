"""Inner :class:`Executor` adapter for the OpenCode CLI.

OpenCode (https://opencode.ai) is a TypeScript / Bun coding agent
that ships as a single ``opencode`` binary. This executor drives it
in non-interactive mode via ``opencode run --format json`` and
translates the line-delimited JSON event stream into the inner
:mod:`omnigent.inner.executor` vocabulary that :class:`ExecutorAdapter`
expects.

Design choices (v0):

- **Per-turn subprocess.** Each :meth:`run_turn` spawns a fresh
  ``opencode run`` invocation rather than maintaining a long-lived
  ``opencode serve`` process. This trades startup cost per turn for
  far simpler state management. The HTTP / SSE transport is a
  natural follow-up once the contract is firm.
- **Session resume.** OpenCode persists sessions server-side keyed
  by a string ID passed via ``--session <id>``. We capture the
  session ID emitted on the first turn's event stream and store it
  keyed by the Omnigent ``session_key`` so subsequent turns
  reattach the same conversation.
- **Tools are internal.** OpenCode has its own built-in tools
  (bash, edit, read, etc.) and runs them inside its own loop. We
  set :meth:`handles_tools_internally` to ``True`` so the Session
  layer treats ``tool_use`` events as informational and does not
  attempt to re-execute them.

Env var contract lives in :mod:`omnigent.inner.opencode_harness`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from typing import Any

from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallMetadata,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)

# Env-var keys read by the wrap (see opencode_harness.py for the
# contract). Centralising as module-level constants gives a single
# grep target if naming ever shifts.
_ENV_MODEL = "HARNESS_OPENCODE_MODEL"
_ENV_AGENT = "HARNESS_OPENCODE_AGENT"
_ENV_CWD = "HARNESS_OPENCODE_CWD"
_ENV_OPENCODE_PATH = "HARNESS_OPENCODE_PATH"
_ENV_VARIANT = "HARNESS_OPENCODE_VARIANT"
_ENV_THINKING = "HARNESS_OPENCODE_THINKING"
_ENV_SKIP_PERMISSIONS = "HARNESS_OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS"

# Gateway-routing env vars — when set, the executor synthesises an
# ``OPENCODE_CONFIG_CONTENT`` JSON payload that overrides the chosen
# provider's ``baseURL`` / ``apiKey`` per ``packages/opencode/src/
# config/config.ts`` (the ``Flag.OPENCODE_CONFIG_CONTENT`` merge path).
# This keeps gateway routing per-invocation and out of the user's
# global ``~/.config/opencode/config.json``.
_ENV_GATEWAY_PROVIDER = "HARNESS_OPENCODE_GATEWAY_PROVIDER"
_ENV_GATEWAY_BASE_URL = "HARNESS_OPENCODE_GATEWAY_BASE_URL"
_ENV_GATEWAY_API_KEY = "HARNESS_OPENCODE_GATEWAY_API_KEY"

# MCP-bridge env var: a JSON object of ``{server_name: ConfigMCPV1.Info}``
# entries merged into ``OPENCODE_CONFIG_CONTENT``'s ``mcp`` map per
# ``packages/opencode/src/config/config.ts`` so OpenCode connects to
# Omnigent's tool dispatch endpoint as an MCP server. v0 reads this
# raw rather than synthesising it inside the executor — the producer
# (workflow.py / the harness wrap) decides whether and how to start
# the MCP endpoint and writes the URL here.
_ENV_MCP_SERVERS = "HARNESS_OPENCODE_MCP_SERVERS"

# Set together with ``OPENCODE_CONFIG_CONTENT``: tells OpenCode to
# skip walking the cwd for ``opencode.json`` so user-project config
# never silently overrides the per-session settings the executor
# generated. The global ``~/.config/opencode/`` is still merged
# (OpenCode has no flag to suppress it), so producers that need a
# truly isolated config should also write a temp global config dir
# via ``OPENCODE_CONFIG_DIR`` before spawning the harness subprocess.
_OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
_OPENCODE_DISABLE_PROJECT_CONFIG_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"

# OpenCode emits one JSON object per line on stdout when invoked
# with ``--format json``. Per packages/opencode/src/cli/cmd/run.ts
# the event envelope is::
#
#     {
#       "type":      "text" | "reasoning" | "tool_use"
#                  | "step_start" | "step_finish" | "error",
#       "timestamp": <ms epoch>,
#       "sessionID": "<string>",
#       ...payload
#     }
#
# We treat the type field as the only required dispatcher; unknown
# event types are logged-and-dropped so an OpenCode release that
# adds a new event kind doesn't crash this harness.
_TEXT_EVENT = "text"
_REASONING_EVENT = "reasoning"
_TOOL_USE_EVENT = "tool_use"
_STEP_START_EVENT = "step_start"
_STEP_FINISH_EVENT = "step_finish"
_ERROR_EVENT = "error"

# When the subprocess produces no output for this long we log a
# warning but keep waiting — a long-running tool or model call can
# legitimately block events for many minutes.
_TURN_EVENT_WARN_SECONDS = 600.0

# Bounded stderr capture: enough to surface the failure reason on a
# non-zero exit without growing without bound for chatty builds.
_STDERR_CHUNK_LIMIT = 65536


def _parse_truthy(raw: str | None) -> bool:
    """Decode the truthy-env-var convention used across harness wraps.

    Matches the parser in ``pi_harness`` / ``codex_harness`` so a
    user who already knows ``HARNESS_PI_GATEWAY=1`` gets the same
    semantics here.

    :param raw: The raw env-var value or ``None`` when unset.
    :returns: ``True`` for ``"1"``, ``"true"``, ``"yes"``,
        ``"on"`` (case-insensitive). Anything else is ``False``,
        including ``None`` and empty string.
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_opencode_binary() -> str:
    """Return the absolute path to the ``opencode`` binary.

    Resolution order:

    1. ``HARNESS_OPENCODE_PATH`` env var when set.
    2. ``shutil.which("opencode")`` on the inherited ``PATH``.

    :returns: Path suitable for ``asyncio.create_subprocess_exec``.
    :raises FileNotFoundError: No ``opencode`` binary located.
    """
    explicit = os.environ.get(_ENV_OPENCODE_PATH, "").strip()
    if explicit:
        return explicit
    found = shutil.which("opencode")
    if not found:
        raise FileNotFoundError(
            "opencode CLI not found on PATH. Install it from "
            "https://opencode.ai or set HARNESS_OPENCODE_PATH to "
            "the binary path."
        )
    return found


def _latest_user_text(messages: list[Message]) -> str:
    """Extract the most recent user message as plain text.

    OpenCode's ``--session <id>`` flag restores its own
    server-side conversation history, so we only need to forward
    the latest user turn rather than replay the entire transcript.
    This mirrors how a TUI user would type their next message into
    the running session.

    Multimodal blocks (input_image, input_file) are dropped in v0
    with a warning — wiring them through the CLI requires file
    materialisation that we can add once base streaming lands.

    :param messages: The Responses-API style ``input`` items the
        adapter built from the request body.
    :returns: The latest user message rendered as a single string;
        empty when no user message is present.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"input_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif block.get("type") in {"input_image", "input_file", "input_audio"}:
                    logger.warning(
                        "opencode v0 harness: dropping %s block; multimodal "
                        "input not yet plumbed through the CLI.",
                        block.get("type"),
                    )
            if parts:
                return "\n".join(parts)
        # Unknown content shape — keep scanning earlier messages.
    return ""


def _build_argv(
    binary: str,
    *,
    session_id: str | None,
    model: str | None,
    cwd_flag: str | None,
    agent: str | None,
    variant: str | None,
    thinking: bool,
    skip_permissions: bool,
    prompt: str,
) -> list[str]:
    """Assemble the argv for ``opencode run``.

    Flag set verified against ``packages/opencode/src/cli/cmd/run.ts``
    in the upstream repo. Booleans are positional ``--flag`` only when
    enabled — never ``--flag=true``.

    :returns: argv ready for :func:`asyncio.create_subprocess_exec`.
    """
    argv: list[str] = [binary, "run", "--format", "json"]
    if session_id:
        argv.extend(["--session", session_id])
    if model:
        argv.extend(["--model", model])
    if agent:
        argv.extend(["--agent", agent])
    if variant:
        argv.extend(["--variant", variant])
    if cwd_flag:
        argv.extend(["--dir", cwd_flag])
    if thinking:
        argv.append("--thinking")
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    argv.append(prompt)
    return argv


def _translate_event(
    payload: dict[str, Any],
    *,
    emit_reasoning: bool,
) -> list[ExecutorEvent]:
    """Translate one OpenCode ``--format json`` event into inner events.

    Mapping (per ``packages/opencode/src/cli/cmd/run.ts``):

    - ``text`` → :class:`TextChunk` with the part's text delta.
    - ``reasoning`` → :class:`ReasoningChunk` when ``--thinking``
      is enabled, otherwise dropped.
    - ``tool_use`` → :class:`ToolCallRequest` + matching
      :class:`ToolCallComplete` so the adapter can emit the
      ``function_call`` / ``function_call_output`` pair that
      Omnigent re-pairs into an observed tool call.
    - ``error`` → :class:`ExecutorError`.
    - ``step_start`` / ``step_finish`` → dropped (informational).

    Unknown event types log-and-drop rather than raise; OpenCode
    is a moving target and a new event kind shouldn't break a turn.

    :param payload: One parsed JSON event from stdout.
    :param emit_reasoning: When ``False``, reasoning events are
        dropped so harnesses without ``--thinking`` enabled don't
        accidentally surface empty reasoning chunks.
    :returns: Zero or more :class:`ExecutorEvent` instances.
    """
    event_type = payload.get("type")
    part = payload.get("part") if isinstance(payload.get("part"), dict) else {}

    if event_type == _TEXT_EVENT:
        text = part.get("text")
        if isinstance(text, str) and text:
            return [TextChunk(text=text)]
        return []

    if event_type == _REASONING_EVENT:
        if not emit_reasoning:
            return []
        text = part.get("text")
        if isinstance(text, str) and text:
            return [ReasoningChunk(delta=text, event_type="reasoning_text")]
        return []

    if event_type == _TOOL_USE_EVENT:
        tool_name = part.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []
        tool_input = part.get("input")
        args: dict[str, Any] = tool_input if isinstance(tool_input, dict) else {}
        call_id = part.get("id") or part.get("callID")
        metadata: ToolCallMetadata = {"call_id": call_id} if call_id else {}
        # OpenCode runs tools inside its own loop; by the time it
        # emits a tool_use event the call already completed.
        # Surface as a paired request + complete so the adapter
        # treats it as an observed (not action-required) call.
        tool_output = part.get("output")
        status_str = part.get("status") if isinstance(part.get("status"), str) else None
        status = ToolCallStatus.SUCCESS
        if status_str == "error":
            status = ToolCallStatus.ERROR
        elif status_str == "cancelled":
            status = ToolCallStatus.CANCELLED
        return [
            ToolCallRequest(name=tool_name, args=args, metadata=metadata),
            ToolCallComplete(
                name=tool_name,
                status=status,
                result=tool_output,
                error=part.get("error") if status != ToolCallStatus.SUCCESS else None,
                duration_ms=0.0,
                metadata=metadata,
            ),
        ]

    if event_type == _ERROR_EVENT:
        err = payload.get("error")
        if isinstance(err, str):
            message = err
        elif err:
            message = json.dumps(err)
        else:
            message = "opencode reported an unspecified error"
        return [ExecutorError(message=f"opencode: {message}", retryable=False)]

    if event_type in (_STEP_START_EVENT, _STEP_FINISH_EVENT):
        return []

    logger.warning("opencode harness: dropping unknown event type %r", event_type)
    return []


def _resolve_mcp_servers_env() -> dict[str, Any]:
    """Decode ``HARNESS_OPENCODE_MCP_SERVERS`` into the OpenCode MCP map.

    The env var is a JSON object whose entries match OpenCode's
    ``ConfigMCPV1.Info`` shape — typically::

        {
          "omnigent": {
            "type":    "remote",
            "url":     "http://127.0.0.1:<port>/mcp",
            "headers": {"Authorization": "Bearer ..."}
          }
        }

    Producers that want to suppress a user's globally-registered MCP
    server (because it would leak into orchestrator-driven turns) can
    include an entry like ``{"<server>": {"enabled": false}}`` —
    OpenCode's ``Config.layer`` merge preserves disabled entries.

    A malformed value RAISES rather than silently dropping, since the
    env var is written by the parent omnigent process and a bad value
    is a producer-side bug.

    :returns: The decoded map, or empty when the env var is unset.
    :raises ValueError: When the env var is set but isn't a JSON
        object.
    """
    raw = os.environ.get(_ENV_MCP_SERVERS, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{_ENV_MCP_SERVERS} is set but is not valid JSON ({exc}). "
            "Producers writing this env var must emit a JSON object."
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{_ENV_MCP_SERVERS} decoded to {type(parsed).__name__}; "
            "expected a JSON object keyed by MCP server name."
        )
    return parsed


def _build_opencode_config_content() -> dict[str, Any] | None:
    """Synthesise the ``OPENCODE_CONFIG_CONTENT`` payload, if any.

    Combines the gateway-routing env vars and the MCP-bridge env var
    into one JSON-serialisable dict matching OpenCode's
    ``ConfigV1.Info`` schema. Returns ``None`` when nothing is
    configured — callers pass through without setting the env var so
    OpenCode's normal config-discovery path runs unchanged.

    Gateway override (per
    ``packages/opencode/src/provider/provider.ts`` ``resolveSDK``)
    flows through ``provider.<id>.options.baseURL`` and
    ``provider.<id>.options.apiKey``. ``HARNESS_OPENCODE_GATEWAY_PROVIDER``
    chooses the provider id; the default ``"anthropic"`` matches the
    most common gateway shape (Databricks AI gateway →
    Anthropic-compatible endpoint).
    """
    base_url = os.environ.get(_ENV_GATEWAY_BASE_URL, "").strip()
    api_key = os.environ.get(_ENV_GATEWAY_API_KEY, "").strip()
    mcp_servers = _resolve_mcp_servers_env()

    if not base_url and not api_key and not mcp_servers:
        return None

    payload: dict[str, Any] = {}

    if base_url or api_key:
        provider_id = os.environ.get(_ENV_GATEWAY_PROVIDER, "").strip() or "anthropic"
        options: dict[str, Any] = {}
        if base_url:
            options["baseURL"] = base_url
        if api_key:
            options["apiKey"] = api_key
        payload["provider"] = {provider_id: {"options": options}}

    if mcp_servers:
        payload["mcp"] = mcp_servers

    return payload


class OpenCodeExecutor(Executor):
    """Drive the OpenCode CLI as a per-turn subprocess.

    One :class:`OpenCodeExecutor` instance is shared across all
    Omnigent sessions hosted by the harness subprocess. Per-session
    state — currently just the OpenCode session ID captured on the
    first turn — lives in ``self._session_ids`` keyed by the
    Omnigent ``session_key`` passed through :meth:`close_session`.
    """

    def __init__(self) -> None:
        """Read configuration from env vars at construction time."""
        self._binary: str | None = None
        self._model = os.environ.get(_ENV_MODEL, "").strip() or None
        self._agent = os.environ.get(_ENV_AGENT, "").strip() or None
        self._cwd = os.environ.get(_ENV_CWD, "").strip() or None
        self._variant = os.environ.get(_ENV_VARIANT, "").strip() or None
        self._thinking = _parse_truthy(os.environ.get(_ENV_THINKING))
        # Default to True: a meta-harness running OpenCode headlessly
        # has nowhere to surface permission prompts. The env var lets
        # an operator opt back into interactive permissions when they
        # know they're attaching the OpenCode TUI somewhere.
        skip_raw = os.environ.get(_ENV_SKIP_PERMISSIONS)
        self._skip_permissions = True if skip_raw is None else _parse_truthy(skip_raw)
        self._session_ids: dict[str, str] = {}
        # One-time guard for the "spec tools declared but no MCP
        # bridge wired" warning — see run_turn.
        self._warned_no_mcp_bridge = False

    def _resolve_binary(self) -> str:
        """Locate ``opencode`` lazily and cache the path."""
        if self._binary is None:
            self._binary = _resolve_opencode_binary()
        return self._binary

    def _build_spawn_env(self) -> dict[str, str]:
        """Return the env mapping for the per-turn ``opencode run`` subprocess.

        Inherits the harness subprocess's environment so user PATH /
        XDG vars / shell config stay accessible, then layers in the
        gateway / MCP overrides that the producer wrote into the
        ``HARNESS_OPENCODE_GATEWAY_*`` and ``HARNESS_OPENCODE_MCP_*``
        env vars. The resulting ``OPENCODE_CONFIG_CONTENT`` is merged
        on top of the user's global config by ``Config.layer`` in
        OpenCode itself — so this is non-destructive for the
        operator's ``~/.config/opencode/config.json``.

        ``OPENCODE_DISABLE_PROJECT_CONFIG=1`` is set unconditionally
        when we generate a config override so a user-project
        ``opencode.json`` cannot silently re-introduce the
        provider/MCP entries the producer wanted suppressed.
        """
        env = dict(os.environ)
        payload = _build_opencode_config_content()
        if payload is None:
            return env
        env[_OPENCODE_CONFIG_CONTENT_ENV] = json.dumps(payload, separators=(",", ":"))
        env[_OPENCODE_DISABLE_PROJECT_CONFIG_ENV] = "1"
        return env

    def _session_key_for(self, messages: list[Message]) -> str | None:
        """Pull the Omnigent session_key off the inbound messages, if present.

        ``ExecutorAdapter`` stamps ``session_id`` onto each message
        when forwarding to the inner executor (see
        ``_executor_adapter.py``). We use it to key our per-session
        OpenCode-session-ID cache so a multi-turn conversation
        reattaches the same OpenCode session.

        :returns: The first ``session_id`` field found, or ``None``.
        """
        for message in messages:
            sid = message.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return None

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one OpenCode turn.

        ``system_prompt`` is accepted for ABI compatibility but
        ignored — OpenCode loads instructions from its own
        configuration (``opencode auth`` + ``opencode.json``).

        ``tools`` is forwarded by the adapter but **not yet bridged
        into the OpenCode subprocess** in this build. The producer
        side (workflow.py spawn-env builder) can populate
        ``HARNESS_OPENCODE_MCP_SERVERS`` to register an Omnigent
        MCP endpoint that exposes spec tools — that env var is
        consumed by :func:`_build_opencode_config_content` and
        flows into ``OPENCODE_CONFIG_CONTENT.mcp``. The in-process
        FastMCP server that would serve that endpoint is intentionally
        out of scope for v1 (it needs careful lifecycle and
        :attr:`_tool_executor` callback plumbing); when ``tools`` is
        non-empty and no MCP server has been wired we log a one-time
        warning so the user knows their spec tools aren't reaching
        OpenCode.
        """
        del system_prompt
        if tools and not os.environ.get(_ENV_MCP_SERVERS) and not self._warned_no_mcp_bridge:
            logger.warning(
                "opencode harness: %d spec tool(s) declared but no Omnigent "
                "MCP server is registered (HARNESS_OPENCODE_MCP_SERVERS is "
                "unset). Tools will not be exposed to OpenCode. Set the env "
                "var to an Omnigent-served MCP endpoint to bridge them.",
                len(tools),
            )
            self._warned_no_mcp_bridge = True
        binary = self._resolve_binary()
        omnigent_session_key = self._session_key_for(messages)
        opencode_session_id = (
            self._session_ids.get(omnigent_session_key) if omnigent_session_key else None
        )

        prompt = _latest_user_text(messages)
        if not prompt:
            yield ExecutorError(
                message="opencode harness: no user message in request",
                retryable=False,
            )
            return

        # Per-turn model override (config.model) wins over the
        # harness default, matching the supervisor/codex executors.
        model = self._model
        if config is not None and config.model:
            model = config.model

        argv = _build_argv(
            binary,
            session_id=opencode_session_id,
            model=model,
            cwd_flag=self._cwd,
            agent=self._agent,
            variant=self._variant,
            thinking=self._thinking,
            skip_permissions=self._skip_permissions,
            prompt=prompt,
        )

        logger.info(
            "opencode harness: spawning %s (session=%s, model=%s, cwd=%s)",
            binary,
            opencode_session_id or "<new>",
            model or "<default>",
            self._cwd or "<inherited>",
        )

        spawn_env = self._build_spawn_env()

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd or None,
                env=spawn_env,
            )
        except FileNotFoundError as exc:
            yield ExecutorError(
                message=f"opencode harness: cannot spawn opencode CLI: {exc}",
                retryable=False,
            )
            return

        assert process.stdout is not None
        assert process.stderr is not None

        stderr_buf = bytearray()
        captured_session_id: str | None = None
        final_text_parts: list[str] = []
        emit_reasoning = self._thinking

        async def _drain_stderr() -> None:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                if len(stderr_buf) < _STDERR_CHUNK_LIMIT:
                    stderr_buf.extend(chunk[: _STDERR_CHUNK_LIMIT - len(stderr_buf)])

        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        process.stdout.readline(), timeout=_TURN_EVENT_WARN_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "opencode harness: no event from CLI for %.0fs; still waiting",
                        _TURN_EVENT_WARN_SECONDS,
                    )
                    continue
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON stdout output (e.g. progress text) —
                    # log at debug and skip; --format json should
                    # only emit JSON but we don't want to crash a
                    # turn if the upstream emits a stray line.
                    logger.debug("opencode harness: non-JSON stdout: %r", line)
                    continue
                if not isinstance(payload, dict):
                    continue
                if captured_session_id is None:
                    sid = payload.get("sessionID")
                    if isinstance(sid, str) and sid:
                        captured_session_id = sid
                for event in _translate_event(payload, emit_reasoning=emit_reasoning):
                    if isinstance(event, TextChunk):
                        final_text_parts.append(event.text)
                    yield event

            return_code = await process.wait()
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            stderr_task.cancel()
            # Best-effort drain of the stderr-capture task on
            # teardown; we don't propagate its outcome since the
            # turn's success/failure is decided by the subprocess
            # exit code.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task

        if (
            omnigent_session_key
            and captured_session_id
            and omnigent_session_key not in self._session_ids
        ):
            self._session_ids[omnigent_session_key] = captured_session_id

        if return_code != 0:
            stderr_text = bytes(stderr_buf).decode("utf-8", errors="replace").strip()
            yield ExecutorError(
                message=(
                    f"opencode CLI exited with code {return_code}"
                    + (f": {stderr_text}" if stderr_text else "")
                ),
                retryable=False,
            )
            return

        yield TurnComplete(response="".join(final_text_parts) or None)

    def supports_streaming(self) -> bool:
        """:returns: ``True`` — text deltas stream as the CLI emits them."""
        return True

    def supports_tool_calling(self) -> bool:
        """:returns: ``True`` — OpenCode invokes its own built-in tools."""
        return True

    def handles_tools_internally(self) -> bool:
        """:returns: ``True`` — OpenCode runs every tool inside its own loop.

        The Session layer must NOT re-execute :class:`ToolCallRequest`
        events from this executor; they describe calls OpenCode
        already made.
        """
        return True

    def max_context_tokens(self) -> int | None:
        """:returns: ``None`` — OpenCode manages context internally."""
        return None

    async def close_session(self, session_key: str) -> None:
        """Drop the cached OpenCode session ID for *session_key*.

        OpenCode persists session state itself; we just forget our
        mapping so a future turn on the same key starts fresh.
        """
        self._session_ids.pop(session_key, None)

    async def interrupt_session(self, session_key: str) -> bool:
        """:returns: ``False`` — per-turn subprocess has no out-of-band cancel.

        The runtime cancels the wrapping HTTP request, which
        cancels the ``run_turn`` async generator and ``finally``
        terminates the subprocess. So interruption *works*, just
        not via a separate ``interrupt_session`` call.
        """
        del session_key
        return False

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """:returns: ``False`` — no live message queue in v0.

        The HTTP/SSE transport (``opencode serve``) supports
        ``prompt_async`` queueing; the per-turn CLI does not. A
        follow-up that switches to a long-lived server can implement
        this.
        """
        del session_key, content
        return False

    async def close(self) -> None:
        """No-op — no executor-wide resources to release."""
        return
