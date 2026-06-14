"""CursorExecutor: run agents through a persistent ``cursor-agent acp`` session.

Drives Cursor's ``cursor-agent`` CLI over the Agent Client Protocol (ACP,
``cursor-agent acp``). Per Omnigent conversation it holds one ACP session
(``session/new``) that stays open across turns; each ``run_turn`` sends one
``session/prompt`` and translates the streamed ``session/update`` notifications
into ExecutorEvents (assistant text → :class:`TextChunk`, agent thoughts →
:class:`ReasoningChunk`, tool calls → :class:`ToolCallRequest` /
:class:`ToolCallComplete`), completing on the prompt response's ``stopReason``.
The ACP transport lives in :class:`omnigent.inner.cursor_acp.AcpClient`.

cursor-agent talks only to Cursor's own backend (``CURSOR_API_KEY`` /
``cursor-agent login``); there is no Databricks gateway, so a ``databricks-*``
model id is dropped in favor of cursor's default. The system prompt is prepended
to the first turn (ACP has no system-prompt field). The agent uses its own
native tools; bridging Omnigent spec-declared tools would require an http/sse
MCP server via ``session/new`` ``mcpServers``, and ACP reports no token usage.

Requirements:
    The ``cursor-agent`` CLI must be installed and on PATH (or
    ``HARNESS_CURSOR_PATH`` set).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from .cursor_acp import AcpClient, AcpError
from .datamodel import OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
)

logger = logging.getLogger(__name__)

# One ACP ``session/update`` payload (the inner ``params.update`` object).
# Opaque JSON owned by the ACP spec / cursor-agent.
AcpUpdate: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Prefix-matched env var names allowed into the cursor-agent subprocess. Only
# known-safe categories pass: Cursor's own config knobs, proxy/TLS settings,
# Node.js runtime knobs (cursor-agent bundles a Node runtime), and locale.
# Credential families (``DATABRICKS_*``, ``AWS_*``, provider API keys, ...)
# deliberately do NOT match — mirrors :data:`pi_executor._PI_ENV_ALLOW_PREFIXES`.
_CURSOR_ENV_ALLOW_PREFIXES: tuple[str, ...] = (
    "CURSOR_",
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

# Exact-matched env var names allowed into the cursor-agent subprocess: the
# minimal set a POSIX CLI reasonably expects (HOME carries ~/.cursor login).
_CURSOR_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
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


def _find_cursor() -> str | None:
    """Find the ``cursor-agent`` CLI on PATH."""
    return shutil.which("cursor-agent")


def _sandbox_mode(os_env: OSEnvSpec | None) -> str:
    """Map ``os_env.sandbox`` to cursor-agent's ``--sandbox`` mode.

    Mirrors codex's ``_sandbox_mode``: a restrictive sandbox enables cursor's
    own sandbox; ``"none"`` / unset (the headless default) disables it for full
    access. Enforcement is cursor-agent's, not Omnigent's bwrap.
    """
    sandbox = os_env.sandbox if os_env is not None else None
    if sandbox is None or sandbox.type == "none":
        return "disabled"
    return "enabled"


def _clean_cursor_env(extra_allowed: Sequence[str] | None = None) -> dict[str, str]:
    """Build a filtered copy of ``os.environ`` for the cursor-agent subprocess.

    Deny-by-default allowlist mirroring :func:`pi_executor._clean_pi_env`: only
    the known-safe prefixes/exact names pass, so host secrets (cloud tokens,
    unrelated API keys) never reach the cursor-agent process. ``CURSOR_API_KEY``
    is allowed via the ``CURSOR_`` prefix; the executor also injects it
    explicitly from config when provided.

    :param extra_allowed: Extra exact names to pass through, e.g. a spec's
        ``os_env.sandbox.env_passthrough`` entries. ``None`` means no extras.
    :returns: Filtered environment dict.
    """
    allow_exact = set(_CURSOR_ENV_ALLOW_EXACT)
    if extra_allowed is not None:
        allow_exact.update(extra_allowed)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allow_exact or key.startswith(_CURSOR_ENV_ALLOW_PREFIXES)
    }


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _extract_text(msg: Message) -> str:
    """Extract plain text content from a message dict."""
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content)


def _latest_user_text(messages: list[Message]) -> str:
    """Return the text of the latest user message (multimodal parts joined)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _extract_text(msg)
    return ""


