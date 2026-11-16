"""Executor that bridges Omnigent web turns into a native Cline hub session.

The runner-side launcher owns the ``cline hub`` connection and its
forwarder; this executor reads the endpoint coordinates from bridge state
and drives ``session.send_input`` for each web-UI turn — the same
injection/completion split as grok-native: output is mirrored by the
forwarder, so ``run_turn`` only admits the prompt and yields
``TurnComplete``.

Hub restarts rotate the auth token and mark live sessions ``aborted``, so
every hub interaction here refreshes coordinates once via
``cline hub start`` before giving up, and reports the session's recorded
status/provider in error text so a dead agent is diagnosable from logs.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.harnesses.cline_native.bridge import (
    ClineBridgeState,
    bridge_dir_from_env,
    read_bridge_state,
    write_bridge_state,
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

logger = logging.getLogger(__name__)

CLINE_NATIVE_HARNESS_ID = "cline-native"

# Statuses the hub leaves a session in when it can no longer accept input —
# a hub restart marks live sessions ``aborted`` and only a fresh
# ``session.create`` (runner-side) revives the conversation.
_DEAD_SESSION_STATUSES = {"aborted", "terminated", "deleted", "closed"}

_RUN_TERMINAL_EVENTS = {"run.completed", "run.failed", "run.aborted", "run.interrupted"}

# How long to wait for an in-flight run to settle after reconnecting mid-turn.
_RUN_SETTLE_TIMEOUT_S = 3600.0


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
            await _prompt_session(
                state,
                text,
                self._bridge_dir,
                omnigent_session_id=self._bridge_dir.name,
            )
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
            try:
                await _abort_session(state)
            except (ClineHubError, OSError, RuntimeError):
                # Stale token after a hub restart — refresh once, retry once.
                state = await _refresh_state_if_needed(state, self._bridge_dir)
                await _abort_session(state)


def _describe_session_record(record: dict[str, Any]) -> str:
    """One-line diagnostic summary of a hub session record."""
    metadata = record.get("metadata") or {}
    participants = record.get("participants") or []
    return (
        f"status={record.get('status')!r} "
        f"provider={metadata.get('provider')!r} model={metadata.get('model')!r} "
        f"participants={len(participants)}"
    )


async def _refresh_state_if_needed(
    state: ClineBridgeState, bridge_dir: Path
) -> ClineBridgeState:
    """Re-run ``cline hub start`` and persist fresh coordinates when they changed.

    A hub restart rotates the auth token, so persisted state goes stale; the
    workspace/binary needed to re-ensure the hub are carried in the state
    itself.
    """
    from omnigent.harnesses.cline_native.serve import ensure_cline_hub, token_fingerprint

    if not state.workspace:
        raise ClineHubError(
            "cline hub coordinates stale and bridge state has no workspace to re-ensure"
        )
    endpoint = await ensure_cline_hub(
        cwd=state.workspace, executable=state.cline_command or None
    )
    refreshed = dataclasses.replace(
        state, ws_url=endpoint.ws_url, auth_token=endpoint.auth_token
    )
    write_bridge_state(bridge_dir, refreshed)
    logger.info(
        "cline-native: hub re-ensured at %s (token fp %s)",
        endpoint.ws_url,
        token_fingerprint(endpoint.auth_token),
    )
    return refreshed


async def _connect(
    state: ClineBridgeState, bridge_dir: Path
) -> tuple[Any, ClineBridgeState]:
    """Connect to the hub, refreshing stale coordinates once on failure."""
    from omnigent.harnesses.cline_native.client import ClineHubClient

    try:
        client = await ClineHubClient.connect(
            state.ws_url, state.auth_token, workspace_root=state.workspace, cwd=state.workspace
        )
        return client, state
    except (ClineHubError, OSError) as exc:
        logger.info(
            "cline-native: hub connect failed (%s); refreshing coordinates", exc
        )
    state = await _refresh_state_if_needed(state, bridge_dir)
    client = await ClineHubClient.connect(
        state.ws_url, state.auth_token, workspace_root=state.workspace, cwd=state.workspace
    )
    return client, state


def _require_live_session(record: dict[str, Any], session_id: str) -> None:
    """Raise a diagnostic error when the session can't accept input."""
    status = str(record.get("status") or "")
    if status in _DEAD_SESSION_STATUSES:
        raise ClineHubError(
            f"cline hub session {session_id} is {status} and cannot accept input "
            f"({_describe_session_record(record)}); recreate the cline terminal"
        )


