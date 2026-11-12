"""Executor that bridges Omnigent web turns into a native Cline hub session.

The runner-side launcher owns the ``cline hub`` connection and its
forwarder; this executor reads the endpoint coordinates from bridge state
and drives ``session.send_input`` for each web-UI turn — the same
injection/completion split as grok-native: output is mirrored by the
forwarder, so ``run_turn`` only admits the prompt and yields
``TurnComplete``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.harnesses.cline_native.bridge import (
    ClineBridgeState,
    bridge_dir_from_env,
    read_bridge_state,
)
from omnigent.harnesses.cline_native.client import ClineHubError
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

CLINE_NATIVE_HARNESS_ID = "cline-native"


class ClineNativeExecutor(Executor):
    """Harness-side executor for ``omnigent cline`` web-UI turns.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`CLINE_NATIVE_BRIDGE_DIR_ENV_VAR` from the spawn env.
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
        """Admit the latest web-UI user message as a ``session.send_input``."""
        del tools, system_prompt, config
        text = _latest_user_text(messages)
        if not text:
            yield ExecutorError(message="cline native turn had no user text to send")
            return
        # The runner's hub lifecycle publishes bridge state after session.create
        # returns — a fast-posted first turn can arrive before it lands, so
        # wait briefly rather than erroring the turn.
        state = await _wait_bridge_state(self._bridge_dir)
        if state is None or not state.session_id:
            yield ExecutorError(
                message="cline native session not ready (no bridge state/session id)"
            )
            return
        try:
            await _prompt_session(state, text, self._bridge_dir.name)
        except (ClineHubError, OSError, RuntimeError) as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        yield TurnComplete(response=None)

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Decline mid-turn steering until the cline steer path is wired."""
        del session_key, content
        return False

    async def stop(self) -> None:
        """Interrupt the active turn via ``run.abort``."""
        state = read_bridge_state(self._bridge_dir)
        if state is None or not state.session_id:
            return
        with contextlib.suppress(ClineHubError, OSError, RuntimeError):
            await _abort_session(state)


async def _prompt_session(state: ClineBridgeState, text: str, omnigent_session_id: str) -> None:
    """Connect to the hub and drive one prompt turn.

    Subscribes to the session's event stream on the prompting connection so
    ``approval.requested`` events route through Omnigent's
    ``native-permission-request`` hook — the ASK leg of the policy bridge —
    and answers them with ``approval.respond``.
    """
    from omnigent.harnesses.cline_native.client import ClineHubClient

    client = await ClineHubClient.connect(state.ws_url, state.auth_token)
    client.set_event_handler(_approval_event_handler(client, state, omnigent_session_id))
    await client.subscribe(state.session_id)
    try:
        await client.send_input(state.session_id, text, timeout_s=600.0)
    finally:
        await client.close()


def _approval_event_handler(client: Any, state: ClineBridgeState, omnigent_session_id: str):
    """Route ``approval.requested`` events into the Omnigent elicitation hook."""
    import asyncio

    def _on_event(envelope: dict[str, Any]) -> None:
        if envelope.get("event") != "approval.requested":
            return
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return
        asyncio.get_event_loop().create_task(
            _answer_approval(client, state, omnigent_session_id, payload)
        )

    return _on_event


async def _answer_approval(
    client: Any,
    state: ClineBridgeState,
    omnigent_session_id: str,
    payload: dict[str, Any],
) -> None:
    """Resolve one ``approval.requested`` via the elicitation hook, then respond."""
    approval_id = str(payload.get("approvalId") or "")
    if not approval_id:
        return
    approved = await _request_permission(
        state,
        omnigent_session_id,
        tool_name=str(payload.get("toolName") or "a tool call"),
        input_json=str(payload.get("inputJson") or ""),
    )
    with contextlib.suppress(ClineHubError, OSError, RuntimeError):
        await client.respond_approval(approval_id, approved)


async def _request_permission(
    state: ClineBridgeState,
    omnigent_session_id: str,
    *,
    tool_name: str,
    input_json: str,
) -> bool:
    """POST to ``native-permission-request``; ``True`` only on accept."""
    import secrets

    import httpx

    if not state.server_url:
        return False
    preview = input_json[:400] if input_json else tool_name
    payload = {
        "elicitation_id": f"cline-{secrets.token_hex(8)}",
        "agent": "Cline",
        "policy_name": "cline_native_permission",
        "operation_type": "tool",
        "message": f"Cline wants approval for {tool_name}",
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
        return False
    return action == "accept"


async def _wait_bridge_state(bridge_dir: Path, timeout_s: float = 60.0) -> ClineBridgeState | None:
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


async def _abort_session(state: ClineBridgeState) -> None:
    """Open a hub connection and send ``run.abort`` for the active turn."""
    from omnigent.harnesses.cline_native.client import ClineHubClient

    client = await ClineHubClient.connect(state.ws_url, state.auth_token)
    try:
        await client.abort(state.session_id)
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


__all__ = ["CLINE_NATIVE_HARNESS_ID", "ClineNativeExecutor"]
