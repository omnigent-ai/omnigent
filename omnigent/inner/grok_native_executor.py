"""Executor that bridges Omnigent web turns into a native Grok session.

The runner-side launcher owns the ``grok agent serve`` WebSocket server and
its forwarder; this executor reads the endpoint coordinates from bridge
state and drives ``session/prompt`` for each web-UI turn — the same
injection/completion split as goose-native: output is mirrored by the
forwarder, so ``run_turn`` only admits the prompt and yields
``TurnComplete``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.harnesses.grok_native.bridge import (
    GrokBridgeState,
    bridge_dir_from_env,
    read_bridge_state,
)
from omnigent.harnesses.grok_native.client import GrokAcpError
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

GROK_NATIVE_HARNESS_ID = "grok-native"


class GrokNativeExecutor(Executor):
    """Harness-side executor for ``omnigent grok`` web-UI turns.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`GROK_NATIVE_BRIDGE_DIR_ENV_VAR` from the spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or bridge_dir_from_env()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is mirrored by the runner-side forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``False`` — mid-turn steering is not wired yet."""
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Admit the latest web-UI user message as a ``session/prompt``."""
        del tools, system_prompt, config
        text = _latest_user_text(messages)
        if not text:
            yield ExecutorError(message="grok native turn had no user text to send")
            return
        # The runner's serve lifecycle publishes bridge state after the pane
        # binds and session/new returns — a fast-posted first turn can arrive
        # before it lands, so wait briefly rather than erroring the turn.
        state = await _wait_bridge_state(self._bridge_dir)
        if state is None or not state.session_id:
            yield ExecutorError(
                message="grok native session not ready (no bridge state/session id)"
            )
            return
        try:
            await _prompt_session(state, text, self._bridge_dir.name)
        except (GrokAcpError, OSError, RuntimeError) as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        yield TurnComplete(response=None)

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Decline mid-turn steering until the grok steer path is wired."""
        del session_key, content
        return False

    async def stop(self) -> None:
        """Interrupt the active turn via ``session/cancel``."""
        state = read_bridge_state(self._bridge_dir)
        if state is None or not state.session_id:
            return
        with contextlib.suppress(GrokAcpError, OSError, RuntimeError):
            await _cancel_session(state)


async def _prompt_session(state: GrokBridgeState, text: str, omnigent_session_id: str) -> None:
    """Open a WS to the serve endpoint and drive one prompt turn.

    Installs a ``session/request_permission`` handler that surfaces the
    approval through Omnigent's ``native-permission-request`` hook — the
    ASK leg of the policy bridge needs this, since the plugin's command
    hook routes elicitation to the same endpoint.
    """
    from omnigent.harnesses.grok_native.client import GrokAcpClient

    client = await GrokAcpClient.connect(state.ws_url)
    client.set_permission_handler(
        lambda params: _answer_permission_request(state, omnigent_session_id, params)
    )
    try:
        await client.session_prompt(session_id=state.session_id, text=text)
    finally:
        await client.close()


async def _answer_permission_request(
    state: GrokBridgeState, omnigent_session_id: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Resolve a ``session/request_permission`` via the Omnigent elicitation hook."""
    import secrets

    import httpx

    if not state.server_url:
        return None
    tool_call = params.get("toolCall") or {}
    preview = str(tool_call.get("title") or tool_call.get("kind") or "a tool call")
    options = params.get("options") or []
    allow_id, reject_id = None, None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("optionId") or "").lower()
        kind = str(opt.get("kind") or "").lower()
        if allow_id is None and ("allow" in oid or "allow" in kind):
            allow_id = opt.get("optionId")
        if reject_id is None and ("reject" in oid or "reject" in kind):
            reject_id = opt.get("optionId")
    payload = {
        "elicitation_id": f"grok-{secrets.token_hex(8)}",
        "agent": "Grok Build",
        "policy_name": "grok_native_permission",
        "operation_type": "tool",
        "message": f"Grok wants approval for {preview}",
        "content_preview": preview,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as http:
            resp = await http.post(
                f"{state.server_url.rstrip('/')}/v1/sessions/{omnigent_session_id}"
                "/hooks/native-permission-request",
                headers=dict(state.headers),
                json=payload,
            )
        action = resp.json().get("action") if resp.status_code < 400 and resp.content else None
    except httpx.HTTPError:
        return None
    if action == "accept":
        option_id = allow_id or (options[0].get("optionId") if options else None)
        if option_id:
            return {"outcome": {"selected": {"optionId": option_id}}}
        return None
    if action == "decline":
        if reject_id:
            return {"outcome": {"selected": {"optionId": reject_id}}}
        return None
    return None


async def _wait_bridge_state(bridge_dir: Path, timeout_s: float = 60.0) -> GrokBridgeState | None:
    """Poll for published bridge state up to *timeout_s*."""
    import asyncio
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        state = read_bridge_state(bridge_dir)
        if state is not None and state.ws_url:
            return state
        if time.monotonic() >= deadline:
            return state
        await asyncio.sleep(0.5)


async def _cancel_session(state: GrokBridgeState) -> None:
    """Open a WS and send ``session/cancel`` for the active turn."""
    from omnigent.harnesses.grok_native.client import GrokAcpClient

    client = await GrokAcpClient.connect(state.ws_url)
    try:
        await client.session_cancel(session_id=state.session_id)
    finally:
        await client.close()


def _latest_user_text(messages: list[Message]) -> str:
    """Return the latest user message's text content."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n\n".join(
                str(b.get("text"))
                for b in content
                if isinstance(b, dict)
                and b.get("type") in ("input_text", "text")
                and b.get("text")
            )
    return ""


__all__ = ["GROK_NATIVE_HARNESS_ID", "GrokNativeExecutor"]