async def _prompt_session(
    state: ClineBridgeState,
    text: str,
    bridge_dir: Path,
    omnigent_session_id: str,
) -> None:
    """Connect to the hub and drive one prompt turn.

    Subscribes to the session's event stream on the prompting connection so
    ``approval.requested`` events route through Omnigent's
    ``native-permission-request`` hook — the ASK leg of the policy bridge —
    and answers them with ``approval.respond``. The ``send_input`` reply only
    lands when the run settles, so there is no artificial per-turn deadline;
    a dropped socket reconnects once and either awaits the still-running turn
    or resubmits the prompt.
    """
    started = time.monotonic()
    client, state = await _connect(state, bridge_dir)
    try:
        record = await client.session_get(state.session_id)
    except ClineHubError as exc:
        await client.close()
        raise ClineHubError(
            f"cline hub session {state.session_id} lookup failed: {exc}; "
            "recreate the cline terminal"
        ) from exc
    logger.info(
        "cline-native: session %s before prompt: %s",
        state.session_id,
        _describe_session_record(record),
    )
    _require_live_session(record, state.session_id)
    # Attach keeps us a session participant (TUI parity); dead sessions still
    # answer attach, so the status check above stays the authority.
    with contextlib.suppress(ClineHubError):
        await client.session_attach(state.session_id)
    client.set_event_handler(_approval_event_handler(client, state, omnigent_session_id))
    await client.subscribe(state.session_id)
    try:
        reply = await client.send_input(state.session_id, text, timeout_s=None)
    except (ClineHubError, OSError) as exc:
        await client.close()
        logger.info(
            "cline-native: send_input dropped for session %s (%s); reconnecting",
            state.session_id,
            exc,
        )
        reply = await _resume_or_resend(state, text, bridge_dir)
        logger.info(
            "cline-native: prompt settled for session %s after reconnect in %.1fs",
            state.session_id,
            time.monotonic() - started,
        )
        return
    finally:
        with contextlib.suppress(Exception):
            await client.close()
    logger.info(
        "cline-native: prompt settled for session %s in %.1fs (result keys: %s)",
        state.session_id,
        time.monotonic() - started,
        sorted(reply.keys()),
    )


async def _resume_or_resend(
    state: ClineBridgeState, text: str, bridge_dir: Path
) -> dict[str, Any]:
    """Recover a dropped ``send_input``: await the live run or resubmit once."""
    import asyncio

    client, state = await _connect(state, bridge_dir)
    try:
        try:
            record = await client.session_get(state.session_id)
        except ClineHubError as exc:
            raise ClineHubError(
                f"cline hub session {state.session_id} lookup failed after "
                f"reconnect: {exc}"
            ) from exc
        _require_live_session(record, state.session_id)
        if str(record.get("status") or "") == "running":
            # The original send_input is still being processed — resubmitting
            # would queue a duplicate prompt, so just await its terminal event.
            run_done: asyncio.Future = asyncio.get_event_loop().create_future()

            def _on_run_event(envelope: dict[str, Any]) -> None:
                if (
                    envelope.get("sessionId") == state.session_id
                    and envelope.get("event") in _RUN_TERMINAL_EVENTS
                    and not run_done.done()
                ):
                    run_done.set_result(envelope)

            client.set_event_handler(_on_run_event)
            await client.subscribe(state.session_id)
            envelope = await asyncio.wait_for(run_done, _RUN_SETTLE_TIMEOUT_S)
            if envelope.get("event") in {"run.failed", "run.aborted"}:
                raise ClineHubError(
                    f"cline run on session {state.session_id} ended with "
                    f"{envelope.get('event')}: {envelope.get('payload')}"
                )
            return {"result": envelope.get("payload") or {}}
        # Session settled before or during the drop — the prompt may never
        # have been admitted; resubmit once.
        with contextlib.suppress(ClineHubError):
            await client.session_attach(state.session_id)
        return await client.send_input(state.session_id, text, timeout_s=None)
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
