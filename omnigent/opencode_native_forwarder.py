"""SSE consumer that mirrors OpenCode events into an Omnigent session.

The runner owns this forwarder (parallel to the codex-native forwarder).
It connects to the per-session ``opencode serve`` SSE stream (``GET
/event``), filters to the session's OpenCode session id, and translates
OpenCode events into Omnigent session-stream events posted to
``/v1/sessions/{id}/events`` — the same envelope the codex forwarder uses
(``external_conversation_item`` / ``external_session_status`` /
``external_output_text_delta``).

Design references: the SSE-event → Omnigent-event translation table in
``designs/opencode-harness-and-unified-interface.md`` §A.9. The forwarder
is tolerant of unknown events (logged, never fatal) and dedupes by stable
OpenCode message / part / tool-call ids so web and TUI driving the same
session never double-post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from omnigent.opencode_native_bridge import update_active_message_id, update_last_event_id
from omnigent.opencode_native_client import OpenCodeClient, OpenCodeEvent
from omnigent.opencode_native_permissions import (
    PolicyDecision,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
    reply_body,
)

_logger = logging.getLogger(__name__)

_AGENT_NAME = "opencode"
# Omnigent session-event types (must match the server's ingestion route;
# shared with the codex-native forwarder).
_EXTERNAL_ITEM = "external_conversation_item"
_EXTERNAL_STATUS = "external_session_status"

_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"

# Bound the dedupe set so a long-lived session can't grow it without limit.
_MAX_DEDUPE_KEYS = 8192

# Policy verdict resolver: receives a normalized policy input and returns a
# verdict mapping (or None when no policy is configured / reachable).
PolicyEvaluator = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]]


@dataclass
class OpenCodeForwarderState:
    """
    Mutable per-run forwarder state.

    :param seen: Bounded set of dedupe keys already posted.
    :param turn_active: Whether a turn is currently streaming.
    """

    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    turn_active: bool = False

    def mark(self, key: str) -> bool:
        """
        Record *key*; return ``True`` the first time it is seen.

        :param key: Stable dedupe key, e.g. ``"opencode:ses:msg:prt"``.
        :returns: ``True`` when newly seen, ``False`` for a duplicate.
        """
        if key in self.seen:
            return False
        self.seen[key] = None
        while len(self.seen) > _MAX_DEDUPE_KEYS:
            self.seen.popitem(last=False)
        return True


class OpenCodeNativeForwarder:
    """
    Translate one OpenCode session's SSE stream into Omnigent events.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param opencode_session_id: OpenCode session id to filter on.
    :param opencode_client: Client connected to the ``opencode serve``
        server (for SSE + permission replies).
    :param server_client: HTTP client for the Omnigent server (event posts).
    :param bridge_dir: Native OpenCode bridge directory (status/active-id
        persistence). ``None`` disables bridge writes (tests).
    :param workspace: Session workspace, used for permission normalization.
    :param policy_evaluator: Optional async policy resolver. Production
        wires one that POSTs each request to
        ``/v1/sessions/{id}/policies/evaluate`` (see
        ``omnigent.runner.app._build_opencode_policy_evaluator``) — the SAME
        server gate codex-native's policy hook uses, where an ``ask`` verdict
        is parked as a human approval card and blocks until a human resolves
        it. ``None`` uses *default_decision* for every request.
    :param default_decision: Decision used when no evaluator is provided or
        it returns ``None`` (evaluator unreachable / no verdict). Defaults to
        ``reject`` so an unconfigured or unreachable policy FAILS CLOSED — a
        headless OpenCode turn must NEVER silently auto-approve a sensitive
        operation. Only an explicit policy ``allow`` reaches
        ``once``/``always``.
    """

    def __init__(
        self,
        *,
        session_id: str,
        opencode_session_id: str,
        opencode_client: OpenCodeClient,
        server_client: httpx.AsyncClient,
        bridge_dir: Path | None = None,
        workspace: str | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        default_decision: PolicyDecision = "reject",
    ) -> None:
        self._session_id = session_id
        self._opencode_session_id = opencode_session_id
        self._opencode = opencode_client
        self._server = server_client
        self._bridge_dir = bridge_dir
        self._workspace = workspace
        self._policy_evaluator = policy_evaluator
        self._default_decision = default_decision
        self.state = OpenCodeForwarderState()
        # messageID -> role ("user"/"assistant"), learned from
        # ``message.updated``. Only assistant text parts become durable chat
        # items (a user part is already echoed by the client).
        self._msg_role: dict[str, str] = {}
        # partID -> (assistant messageID, latest full-text snapshot) for
        # in-flight assistant text parts, finalized (posted once) on
        # ``step-finish`` / ``session.idle``. The messageID becomes the item's
        # per-turn ``response_id``.
        self._pending_text: dict[str, tuple[str | None, str]] = {}

    async def seed_dedupe_from_history(self) -> None:
        """
        Pre-seed dedupe state from existing OpenCode messages.

        Prevents re-posting prior history on a resume/reconnect. Best
        effort: a failure leaves the dedupe set empty (at worst a few
        re-posts on resume).
        """
        try:
            messages = await self._opencode.list_messages(self._opencode_session_id)
        except Exception:  # noqa: BLE001 - seeding is best effort.
            _logger.debug("OpenCode forwarder could not seed dedupe from history", exc_info=True)
            return
        for message in messages:
            info = message.get("info") if isinstance(message, Mapping) else None
            message_id = info.get("id") if isinstance(info, Mapping) else None
            role = info.get("role") if isinstance(info, Mapping) else None
            if isinstance(message_id, str) and isinstance(role, str):
                self._msg_role[message_id] = role
            parts = message.get("parts") if isinstance(message, Mapping) else None
            if isinstance(parts, list):
                for part in parts:
                    if not isinstance(part, Mapping):
                        continue
                    part_id = part.get("id")
                    if isinstance(part_id, str):
                        self.state.mark(self._key("part", part_id))
                    # Pre-mark the keys the live handlers check so a resume
                    # never re-posts already-finalized text / tool parts.
                    if part.get("type") == "text" and isinstance(part_id, str):
                        # Pre-mark both the assistant-finalize and user-message
                        # keys so a resume re-posts neither.
                        self.state.mark(self._key("text-final", part_id))
                        self.state.mark(self._key("user-text", part_id))
                    if part.get("type") == "tool":
                        call_id = part.get("callID")
                        if isinstance(call_id, str):
                            self.state.mark(self._key("tool-call", call_id))
                            self.state.mark(self._key("tool-out", call_id))
            if isinstance(message_id, str):
                self.state.mark(self._key("message", message_id))

    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used
            by tests to bound the loop.
        """
        await self.seed_dedupe_from_history()
        attempt = 0
        backoff = 0.5
        while True:
            try:
                await self._consume_once()
                # Clean stream end (server closed): reconnect.
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on any transient SSE failure.
                _logger.warning(
                    "OpenCode forwarder SSE error for session=%s; reconnecting",
                    self._session_id,
                    exc_info=True,
                )
            attempt += 1
            if max_reconnects is not None and attempt > max_reconnects:
                return
            await asyncio.sleep(min(backoff, 5.0))
            backoff = min(backoff * 2, 5.0)

    async def _consume_once(self) -> None:
        """Consume the SSE stream once, dispatching each event."""
        async for event in self._opencode.events():
            await self.handle_event(event)

    async def handle_event(self, event: OpenCodeEvent) -> None:
        """
        Translate one OpenCode event into Omnigent session events.

        :param event: A decoded OpenCode SSE event.
        """
        if not self._event_targets_session(event):
            return
        if event.id and self._bridge_dir is not None:
            update_last_event_id(self._bridge_dir, event.id)
        handler = _HANDLERS.get(event.type)
        if handler is None:
            _logger.debug(
                "OpenCode forwarder ignoring event type=%s for session=%s",
                event.type,
                self._session_id,
            )
            return
        await handler(self, event)

    # --- filtering -------------------------------------------------------

    def _event_targets_session(self, event: OpenCodeEvent) -> bool:
        """
        Return whether *event* belongs to this forwarder's session.

        Events without a session id (e.g. ``server.connected``) pass
        through so readiness/global signals are not dropped.

        :param event: A decoded OpenCode event.
        :returns: ``True`` when the event should be handled.
        """
        props = event.properties
        session_id = props.get("sessionID") or props.get("session_id")
        info = props.get("info")
        if session_id is None and isinstance(info, Mapping):
            session_id = info.get("id")
        if session_id is None:
            return True
        return bool(session_id == self._opencode_session_id)

    # --- dedupe / keys ---------------------------------------------------

    def _key(self, *parts: str) -> str:
        """
        Build a session-scoped dedupe key.

        :param parts: Key segments, e.g. ``("text", "prt_1")``.
        :returns: ``"opencode:<sessionID>:<part>:..."``.
        """
        return "opencode:" + ":".join((self._opencode_session_id, *parts))

    # --- posting helpers -------------------------------------------------

    async def _post_event(self, event_type: str, data: dict[str, Any]) -> httpx.Response | None:
        """
        POST one Omnigent session event with a single retry.

        :param event_type: Omnigent event type, e.g.
            ``"external_session_status"``.
        :param data: Event data payload.
        :returns: The HTTP response, or ``None`` on transport failure.
        """
        url = f"/v1/sessions/{quote(self._session_id, safe='')}/events"
        payload = {"type": event_type, "data": data}
        try:
            return await self._server.post(url, json=payload)
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode forwarder failed to post %s for session=%s",
                event_type,
                self._session_id,
                exc_info=True,
            )
            return None

    async def _post_status(self, status: str) -> None:
        """Publish a coarse session status edge."""
        await self._post_event(_EXTERNAL_STATUS, {"status": status})

    def _response_id(self, message_id: str | None) -> str:
        """Map an opencode assistant messageID to a per-turn ``response_id``.

        Items are grouped into a chat "response" by ``response_id``; a constant
        value clusters every turn's assistant items into one block (breaking
        ordering against the user messages). opencode's per-assistant-message id
        is the natural per-turn key — fall back to the session id only when the
        message id is unknown.
        """
        return message_id or self._opencode_session_id

    async def _post_assistant_text(self, text: str, *, message_id: str | None) -> None:
        """Persist a finalized assistant message under its per-turn response."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": _AGENT_NAME,
                    "content": [{"type": "output_text", "text": text}],
                },
                "response_id": self._response_id(message_id),
            },
        )

    async def _post_user_text(self, text: str, *, message_id: str | None) -> None:
        """Persist a user message mirrored from the native transcript."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
                "response_id": self._response_id(message_id),
            },
        )

    async def _post_tool_call(
        self, call_id: str, tool: str, arguments: dict[str, Any], *, message_id: str | None
    ) -> None:
        """Mirror a tool invocation as a function_call item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call",
                "item_data": {
                    "agent": _AGENT_NAME,
                    "name": tool,
                    "arguments": json.dumps(arguments, ensure_ascii=True),
                    "call_id": call_id,
                },
                "response_id": self._response_id(message_id),
            },
        )

    async def _post_tool_output(
        self, call_id: str, output: str, *, message_id: str | None
    ) -> None:
        """Mirror a tool result as a function_call_output item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call_output",
                "item_data": {"call_id": call_id, "output": output},
                "response_id": self._response_id(message_id),
            },
        )

    async def _begin_turn_if_needed(self) -> None:
        """Post a single ``running`` status at the start of a turn."""
        if not self.state.turn_active:
            self.state.turn_active = True
            await self._post_status(_STATUS_RUNNING)

    async def _end_turn(self) -> None:
        """Post ``idle`` and clear active state at turn end."""
        self.state.turn_active = False
        if self._bridge_dir is not None:
            update_active_message_id(self._bridge_dir, None, status="idle")
        await self._post_status(_STATUS_IDLE)

    # --- per-event handlers ----------------------------------------------

    async def _on_message_updated(self, event: OpenCodeEvent) -> None:
        """Handle ``message.updated`` — learn role; begin a turn for assistant.

        opencode attaches text/tool parts to a message id; the role lives on
        the message, not the part, so we cache it here to route parts.
        """
        info = event.properties.get("info")
        if not isinstance(info, Mapping):
            return
        message_id = info.get("id")
        role = info.get("role")
        if not isinstance(message_id, str) or not isinstance(role, str):
            return
        self._msg_role[message_id] = role
        if role == "assistant":
            if self._bridge_dir is not None:
                update_active_message_id(self._bridge_dir, message_id, status="busy")
            await self._begin_turn_if_needed()

    async def _on_part_updated(self, event: OpenCodeEvent) -> None:
        """Handle ``message.part.updated`` — text / tool / step-boundary parts."""
        part = event.properties.get("part")
        if not isinstance(part, Mapping):
            return
        part_type = part.get("type")
        if part_type == "text":
            # A native-server forwarder is the SOLE source of the conversation
            # transcript (omnigent persists no separate user item for these
            # harnesses — mirrors codex-native). So the USER message must be
            # posted here, BEFORE its assistant reply, or the chat shows the
            # assistant turns with no/late user messages. Assistant text is
            # accumulated and finalized on step/turn end.
            if self._msg_role.get(str(part.get("messageID"))) == "user":
                await self._post_user_text_part(part)
            else:
                self._accumulate_text_part(part)
        elif part_type == "tool":
            await self._handle_tool_part(part)
        elif part_type == "step-start":
            await self._begin_turn_if_needed()
        elif part_type == "step-finish":
            # A step's assistant text is complete once the step closes; flush
            # it so text and tool items land in the chat in step order.
            await self._flush_pending_text()

    def _accumulate_text_part(self, part: Mapping[str, Any]) -> None:
        """Record the latest full-text snapshot for an assistant text part.

        ``message.part.updated`` carries the cumulative text each time, so we
        keep the latest snapshot and finalize it once on step/turn end.
        """
        part_id = part.get("id")
        text = part.get("text")
        message_id = part.get("messageID")
        if not isinstance(part_id, str) or not isinstance(text, str):
            return
        # User-message text is echoed by the client; only assistant text
        # becomes a durable chat item.
        if self._msg_role.get(str(message_id)) != "assistant":
            return
        # Keep the owning messageID so the finalized item lands under the right
        # per-turn response group (ordering vs the user messages).
        self._pending_text[part_id] = (
            message_id if isinstance(message_id, str) else None,
            text,
        )

    async def _post_user_text_part(self, part: Mapping[str, Any]) -> None:
        """Post a user message immediately so it precedes its assistant reply.

        Unlike assistant text (accumulated + flushed on step end), a user part
        is complete on arrival and must land at its own earlier position, so we
        post it eagerly, deduped by part id.
        """
        part_id = part.get("id")
        text = part.get("text")
        message_id = part.get("messageID")
        if not isinstance(part_id, str) or not isinstance(text, str) or not text:
            return
        if not self.state.mark(self._key("user-text", part_id)):
            return
        await self._post_user_text(
            text, message_id=message_id if isinstance(message_id, str) else None
        )

    async def _flush_pending_text(self) -> None:
        """Finalize accumulated assistant text parts as durable chat items."""
        for part_id, (message_id, text) in list(self._pending_text.items()):
            self._pending_text.pop(part_id, None)
            if not text:
                continue
            if not self.state.mark(self._key("text-final", part_id)):
                continue
            await self._post_assistant_text(text, message_id=message_id)

    async def _handle_tool_part(self, part: Mapping[str, Any]) -> None:
        """Mirror an opencode tool part (call + result) as chat items.

        opencode reports a tool as a single part whose ``state`` advances
        ``pending`` → ``running`` → ``completed`` / ``error`` with ``input``
        then ``output``; we post the call once its input is populated and the
        output once it completes (deduped by ``callID``).
        """
        call_id = part.get("callID")
        tool = part.get("tool")
        state = part.get("state")
        if not isinstance(call_id, str) or not isinstance(tool, str):
            return
        if not isinstance(state, Mapping):
            return
        message_id = part.get("messageID")
        response_message_id = message_id if isinstance(message_id, str) else None
        raw_input = state.get("input")
        arguments = raw_input if isinstance(raw_input, dict) else {}
        if arguments and self.state.mark(self._key("tool-call", call_id)):
            await self._begin_turn_if_needed()
            await self._post_tool_call(call_id, tool, arguments, message_id=response_message_id)
        status = state.get("status")
        if status == "completed" and self.state.mark(self._key("tool-out", call_id)):
            await self._post_tool_output(
                call_id, _tool_output_text(state), message_id=response_message_id
            )
        elif status == "error" and self.state.mark(self._key("tool-out", call_id)):
            error = state.get("error")
            await self._post_tool_output(
                call_id, f"[error] {error}" if error else "[error]", message_id=response_message_id
            )

    async def _on_session_status(self, event: OpenCodeEvent) -> None:
        """Handle ``session.status`` — surface the running edge."""
        status = event.properties.get("status")
        status_type = status.get("type") if isinstance(status, Mapping) else status
        if status_type == "busy":
            await self._begin_turn_if_needed()

    async def _on_session_idle(self, event: OpenCodeEvent) -> None:
        """Handle ``session.idle`` — finalize text and end the turn."""
        del event
        await self._flush_pending_text()
        await self._end_turn()

    async def _on_session_error(self, event: OpenCodeEvent) -> None:
        """Handle ``session.error`` — log, finalize, end turn."""
        _logger.warning(
            "OpenCode session error for session=%s: %s",
            self._session_id,
            event.properties.get("error"),
        )
        await self._flush_pending_text()
        await self._end_turn()

    async def _on_permission_asked(self, event: OpenCodeEvent) -> None:
        """Handle ``permission.v2.asked`` — evaluate policy and reply."""
        request = parse_permission_request(event.properties)
        if request is None:
            return
        if not self.state.mark(self._key("perm", request.request_id)):
            return
        decision = await self._resolve_permission(request_dict=request)
        reply = decision_to_reply(decision)
        if reply is None:
            # Fail closed. ``decision_to_reply`` returns ``None`` only for
            # ``ask``. The genuine human approval for an ``ask`` happens
            # UPSTREAM inside the policy evaluator (the server parks an
            # approval card on ``/policies/evaluate`` and returns a hard
            # allow/deny). So an ``ask`` still reaching here means no human
            # resolution was obtained — which must DENY, never auto-approve.
            reply = "reject"
        try:
            await self._opencode.reply_permission(
                request.request_id, reply_body(reply, message="omnigent-policy")
            )
        except Exception:  # noqa: BLE001 - reply is best effort; log and move on.
            _logger.warning(
                "OpenCode permission reply failed for request=%s",
                request.request_id,
                exc_info=True,
            )

    async def _resolve_permission(self, *, request_dict: Any) -> PolicyDecision:
        """
        Resolve a permission request to a normalized decision.

        :param request_dict: The parsed permission request.
        :returns: The normalized policy decision.
        """
        if self._policy_evaluator is None:
            # No policy gate wired → fail closed (default ``reject``). A
            # forwarder with no evaluator must never auto-approve.
            return self._default_decision
        normalized = normalize_for_policy(
            request_dict,
            omnigent_session_id=self._session_id,
            workspace=self._workspace,
        )
        try:
            verdict = await self._policy_evaluator(normalized)
        except Exception:  # noqa: BLE001 - policy errors fail closed.
            _logger.warning("OpenCode policy evaluation failed", exc_info=True)
            return "ask"
        if verdict is None:
            return self._default_decision
        return map_verdict_to_decision(verdict)