def _build_cursor_prompt(
    messages: list[Message],
    *,
    is_first_turn: bool,
    system_prompt: str,
) -> str:
    """Build the prompt text for a ``session/prompt``.

    cursor-agent's ACP session has no system-prompt field, so on the first turn
    the Omnigent system prompt is prepended. When the first turn also carries
    prior history (e.g. a sub-agent with ``pass_history=True``), the
    conversation is serialized so cursor has context. On subsequent turns the
    ACP session already holds the history, so only the latest user message is
    sent.

    :param messages: Omnigent conversation history for the turn.
    :param is_first_turn: ``True`` when this is the first turn against a fresh
        cursor session (system prompt + any prior history must be included).
    :param system_prompt: The Omnigent system prompt. Prepended only on the
        first turn; pass ``""`` to skip.
    :returns: The prompt string (empty string when there is nothing to send).
    """
    user_messages = [m for m in messages if m.get("role") == "user"]
    if is_first_turn and len(messages) > 1 and len(user_messages) > 1:
        lines = ["Conversation so far:"]
        for msg in messages:
            role = str(msg.get("role") or "user").replace("_", " ")
            lines.append(f"{role}: {_extract_text(msg)}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        body = "\n".join(lines)
    else:
        body = _latest_user_text(messages)

    if is_first_turn and system_prompt:
        return f"{system_prompt}\n\n{body}" if body else system_prompt
    return body


# ---------------------------------------------------------------------------
# session/update → ExecutorEvent
# ---------------------------------------------------------------------------


def _update_to_event(update: AcpUpdate) -> ExecutorEvent | None:
    """Map one ACP ``session/update`` to an ExecutorEvent, or ``None`` to skip.

    :param update: The ``params.update`` object from a ``session/update``
        notification (carries a ``sessionUpdate`` discriminator).
    :returns: The mapped event, or ``None`` for updates with nothing to surface
        (mode changes, command lists, plans, echoed user input).
    """
    kind = update.get("sessionUpdate")
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None

    if kind == "agent_message_chunk":
        return TextChunk(text=text) if isinstance(text, str) and text else None
    if kind == "agent_thought_chunk":
        if isinstance(text, str) and text:
            return ReasoningChunk(delta=text, event_type="reasoning_text")
        return None
    if kind == "tool_call":
        raw_input = update.get("rawInput")
        return ToolCallRequest(
            name=str(update.get("title") or update.get("kind") or "tool"),
            args=raw_input if isinstance(raw_input, dict) else {},
            metadata={"call_id": update.get("toolCallId")},
        )
    if kind == "tool_call_update" and update.get("status") == "completed":
        result = update.get("content")
        classification = classify_tool_result(result)
        return ToolCallComplete(
            name=str(update.get("title") or "tool"),
            status=classification.status,
            result=result,
            error=classification.error or None,
            metadata={"call_id": update.get("toolCallId")},
        )
    return None


# ---------------------------------------------------------------------------
# CursorExecutor
# ---------------------------------------------------------------------------


@dataclass
class _CursorSessionState:
    """Per-Omnigent-conversation ACP session state."""

    client: AcpClient | None = None
    session_id: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    has_sent_prompt: bool = False


class CursorExecutor(Executor):
    """Execute agent turns via a persistent ``cursor-agent acp`` session."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        cursor_path: str | None = None,
        api_key: str | None = None,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """Create a CursorExecutor.

        :param cwd: Working directory the ACP session operates in. ``None``
            falls back to ``os_env.cwd`` then the process cwd.
        :param os_env: Optional OS environment / sandbox spec (its ``cwd`` is
            used when *cwd* is unset).
        :param model: Cursor model id (e.g. ``"gpt-5"``); a ``databricks-*`` id
            is dropped in favor of cursor's default. ``None`` uses the default.
        :param cursor_path: Absolute path to ``cursor-agent``. ``None`` searches PATH.
        :param api_key: Cursor API key injected as ``CURSOR_API_KEY``. ``None``
            relies on an inherited key or a prior ``cursor-agent login``.
        :param bundle_dir: Reserved for future skill wiring; unused in v1.
        :param agent_name: Reserved for future use.
        :param skills_filter: Accepted for parity; cursor has no skill mechanism here.
        :raises ImportError: When ``cursor-agent`` is not found.
        """
        resolved = cursor_path or _find_cursor()
        if not resolved:
            raise ImportError(
                "CursorExecutor requires the 'cursor-agent' CLI on PATH. "
                "Install it with: curl https://cursor.com/install -fsS | bash"
            )
        self._cursor_path = resolved
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        self._model_override = model
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        passthrough = (
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        self._env: dict[str, str] = _clean_cursor_env(passthrough)
        if api_key:
            self._env["CURSOR_API_KEY"] = api_key
        self._session_states: dict[str, _CursorSessionState] = {}

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        # Unlike claude-sdk / codex / pi, ACP exposes no confirmed mid-turn
        # steer, so a message can't be injected into a running turn.
        return False

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            last = messages[-1]
            if last.get("session_id"):
                return str(last["session_id"])
            meta = last.get("metadata", {})
            if isinstance(meta, dict) and meta.get("session_id"):
                return str(meta["session_id"])
        return "__default__"

    def _resolve_model(self, config: ExecutorConfig | None) -> str | None:
        """Resolve the cursor model; drop non-cursor ``databricks-*`` ids.

        cursor-agent accepts only Cursor model ids (``auto``, ``gpt-5``,
        ``claude-4.5-sonnet``, ...) and rejects gateway ids, so a
        ``databricks-*`` model (from a spec authored for another harness) falls
        back to cursor's default. Returns ``None`` to use cursor's default.
        """
        cfg = config or ExecutorConfig()
        model = cfg.model or self._model_override
        if model and model.startswith(("databricks-", "databricks/")):
            logger.debug("CursorExecutor: %r is not a cursor model; using cursor's default", model)
            return None
        return model

    async def _close_state(self, state: _CursorSessionState) -> None:
        if state.client is not None:
            await state.client.close()
            state.client = None

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None:
            await self._close_state(state)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None:
            return False
        if state.client is not None and state.session_id is not None:
            with contextlib.suppress(Exception):
                await state.client.cancel(state.session_id)
        # Always drop the session so the next turn starts fresh — same rationale
        # as the pi executor (a resumed turn would bypass the runner's interrupt
        # marker).
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface as False
            logger.debug("CursorExecutor: close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        for key in list(self._session_states.keys()):
            await self.close_session(key)

    async def _ensure_session(self, state: _CursorSessionState, model: str | None) -> None:
        """Spawn the ACP server and open a session if one isn't live."""
        if state.client is not None and state.client.running:
            return
        cwd = self._cwd or os.getcwd()
        client = AcpClient(
            self._cursor_path,
            env=self._env,
            cwd=cwd,
            extra_args=["--sandbox", _sandbox_mode(self._os_env_spec)],
        )
        await client.start()
        state.session_id = await client.new_session(cwd=cwd, model=model, mcp_servers=[])
        state.client = client

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 — cursor uses its own native tools in v1
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        session_key = self._session_key(messages)
        model = self._resolve_model(config)
        state = self._session_states.setdefault(session_key, _CursorSessionState())

        # The system prompt is baked into the first turn and the model is fixed
        # at session creation, so a change to either means a fresh ACP session.
        if state.client is not None and (
            state.system_prompt != system_prompt or state.model != model
        ):
            await self._close_state(state)
            state = _CursorSessionState()
            self._session_states[session_key] = state
        is_first_turn = not state.has_sent_prompt
        state.system_prompt = system_prompt
        state.model = model

        try:
            await self._ensure_session(state, model)
        except (AcpError, OSError, ImportError) as exc:
            await self.close_session(session_key)
            yield ExecutorError(message=f"Failed to start cursor-agent acp: {exc}")
            return

        prompt = _build_cursor_prompt(
            messages, is_first_turn=is_first_turn, system_prompt=system_prompt
        )
        if not prompt:
            yield TurnComplete(response=None)
            return

        assert state.client is not None and state.session_id is not None
        state.has_sent_prompt = True
        response_text = ""
        try:
            async for kind, payload in state.client.prompt_stream(
                state.session_id, [{"type": "text", "text": prompt}]
            ):
                if kind == "update":
                    event = _update_to_event(payload)
                    if event is not None:
                        if isinstance(event, TextChunk):
                            response_text += event.text
                        yield event
                elif kind == "error":
                    yield ExecutorError(
                        message=f"cursor-agent acp error: {payload}", retryable=True
                    )
                    return
                else:  # "result"
                    # stopReason in payload; ACP reports no token usage, so the
                    # turn is left unpriced (usage=None).
                    yield TurnComplete(response=response_text or None, usage=None)
                    return
        except AcpError as exc:
            # The ACP server died mid-turn — drop the session so the next turn
            # respawns it, and surface a retryable error.
            stderr = state.client.stderr_tail() if state.client is not None else ""
            await self.close_session(session_key)
            suffix = f" Stderr: {stderr}" if stderr else ""
            yield ExecutorError(
                message=f"cursor-agent acp turn failed: {exc}{suffix}", retryable=True
            )
