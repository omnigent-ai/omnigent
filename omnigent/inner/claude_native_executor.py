"""Executor that bridges Omnigent web-chat turns into Claude Code."""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from omnigent.claude_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    ClaudePromptReadyTimeout,
    inject_user_message,
    is_claude_prompt_ready_timeout,
    read_active_session_id,
    read_permission_hook_config,
)
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)
from omnigent.inner.native_attachments import materialize_attachment
from omnigent.native_policy_hook import (
    _is_login_redirect_or_unauthorized,
    policy_hook_reauth,
)

_logger = logging.getLogger(__name__)
_CLAUDE_TERMINAL_NAME = "claude"
_CLAUDE_TERMINAL_SESSION_KEY = "main"
_CLAUDE_TERMINAL_RECOVERY_TIMEOUT_S = 30.0
_CLAUDE_BOOT_RECOVERY_ERROR_CODE = "timeout"


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

    def supports_streaming(self) -> bool:
        """:returns: ``False`` because output is emitted by the transcript forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` because messages can be injected mid-turn."""
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
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
        :param config: Per-turn executor config. Unused by this
            terminal-backed executor.
        :yields: :class:`TurnComplete` after the input was injected,
            or :class:`ExecutorError` on bridge failure.
        """
        del tools, system_prompt, config
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
        try:
            with telemetry.span("claude_native.inject"):
                async with self._inject_lock:
                    await _inject_user_message_with_boot_recovery(
                        self._bridge_dir,
                        content=text,
                        request_session_id=self._request_session_id,
                    )
        except RuntimeError as exc:
            yield ExecutorError(
                message=str(exc),
                retryable=is_claude_prompt_ready_timeout(exc),
                code=(
                    _CLAUDE_BOOT_RECOVERY_ERROR_CODE
                    if is_claude_prompt_ready_timeout(exc)
                    else None
                ),
            )
            return
        yield TurnComplete(response=None)


async def _inject_user_message_with_boot_recovery(
    bridge_dir: Path,
    *,
    content: str,
    request_session_id: str | None,
) -> None:
    """
    Inject a message, recovering once from Claude Code boot timeouts.

    A readiness timeout means tmux exists but Claude Code's input
    prompt never mounted. The pane is not reusable, so close it through
    the resource API, ask the runner to cold-resume the native terminal,
    then deliver the same pending message once more. No other injection
    failure is retried.

    :param bridge_dir: Native bridge directory.
    :param content: User text to deliver to Claude Code.
    :param request_session_id: Session id from harness spawn env.
    :returns: None on successful injection.
    :raises RuntimeError: Original non-readiness failures, or a
        readiness timeout after the single recovery attempt.
    """
    try:
        await asyncio.to_thread(inject_user_message, bridge_dir, content=content)
        return
    except RuntimeError as exc:
        if not is_claude_prompt_ready_timeout(exc):
            raise
        first_error = str(exc)

    session_id = _recovery_session_id(bridge_dir, request_session_id)
    _logger.warning(
        "Claude Code prompt readiness timed out for session %s; closing terminal "
        "and relaunching once before retrying injection.",
        session_id,
    )
    await _close_claude_terminal_for_recovery(bridge_dir, session_id)
    try:
        await _ensure_claude_terminal_for_recovery(bridge_dir, session_id)
    except ClaudeNativeBootRecoveryError:
        await _best_effort_close_claude_terminal_for_recovery(bridge_dir, session_id)
        raise

    try:
        await asyncio.to_thread(inject_user_message, bridge_dir, content=content)
        return
    except httpx.HTTPError as exc:
        await _best_effort_close_claude_terminal_for_recovery(bridge_dir, session_id)
        raise ClaudeNativeBootRecoveryError(
            "Claude native boot recovery relaunched the terminal, but retry injection "
            f"failed with a transient HTTP error. The retry pane was closed; retry this "
            f"message to cold-resume the session. First startup error: {first_error} "
            f"Retry injection error: {exc}"
        ) from exc
    except RuntimeError as exc:
        # Any retry-injection failure (readiness timeout, tmux target
        # missing, submit verification) leaves the freshly-ensured pane
        # behind; close it so a later native ensure cannot reuse it.
        await _best_effort_close_claude_terminal_for_recovery(bridge_dir, session_id)
        if not is_claude_prompt_ready_timeout(exc):
            raise
        raise ClaudeNativeBootRecoveryError(
            "Claude Code terminal did not become ready after one automatic "
            "relaunch. The crashed pane was closed; retry this message to "
            f"cold-resume the session. First startup error: {first_error} "
            f"Retry startup error: {exc}"
        ) from exc


class ClaudeNativeBootRecoveryError(ClaudePromptReadyTimeout):
    """Claude prompt readiness failed again after the single recovery retry."""


async def _best_effort_close_claude_terminal_for_recovery(
    bridge_dir: Path,
    session_id: str,
) -> None:
    """
    Close a possibly-created retry pane without masking the real failure.

    :param bridge_dir: Native bridge directory.
    :param session_id: Omnigent session id.
    :returns: None.
    """
    try:
        await _close_claude_terminal_for_recovery(bridge_dir, session_id)
    except ClaudeNativeBootRecoveryError:
        _logger.warning(
            "Claude native boot recovery could not confirm cleanup for session %s",
            session_id,
            exc_info=True,
        )


def _recovery_session_id(bridge_dir: Path, request_session_id: str | None) -> str:
    """
    Resolve the Omnigent session id used for terminal recovery.

    :param bridge_dir: Native bridge directory.
    :param request_session_id: Harness spawn session id, when present.
    :returns: Active Omnigent session id.
    :raises RuntimeError: If no session id can be resolved.
    """
    if request_session_id:
        return request_session_id
    active_session_id = read_active_session_id(bridge_dir)
    if active_session_id:
        return active_session_id
    raise ClaudeNativeBootRecoveryError(
        "Claude native boot recovery cannot resolve the active session id."
    )


def _recovery_server_config(bridge_dir: Path) -> tuple[str, dict[str, str]]:
    """
    Read server URL and headers from the bridge permission-hook config.

    :param bridge_dir: Native bridge directory.
    :returns: ``(base_url, headers)`` for Omnigent server requests.
    :raises RuntimeError: If the bridge lacks server routing metadata.
    """
    config = read_permission_hook_config(bridge_dir)
    raw_base_url = config.get("ap_server_url")
    if not isinstance(raw_base_url, str) or not raw_base_url.strip():
        raise ClaudeNativeBootRecoveryError(
            "Claude native boot recovery cannot relaunch the terminal because "
            "the bridge is missing Omnigent server routing metadata."
        )
    raw_headers = config.get("ap_auth_headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    # Replays the runner-minted owner bearer plus routing headers; the
    # server terminal routes enforce the normal session LEVEL_EDIT ACL.
    return raw_base_url.rstrip("/"), headers


def _recovery_http_client(base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
    """
    Build the HTTP client used for terminal recovery calls.

    :param base_url: Omnigent server base URL.
    :param headers: Auth/routing headers read from the bridge config.
    :returns: Configured async HTTP client.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=_CLAUDE_TERMINAL_RECOVERY_TIMEOUT_S,
    )


