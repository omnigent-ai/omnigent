"""Executor that bridges Omnigent web-chat turns into Claude Code."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.claude_model_vocabulary import claude_model_command_arg, normalized_model_id
from omnigent.claude_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    SWITCH_MODEL_DIALOG_HINT,
    inject_slash_command,
    inject_user_message,
    read_active_session_id,
    read_claude_status_model,
    read_launch_model,
    read_model_env,
)
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
    describe_exception,
)
from omnigent.inner.native_attachments import attachment_reference_line

_logger = logging.getLogger(__name__)


class ClaudeNativeExecutor(Executor):
    """
    Harness-side executor for ``omnigent claude`` web UI turns.

    It does not launch Claude itself. The native wrapper has already
    launched Claude Code in the session terminal with the Omnigent
    bridge MCP server and hooks enabled. Each executor turn only
    injects the latest web UI user message into the same tmux pane
    Claude is attached to (via ``tmux send-keys``). User-visible
    transcript items are mirrored by the always-on transcript
    forwarder, so every rendered chat item has terminal provenance.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        # Serializes every write to the shared tmux pane. ``run_turn``
        # (the initiating message) and ``enqueue_session_message``
        # (mid-turn steering) run as concurrent tasks against this one
        # cached instance (the adapter keeps one executor per
        # conversation), and ``inject_user_message`` is not atomic — it
        # issues several ``tmux send-keys`` calls. Without this lock the
        # two paths interleave their keystrokes and combine messages
        # (e.g. "1" and "2" land as a single "12" prompt). See
        # designs/NATIVE_INJECTION_SERIALIZATION.md. Relies on the
        # adapter caching one executor per conversation; per-turn
        # construction would regress the lock to per-turn scope.
        self._inject_lock = asyncio.Lock()
        # The model the pane is currently on, so a routing turn only types
        # ``/model`` when the model actually changes. Seeded lazily from the
        # spawn ``launch_model`` on the first turn (``None`` = not yet known).
        self._applied_model: str | None = None

    def supports_streaming(self) -> bool:
        """:returns: ``False`` because output is emitted by the transcript forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` because messages can be injected mid-turn."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """
        Inject a live steering message into the Claude terminal.

        :param session_key: Adapter session key. The native bridge is
            per conversation, so this value is not used to route the
            keystrokes (one terminal per conversation).
        :param content: User-supplied content, usually a string.
        :returns: ``True`` when tmux accepted the keystrokes.
        """
        del session_key
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            return False
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        try:
            async with self._inject_lock:
                await asyncio.to_thread(
                    inject_user_message,
                    self._bridge_dir,
                    content=text,
                )
        except RuntimeError:
            return False
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Send the latest user message to Claude's terminal.

        :param messages: Conversation history in executor message
            shape. The latest user message is delivered to Claude;
            prior history already lives in Claude Code's own session.
        :param tools: Tool schemas from Omnigent. Ignored here;
            Claude-native output/tool activity is terminal-originated
            and mirrored from Claude's transcript.
        :param system_prompt: System prompt from the agent spec. The
            native Claude Code terminal controls its own prompt/settings,
            so this is ignored.
        :param config: Per-turn executor config. Only ``config.model``
            is used: when intelligent routing picks a model for this turn,
            it arrives here (adapter maps ``request.model_override`` →
            ``config.model``) and the switch is applied inline, before the
            message — see the ``/model`` handling below.
        :yields: :class:`TurnComplete` after the input was injected,
            or :class:`ExecutorError` on bridge failure.
        """
        del tools, system_prompt
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            yield ExecutorError(
                message=(
                    "Claude native session is no longer active after /clear; "
                    "open the latest conversation and retry."
                )
            )
            return
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="Claude native turn had no user text to send")
            return
        from omnigent.runtime import telemetry

        # Span the tmux send-keys inject — the input half of the decoupled
        # native path. session.id is applied generically by the span processor
        # (the executor adapter binds session_scope for the turn), so there's
        # no per-call-site stamping here.
        #
        # Apply a routed ``/model`` switch and inject the message as ONE
        # step under ``_inject_lock``. Intelligent routing used to switch
        # the model with a separate server-issued ``model_change`` event
        # that raced this inject on the same tmux pane, so the message's
        # keystrokes could land mid-switch and be lost (the "routing drops
        # the first message" bug). Folding the switch into the turn under
        # the same lock removes the second writer entirely: ``/model`` fully
        # commits, then ``inject_user_message`` (which waits for the input
        # box and verifies its submit) delivers the message — in order,
        # once.
        wanted_model = config.model if config is not None else None
        # ``/model`` only accepts this session's aliases / custom slot; a
        # bare catalog id is ignored and the pane keeps its old model.
        wanted_model_arg = self._model_command_arg(wanted_model)
        try:
            with telemetry.span("claude_native.inject"):
                async with self._inject_lock:
                    if wanted_model_arg is not None:
                        # Accepted trade-off: ``/model <id>`` also saves the
                        # pick as the person's global default for new Claude
                        # sessions. Runs to completion before the message
                        # inject below (same lock), so its confirm Enter can't
                        # race the message.
                        await asyncio.to_thread(
                            inject_slash_command,
                            self._bridge_dir,
                            command=f"/model {wanted_model_arg}",
                            auto_confirm=True,
                            confirm_hint=SWITCH_MODEL_DIALOG_HINT,
                        )
                        # Track the routed id, not the alias: the next turn's
                        # comparison is against what routing asked for.
                        self._applied_model = wanted_model
                    await asyncio.to_thread(
                        inject_user_message,
                        self._bridge_dir,
                        content=text,
                    )
        except RuntimeError as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        yield TurnComplete(response=None)

    def _model_command_arg(self, wanted_model: str | None) -> str | None:
        """
        Return the ``/model`` argument for this turn, or ``None`` to skip.

        Two gates: the switch must be needed at all
        (:meth:`_should_switch_model`), and the routed catalog id must
        translate into vocabulary ``/model`` accepts — the session's
        family aliases, or the exact id of its custom picker slot. The
        pinning comes from the terminal's launch env, recorded in the
        bridge config because this process doesn't share that env.

        An untranslatable id fails open: the message still goes in, on
        the current model, with a warning. Typing a value the CLI won't
        take leaves the pane on its old model while reporting success.

        :param wanted_model: The turn's routed model, or ``None``.
        :returns: A ``/model`` argument, or ``None`` when no switch
            should be typed.
        """
        if wanted_model is None:
            _logger.info("claude-native: turn carries no routed model; not typing /model")
            return None
        if not self._should_switch_model(wanted_model):
            _logger.info(
                "claude-native: skipping /model — pane is already on %s",
                wanted_model,
            )
            return None
        env = read_model_env(self._bridge_dir) or None
        wanted_arg = claude_model_command_arg(wanted_model, env)
        if wanted_arg is None:
            _logger.warning(
                "claude-native: skipping /model — routed model %r has no spelling this "
                "session accepts (pins=%s); sending the turn on the current model",
                wanted_model,
                sorted(env or ()),
            )
            return None
        if (
            self._applied_model is not None
            and claude_model_command_arg(self._applied_model, env) == wanted_arg
        ):
            # Resolves to the model the pane is already on, so the switch
            # would be a pointless prompt (and can pop a confirm dialog).
            _logger.info(
                "claude-native: skipping /model — %r resolves to %r, already applied",
                wanted_model,
                wanted_arg,
            )
            return None
        _logger.info(
            "claude-native: typing /model %s for routed model %s",
            wanted_arg,
            wanted_model,
        )
        return wanted_arg

    def _should_switch_model(self, wanted_model: str | None) -> bool:
        """
        Return whether this turn must type ``/model`` before the message.

        Only switches when routing named a model AND it differs from the
        model the pane is already on. The baseline is tracked per turn in
        ``_applied_model``, seeded lazily so turn 1's routed pick is compared
        against what the pane is actually on — not blindly re-issued.

        The LIVE model (the statusLine capture) is the seed, falling back to
        the launch model. ``launch_model`` alone was wrong for a routed first
        message: the turn router blocks the prompt, switches the pane itself
        and then replays the prompt with the same override, so a baseline
        frozen at bridge-prepare time still named the pre-switch model and the
        replay typed a second, redundant ``/model``.

        :param wanted_model: The turn's routed model, or ``None`` when the
            turn carries no override (routing off / already-pinned session).
        :returns: ``True`` when a ``/model`` switch is required.
        """
        if not wanted_model:
            return False
        if self._applied_model is None:
            # Both reads are best-effort (no statusLine capture yet, no ucode
            # profile); an unknown baseline means we switch to be safe — a
            # redundant ``/model`` to the current model is a harmless no-op.
            self._applied_model = read_claude_status_model(self._bridge_dir) or read_launch_model(
                self._bridge_dir
            )
        if self._applied_model is None:
            return True
        # The statusLine reports a display spelling ("Sonnet 5") where routing
        # names a catalog id, so compare normalized.
        return normalized_model_id(wanted_model) != normalized_model_id(self._applied_model)


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If :data:`BRIDGE_DIR_ENV_VAR` is missing.
    """
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for claude-native harness")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    """
    Resolve the Omnigent session id that requested this harness process.

    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None`` when
        the spawn env predates active-session validation.
    """
    raw = os.environ.get(REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _session_is_active(bridge_dir: Path, request_session_id: str | None) -> bool:
    """
    Return whether a request may inject into the shared Claude pane.

    :param bridge_dir: Native bridge directory.
    :param request_session_id: Omnigent session id from
        :data:`REQUEST_SESSION_ID_ENV_VAR`, e.g. ``"conv_abc123"``.
        ``None`` preserves old harness spawns that lack the guard env.
    :returns: ``True`` when injection is allowed.
    """
    if request_session_id is None:
        return True
    active_session_id = read_active_session_id(bridge_dir)
    return active_session_id is None or active_session_id == request_session_id


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """
    Return the latest user text from executor messages.

    Multimodal content blocks (images, files) are materialized to the
    bridge directory and referenced by path in the returned text so
    Claude Code can read them via its Read tool.

    :param messages: Conversation history in executor message shape.
    :param bridge_dir: Bridge directory path for writing attachment
        files, e.g. ``Path("/tmp/omnigent/claude-native/<digest>")``.
    :returns: Concatenated latest user message text, or ``""`` when
        no user text is present.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: EnqueuedContent, bridge_dir: Path) -> str:
    """
    Normalize executor content into plain text.

    Text blocks are extracted directly. Multimodal blocks
    (``input_image``, ``input_file``) that carry resolved base64 data
    URIs are decoded to files in the bridge directory and referenced
    by path so Claude Code can view them with its Read tool.

    :param content: Message content, e.g. a string or a list of
        ``{"type": "input_text", "text": "..."}`` blocks. May also
        contain ``input_image`` blocks with an ``image_url`` data URI
        or ``input_file`` blocks with a ``file_data`` data URI.
    :param bridge_dir: Bridge directory path for writing attachment
        files, e.g. ``Path("/tmp/omnigent/claude-native/<digest>")``.
    :returns: Plain text content with file-path references prepended
        for any materialized attachments.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        attachment_lines: list[str] = []
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "input_text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type in ("input_image", "input_file"):
                attachment_lines.append(attachment_reference_line(block, bridge_dir))
        parts = attachment_lines + text_parts
        return "\n\n".join(parts)
    return ""