def _tool_output_text(state: Mapping[str, Any]) -> str:
    """
    Extract a string tool output from a completed tool part's ``state``.

    :param state: The opencode tool part ``state`` (``output`` /
        ``metadata.output``).
    :returns: A string suitable for ``function_call_output``.
    """
    output = state.get("output")
    if isinstance(output, str) and output:
        return output
    metadata = state.get("metadata")
    if isinstance(metadata, Mapping):
        meta_out = metadata.get("output")
        if isinstance(meta_out, str) and meta_out:
            return meta_out
    if output is not None and not isinstance(output, str):
        return json.dumps(output, ensure_ascii=True)
    return ""


# Event type → bound handler-name lookup. Built once; ``handle_event``
# resolves the method on the instance. Keys are OpenCode event ``type``
# discriminators (see openapi.json Event* schemas).
_HANDLERS: dict[str, Callable[[OpenCodeNativeForwarder, OpenCodeEvent], Awaitable[None]]] = {
    # opencode 1.17.x is part-based: text/tool live on message PARTS, lifecycle
    # on the message + session. (Verified against a real ``opencode serve``.)
    "message.updated": OpenCodeNativeForwarder._on_message_updated,
    "message.part.updated": OpenCodeNativeForwarder._on_part_updated,
    # NB: ``message.part.delta`` (live token stream) is intentionally NOT
    # forwarded. The web chat view reconciles live ``text_delta`` previews with
    # the committed item via a finalize/retire protocol; emitting deltas without
    # that handshake left an unreconciled streaming preview alongside the
    # committed message (duplicated/garbled chat). We post only the durable
    # assistant item (the codex-native finalized-message path) so the chat is
    # correct; live token-streaming is a separate follow-up.
    "session.status": OpenCodeNativeForwarder._on_session_status,
    "session.idle": OpenCodeNativeForwarder._on_session_idle,
    "session.error": OpenCodeNativeForwarder._on_session_error,
    # Permission ask: 1.17.x emits ``permission.asked``; keep the ``v2`` spelling
    # too so a point-release rename still routes through the policy gate.
    "permission.asked": OpenCodeNativeForwarder._on_permission_asked,
    "permission.v2.asked": OpenCodeNativeForwarder._on_permission_asked,
}