async def _recovery_request(
    bridge_dir: Path,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """
    Send a recovery request, re-minting the bearer once on an auth lapse.

    The bridge's ``ap_auth_headers`` are snapshotted at launch and expire
    with the ~1h Databricks OAuth lifetime, so a long-idle session's
    close/ensure calls can arrive with a dead bearer. On a 401 or an
    OAuth-login redirect the token is re-minted through the same factory
    the permission hook uses (``policy_hook_reauth``) and the request is
    retried once, keeping routing headers so the retry still lands.

    :param bridge_dir: Native bridge directory.
    :param method: HTTP method, e.g. ``"DELETE"`` or ``"POST"``.
    :param path: Server request path.
    :param json_body: Optional JSON payload for the request.
    :returns: The server response, after at most one re-auth retry.
    :raises httpx.HTTPError: If the request transport fails.
    """
    base_url, headers = _recovery_server_config(bridge_dir)
    reauth = policy_hook_reauth(base_url, headers)
    reauthed = False
    while True:
        async with _recovery_http_client(base_url, headers) as client:
            response = await client.request(method, path, json=json_body)
        if not reauthed and _is_login_redirect_or_unauthorized(response):
            refreshed = await asyncio.to_thread(reauth)
            if refreshed:
                headers = refreshed
                reauthed = True
                continue
        return response


async def _close_claude_terminal_for_recovery(bridge_dir: Path, session_id: str) -> None:
    """
    Close the claude/main terminal through the existing resource API.

    :param bridge_dir: Native bridge directory.
    :param session_id: Omnigent session id.
    :returns: None.
    :raises RuntimeError: If the server rejects the close.
    """
    terminal_id = terminal_resource_id(_CLAUDE_TERMINAL_NAME, _CLAUDE_TERMINAL_SESSION_KEY)
    quoted_session_id = urllib.parse.quote(session_id, safe="")
    quoted_terminal_id = urllib.parse.quote(terminal_id, safe="")
    try:
        response = await _recovery_request(
            bridge_dir,
            "DELETE",
            f"/v1/sessions/{quoted_session_id}/resources/terminals/{quoted_terminal_id}",
        )
    except httpx.HTTPError as exc:
        raise ClaudeNativeBootRecoveryError(
            "Claude native boot recovery could not close the crashed terminal "
            f"because the Omnigent server request failed: {exc}"
        ) from exc
    if response.status_code in (200, 404):
        return
    raise ClaudeNativeBootRecoveryError(
        "Claude native boot recovery failed to close the crashed terminal "
        f"(HTTP {response.status_code})."
    )


async def _ensure_claude_terminal_for_recovery(bridge_dir: Path, session_id: str) -> None:
    """
    Relaunch claude/main through the existing native ensure API.

    :param bridge_dir: Native bridge directory.
    :param session_id: Omnigent session id.
    :returns: None when the runner accepted the relaunch.
    :raises RuntimeError: If the server rejects the relaunch.
    """
    quoted_session_id = urllib.parse.quote(session_id, safe="")
    try:
        response = await _recovery_request(
            bridge_dir,
            "POST",
            f"/v1/sessions/{quoted_session_id}/resources/terminals",
            json_body={
                "terminal": _CLAUDE_TERMINAL_NAME,
                "session_key": _CLAUDE_TERMINAL_SESSION_KEY,
                "ensure_native_terminal": True,
            },
        )
    except httpx.HTTPError as exc:
        raise ClaudeNativeBootRecoveryError(
            "Claude native boot recovery could not relaunch the terminal "
            f"because the Omnigent server request failed: {exc}"
        ) from exc
    if response.status_code < 400:
        return
    raise ClaudeNativeBootRecoveryError(
        "Claude native boot recovery failed to relaunch the terminal "
        f"(HTTP {response.status_code})."
    )


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


def _content_to_text(content: Any, bridge_dir: Path) -> str:
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
                path = materialize_attachment(block, bridge_dir)
                if path is not None:
                    # Marker format is load-bearing: the transcript mirrors
                    # this text back as the durable user message, and title
                    # seeding strips lines matching _ATTACHMENT_MARKER_RE in
                    # omnigent/entities/conversation.py. Keep in sync.
                    attachment_lines.append(f"[Attached: {path}]")
        parts = attachment_lines + text_parts
        return "\n\n".join(parts)
    return ""
