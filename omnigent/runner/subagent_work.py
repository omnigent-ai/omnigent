"""Runner-local registry for asynchronous sub-agent dispatches.

Tracks each ``sys_session_send`` dispatch from launch to terminal status,
delivers terminal results to the parent session inbox, wakes the parent,
recovers undrained results after a runner restart, and records the
child→parent mapping used to mirror child status onto the parent stream.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse

from omnigent.debug_logging import runner_primary_session_id
from omnigent.native.native_coding_agents import native_coding_agent_for_harness
from omnigent.runner.policy_proxy import _ASK_GATE_DELIVERY_TIMEOUT
from omnigent.util.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    from omnigent.runner.native.interrupt import MarkSubagentTerminalAndWake

_logger = logging.getLogger("omnigent.runner.app")

_SUBAGENT_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


# Bound how long a sub-agent dispatch can wait for a start acknowledgment.
# A timeout reports uncertain launch status, not proof that the process is dead.
_SUBAGENT_LAUNCH_TIMEOUT_S_ENV = "OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S"
_DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S = 180.0
# Interval for the background sweep in the runner entrypoint.
SUBAGENT_LAUNCH_REAP_INTERVAL_S = 30.0


def resolve_subagent_launch_timeout_s() -> float:
    """
    Resolve the sub-agent launch liveness budget in seconds.

    Values ``<= 0`` disable the reaper. A non-numeric override is rejected
    with a warning and falls back to the default.

    :returns: The budget in seconds, e.g. ``180.0``.
    """
    raw = os.environ.get(_SUBAGENT_LAUNCH_TIMEOUT_S_ENV, "").strip()
    if not raw:
        return _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = None
    # Non-finite values (nan/inf) would silently disable reaping without the
    # explicit ``<= 0`` "disabled" intent — reject them like non-numeric input.
    if value is None or not math.isfinite(value):
        _logger.warning(
            "Invalid %s=%r; using default %ss",
            _SUBAGENT_LAUNCH_TIMEOUT_S_ENV,
            raw,
            _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S,
        )
        return _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    return value


_SUBAGENT_DELIVERY_DELIVERED = "delivered"
_SUBAGENT_DELIVERY_ALREADY_DELIVERED = "already_delivered"
_SUBAGENT_DELIVERY_UNTRACKED = "untracked"
_SUBAGENT_DELIVERY_MISSING_WORK_ENTRY = "missing_work_entry"
_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX = "missing_parent_inbox"
# A delayed terminal op (an interrupt's grace-timer cancel) whose originating
# dispatch has since been replaced by a newer send on the reused child session.
# Dropped without touching the newer dispatch so an old timer never cancels it.
_SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH = "superseded_dispatch"
# Runner-owned labels on a child session that make sub-agent result delivery
# durable across a runner restart. The dispatch id is stamped when a turn is
# sent to the child; the delivered id is the receipt the parent's
# ``sys_read_inbox`` drain writes once it has consumed that turn's result.
SUBAGENT_DISPATCH_ID_LABEL_KEY = "omnigent.subagent.dispatch_id"
SUBAGENT_DELIVERED_ID_LABEL_KEY = "omnigent.subagent.delivered_id"


# Bounded retry budget for the sub-agent wake POST. The wake is the sole
# delivery signal for the last child of a fan-out, and Omnigent routinely
# returns a transient 503 RUNNER_UNAVAILABLE while the parent's runner tunnel
# is reconnecting, so a single attempt can strand the parent silently.
_WAKE_POST_MAX_ATTEMPTS = 3
_WAKE_POST_RETRY_BASE_DELAY_S = 0.5
_WAKE_POST_RETRY_MAX_DELAY_S = 4.0


# 4xx statuses that are transient and worth retrying (mirrors the forwarder's
# classification): everything else in 4xx is a permanent client-side rejection.
_WAKE_POST_TRANSIENT_4XX = frozenset({408, 409, 425, 429})


@dataclasses.dataclass
class _SubagentWorkEntry:
    """
    Runner-local state for one asynchronous ``sys_session_send`` dispatch.

    :param parent_session_id: Parent session id that invoked
        ``sys_session_send``, e.g. ``"conv_parent123"``.
    :param child_session_id: Child session id used as the work handle,
        e.g. ``"conv_child456"``.
    :param work_id: Unique id for this dispatch to the child session,
        e.g. ``"subagent_a1b2c3"``.
    :param agent: Sub-agent name from the parent spec, e.g.
        ``"researcher"``.
    :param title: Caller-provided child instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional terminal wrapper label from the
        child session, e.g. ``"codex-native-ui"`` for codex-native
        native sub-agents.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param status: Current work status, e.g. ``"launching"`` or
        ``"running"``.
    :param output: Terminal child output or error text. ``None``
        while the work is still running.
    :param created_at: Unix timestamp when the dispatch was registered.
    :param completed_at: Unix timestamp when the dispatch reached a
        terminal status, or ``None`` while running.
    :param delivered: Whether the terminal payload has been pushed to
        the parent's inbox.
    :param cancellation_confirmed: Whether a native terminal edge confirmed
        an abort, rather than an interrupt merely being requested.
    """

    parent_session_id: str
    child_session_id: str
    work_id: str
    agent: str
    title: str
    wrapper_label: str | None = None
    created_by: str | None = None
    status: str = "launching"
    output: str | None = None
    created_at: float = dataclasses.field(default_factory=time.time)
    completed_at: float | None = None
    delivered: bool = False
    cancellation_confirmed: bool = False


@dataclasses.dataclass(frozen=True)
class _SubagentDeliveryAck:
    """
    Result of attempting to deliver a terminal sub-agent payload.

    :param entry: Work entry whose delivery was attempted, or ``None``
        when the child session is not tracked in the work registry.
    :param delivered: Whether the payload is confirmed delivered to the
        parent inbox. True for both first delivery and already-delivered
        duplicate terminal reports.
    :param delivered_now: Whether this attempt pushed a new payload into
        the parent inbox.
    :param reason: Machine-readable outcome, e.g. ``"delivered"`` or
        ``"missing_parent_inbox"``.
    """

    entry: _SubagentWorkEntry | None
    delivered: bool
    delivered_now: bool
    reason: str


_subagent_work_by_child: dict[str, _SubagentWorkEntry] = {}
_subagent_work_by_parent: dict[str, set[str]] = {}
_drained_delivered_subagent_children: set[str] = set()
# Parents whose restart-recovery scan completed in this process, plus a
# per-parent lock so an init racing a sys_read_inbox drain cannot run two
# scans that both pass the registry check and queue one result twice.
_subagent_recovery_done: set[str] = set()
_subagent_recovery_locks: dict[str, asyncio.Lock] = {}

# Per-(parent, agent_type) monotonic ordinal counter for structured
# sub-agent names (e.g. "researcher-1", "researcher-2").
_subagent_ordinal_counters: dict[tuple[str, str], int] = {}


def next_subagent_ordinal(parent_session_id: str, agent_type: str) -> int:
    """Return the next ordinal for a (parent, agent_type) pair and bump the counter."""
    key = (parent_session_id, agent_type)
    ordinal = _subagent_ordinal_counters.get(key, 0) + 1
    _subagent_ordinal_counters[key] = ordinal
    return ordinal


def recover_subagent_ordinals(
    parent_session_id: str,
    agent_type: str,
    existing_children: list[dict[str, object]],
) -> None:
    """Set the ordinal high-water mark from existing children after a runner restart."""
    import re

    key = (parent_session_id, agent_type)
    if key in _subagent_ordinal_counters:
        return
    pattern = re.compile(rf"^{re.escape(agent_type)}-(\d+)$")
    max_ordinal = 0
    for child in existing_children:
        session_name = child.get("session_name")
        if isinstance(session_name, str):
            m = pattern.match(session_name)
            if m:
                max_ordinal = max(max_ordinal, int(m.group(1)))
    _subagent_ordinal_counters[key] = max_ordinal


def new_subagent_work_id() -> str:
    """
    Mint the id of one sub-agent dispatch, e.g. ``"subagent_a1b2c3d4e5f6"``.

    :returns: A fresh dispatch id.
    """
    return f"subagent_{uuid.uuid4().hex[:12]}"


# Per-child locks serializing the classify+register step of an in-flight
# sub-agent send (see ``tool_dispatch._send_to_in_flight_child``), so two
# concurrent sends to one child can't install divergent work entries. Co-located
# with the work registries so it is torn down alongside them — otherwise a
# long-lived runner would accumulate one lock per steered child forever.
_in_flight_send_locks: dict[str, asyncio.Lock] = {}


def in_flight_send_lock(child_session_id: str) -> asyncio.Lock:
    """
    Return (creating on first use) the per-child in-flight-send lock.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The lock guarding that child's in-flight-send bookkeeping.
    """
    lock = _in_flight_send_locks.get(child_session_id)
    if lock is None:
        lock = asyncio.Lock()
        _in_flight_send_locks[child_session_id] = lock
    return lock


def register_subagent_work(
    *,
    parent_session_id: str,
    child_session_id: str,
    agent: str,
    title: str,
    wrapper_label: str | None = None,
    created_by: str | None = None,
    work_id: str | None = None,
) -> _SubagentWorkEntry:
    """
    Register one running sub-agent dispatch.

    Re-registering the same child replaces the prior entry so a
    repeated send to an existing child represents the latest turn.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param child_session_id: Child session id, e.g.
        ``"conv_child456"``.
    :param agent: Sub-agent name, e.g. ``"researcher"``.
    :param title: Sub-agent instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional child ``omnigent.wrapper``
        label, e.g. ``"claude-code-native-ui"``.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param work_id: Dispatch id already stamped on the child session,
        e.g. ``"subagent_a1b2c3d4e5f6"``; ``None`` mints a new one.
    :returns: The registered work entry.
    """
    prior = _subagent_work_by_child.get(child_session_id)
    if prior is not None:
        children = _subagent_work_by_parent.get(prior.parent_session_id)
        if children is not None:
            children.discard(child_session_id)
            if not children:
                _subagent_work_by_parent.pop(prior.parent_session_id, None)

    entry = _SubagentWorkEntry(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        work_id=work_id or new_subagent_work_id(),
        agent=agent,
        title=title,
        wrapper_label=wrapper_label,
        created_by=created_by,
    )
    _drained_delivered_subagent_children.discard(child_session_id)
    _subagent_work_by_child[child_session_id] = entry
    _subagent_work_by_parent.setdefault(parent_session_id, set()).add(child_session_id)
    return entry


def get_subagent_work(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Return registered sub-agent work by child session id.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The work entry, or ``None`` if the child is not tracked.
    """
    return _subagent_work_by_child.get(child_session_id)


def mark_subagent_work_started(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Promote a sub-agent dispatch from launch bookkeeping to real execution.

    ``sys_session_send`` creates the child session and registers work before
    the child harness has proven it started. The first child
    ``session.status:running`` / ``waiting`` edge is that proof.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The updated work entry, or ``None`` if the child is untracked.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return None
    if entry.status in {"launching", "waiting"}:
        entry.status = "running"
    return entry


def unregister_subagent_work(
    child_session_id: str,
    *,
    work_id: str | None = None,
    remember_drained_delivery: bool = False,
) -> None:
    """
    Remove sub-agent work tracking for a child session.

    Used when the child-message POST fails before a handle has been
    returned to the LLM.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param work_id: Optional dispatch id guard. When provided, the
        current registry entry is removed only if it still belongs to
        that dispatch.
    :param remember_drained_delivery: Whether to remember a delivered
        entry as drained so duplicate terminal status reports for the
        same child are acknowledged as already delivered.
    :returns: None.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return
    if work_id is not None and entry.work_id != work_id:
        return
    if remember_drained_delivery and entry.delivered:
        _drained_delivered_subagent_children.add(child_session_id)
    _subagent_work_by_child.pop(child_session_id, None)
    _in_flight_send_locks.pop(child_session_id, None)
    children = _subagent_work_by_parent.get(entry.parent_session_id)
    if children is None:
        return
    children.discard(child_session_id)
    if not children:
        _subagent_work_by_parent.pop(entry.parent_session_id, None)


def unregister_subagent_work_for_session(session_id: str) -> None:
    """
    Remove sub-agent work associated with a deleted session.

    A deleted session can be either the child work handle itself or
    the parent that owns several child handles. Both indexes are
    cleaned so runner-local state cannot outlive the session tree.

    :param session_id: Session id being deleted, e.g.
        ``"conv_parent123"`` or ``"conv_child456"``.
    :returns: None.
    """
    unregister_subagent_work(session_id)
    _drained_delivered_subagent_children.discard(session_id)
    _in_flight_send_locks.pop(session_id, None)
    for child_id in list(_subagent_work_by_parent.get(session_id, set())):
        _subagent_work_by_child.pop(child_id, None)
        _drained_delivered_subagent_children.discard(child_id)
        _in_flight_send_locks.pop(child_id, None)
    _subagent_work_by_parent.pop(session_id, None)


def list_subagent_work(parent_session_id: str) -> list[_SubagentWorkEntry]:
    """
    List sub-agent work registered by a parent session.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Work entries ordered by creation time.
    """
    child_ids = _subagent_work_by_parent.get(parent_session_id, set())
    entries = [
        entry
        for child_id in child_ids
        if (entry := _subagent_work_by_child.get(child_id)) is not None
    ]
    return sorted(entries, key=lambda entry: entry.created_at)


# Harness whose sub-agents live as threads inside the parent's own app-server.
_CODEX_NATIVE_HARNESS = "codex-native"


def is_codex_native_subagent_wrapper(wrapper_label: str | None) -> bool:
    """
    Whether a child's wrapper label marks it a codex-native sub-agent.

    Covers both a codex-spawned sub-agent and a ``/side`` side chat: each is a
    thread inside the parent's own app-server, so codex consumes its result in
    the thread tree and the parent is never waiting on the Omnigent inbox for it.

    :param wrapper_label: The child's ``omnigent.wrapper`` label, or ``None``.
    :returns: ``True`` when the child is a codex-native sub-agent.
    """
    if wrapper_label is None:
        return False
    agent = native_coding_agent_for_harness(_CODEX_NATIVE_HARNESS)
    return agent is not None and wrapper_label == agent.subagent_wrapper_label


def undelivered_subagent_dispatch_id(labels: Mapping[str, object]) -> str | None:
    """
    Return the dispatch id of a child turn whose result the parent never drained.

    :param labels: Child session labels, e.g.
        ``{"omnigent.subagent.dispatch_id": "subagent_a1b2c3d4e5f6"}``.
    :returns: The dispatch id when the delivered-id receipt is missing or
        names an earlier turn; ``None`` for a drained turn, or for a child
        created before dispatch ids were stamped.
    """
    dispatch_id = labels.get(SUBAGENT_DISPATCH_ID_LABEL_KEY)
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return None
    if labels.get(SUBAGENT_DELIVERED_ID_LABEL_KEY) == dispatch_id:
        return None
    return dispatch_id


class _SubagentRecoveryReadError(Exception):
    """A sessions API read needed by restart recovery returned a non-200."""


async def _get_recovery_page(
    server_client: httpx.AsyncClient, path: str, params: dict[str, str]
) -> Any:
    """
    Read one page of a sessions API listing for restart recovery.

    :param server_client: HTTP client connected to the Omnigent server.
    :param path: Sessions API path, e.g. ``"/v1/sessions/conv_p/child_sessions"``.
    :param params: Query parameters, e.g. ``{"limit": "1000"}``.
    :returns: The decoded JSON page.
    :raises _SubagentRecoveryReadError: When the server returns a non-200.
    """
    response = await server_client.get(path, params=params, timeout=10.0)
    if response.status_code != 200:
        raise _SubagentRecoveryReadError(f"{path} returned {response.status_code}")
    return response.json()


async def _list_child_sessions(
    server_client: httpx.AsyncClient, parent_id: str
) -> list[_JsonObject]:
    """
    Return every child-session summary of a parent, following pagination.

    :param server_client: HTTP client connected to the Omnigent server.
    :param parent_id: Parent session id, e.g. ``"conv_parent123"``.
    :returns: Child summaries as returned by the sessions API.
    :raises _SubagentRecoveryReadError: When a page read fails.
    """
    children: list[_JsonObject] = []
    params: dict[str, str] = {"limit": "1000"}
    while True:
        page = await _get_recovery_page(
            server_client, f"/v1/sessions/{parent_id}/child_sessions", params
        )
        children.extend(page.get("data", []))
        if not page.get("has_more") or not page.get("last_id"):
            return children
        params["after"] = page["last_id"]


async def _fetch_latest_assistant_text(
    server_client: httpx.AsyncClient, session_id: str
) -> str | None:
    """
    Return the newest assistant message text of a session, reading newest first.

    :param server_client: HTTP client connected to the Omnigent server.
    :param session_id: Session to read, e.g. ``"conv_child456"``.
    :returns: Joined text blocks of the newest assistant message (empty when
        that message carries no text, matching live delivery), or ``None``
        when the transcript holds no assistant message.
    :raises _SubagentRecoveryReadError: When a page read fails.
    """
    params: dict[str, str] = {"limit": "100", "order": "desc"}
    while True:
        page = await _get_recovery_page(server_client, f"/v1/sessions/{session_id}/items", params)
        for item in page.get("data", []):
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            return "\n".join(
                block["text"]
                for block in item.get("content", [])
                if block.get("type") in {"output_text", "text"} and block.get("text")
            )
        if not page.get("has_more") or not page.get("last_id"):
            return None
        params["after"] = page["last_id"]


async def _recover_subagent_results_from_server(
    *,
    server_client: httpx.AsyncClient,
    parent_id: str,
    schedule_wake: Callable[[_SubagentWorkEntry], None],
) -> None:
    """
    Re-queue terminal child results whose delivery receipt is missing.

    A child turn is stamped with a dispatch id when it is sent, and the
    parent's ``sys_read_inbox`` drain writes that id back as the delivered
    id. A terminal child whose two ids differ was never drained, so its
    result is rebuilt from the child transcript and queued again under the
    same dispatch id, letting the eventual drain close the loop.

    :param server_client: HTTP client connected to the Omnigent server.
    :param parent_id: Parent session whose inbox was recreated, e.g.
        ``"conv_parent123"``.
    :param schedule_wake: Callback that posts the parent wake notice.
    :raises _SubagentRecoveryReadError: When a server read returns a
        non-200; the caller retries on the next drain.
    """
    for child in await _list_child_sessions(server_client, parent_id):
        child_id = child.get("id")
        status = child.get("current_task_status")
        if not isinstance(child_id, str) or not isinstance(status, str):
            continue
        error = child.get("last_task_error")
        interrupted = status == "in_progress" or (
            status == "failed"
            and isinstance(error, dict)
            and error.get("code") in {"runner_disconnected", "runner_failed_to_start"}
        )
        if status not in _SUBAGENT_TERMINAL_STATUSES and not interrupted:
            continue
        existing = get_subagent_work(child_id)
        if (existing is not None and existing.status != "waiting") or (
            child_id in _drained_delivered_subagent_children
        ):
            continue
        labels = child.get("labels")
        dispatch_id = undelivered_subagent_dispatch_id(labels if isinstance(labels, dict) else {})
        if dispatch_id is None or (existing is not None and existing.work_id != dispatch_id):
            continue
        output: str | None = None
        if status == "failed":
            error = child.get("last_task_error")
            message = error.get("message") if isinstance(error, dict) else None
            output = message if isinstance(message, str) else None
        elif not interrupted:
            output = await _fetch_latest_assistant_text(server_client, child_id)
        # A forwarded completion or newer dispatch may arrive during the history read.
        if (
            get_subagent_work(child_id) is not existing
            or (existing is not None and existing.status != "waiting")
            or child_id in _drained_delivered_subagent_children
        ):
            continue
        entry = existing or register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent=str(child.get("tool") or child.get("agent_name") or "sub-agent"),
            title=str(child.get("session_name") or ""),
            work_id=dispatch_id,
        )
        if interrupted:
            # This dispatch already existed; a local launch timeout cannot judge it.
            entry.status = "waiting"
            continue
        ack = mark_subagent_work_terminal(child_id, status=status, output=output)
        if ack.delivered_now:
            schedule_wake(entry)


def mark_subagent_work_terminal(
    child_session_id: str,
    *,
    status: str,
    output: str | None,
    only_if_work_id: str | None = None,
) -> _SubagentDeliveryAck:
    """
    Mark a sub-agent dispatch terminal and notify the parent inbox.

    A delivered terminal status is final: it is not overturned by a later
    report for the same child (the ``failed``-over-``completed`` edge-race
    exception aside). Distinguishing a confirmed completion from a bare
    quiescence idle is the caller's job (the events route only reports
    ``completed`` for a confirmed turn-end or a legacy harness).

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param status: Terminal status: ``"completed"``, ``"failed"``, or
        ``"cancelled"``.
    :param output: Child output or error text. ``None`` means the
        completion had no assistant text to deliver.
        If an earlier terminal report could not be delivered, a later
        report for the same child replaces the undelivered status and
        output before retrying parent inbox delivery.
    :param only_if_work_id: When set, apply this report only if the current
        entry is still that dispatch. A delayed op (an interrupt's grace-timer
        cancel) bound to the dispatch it was raised for is dropped when a newer
        send has replaced the entry, so an old timer never cancels new work.
    :returns: Delivery acknowledgement for this terminal report.
    :raises ValueError: If ``status`` is not terminal.
    """
    if status not in _SUBAGENT_TERMINAL_STATUSES:
        raise ValueError(
            f"sub-agent terminal status must be one of "
            f"{sorted(_SUBAGENT_TERMINAL_STATUSES)}; got {status!r}"
        )
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        if child_session_id in _drained_delivered_subagent_children:
            return _SubagentDeliveryAck(
                entry=None,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        return _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_UNTRACKED,
        )
    if only_if_work_id is not None and entry.work_id != only_if_work_id:
        # A delayed op raised for an earlier dispatch: a newer send replaced the
        # entry on this reused child session. Drop it untouched so an old
        # interrupt's grace timer can never cancel the new dispatch.
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH,
        )
    if entry.status in _SUBAGENT_TERMINAL_STATUSES:
        # ``failed`` outranks ``completed``: a quiescence-derived ``completed``
        # (the watcher's ``idle`` edge) can be recorded — and delivered — before
        # the turn's real ``failed`` edge lands. The failure must replace it and
        # be re-delivered, or the parent is left believing the turn succeeded
        # and the error text is silently dropped. A parent may act on the false
        # success before the re-delivery arrives — that window is inherent to
        # the edge race; re-delivery is the mitigation, not a prevention.
        if status == "failed" and entry.status == "completed":
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
            entry.delivered = False
            return _deliver_subagent_completion(entry)
        if entry.delivered:
            return _SubagentDeliveryAck(
                entry=entry,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        # A late stop_session-driven "cancelled" must not downgrade an
        # already-recorded "completed"/"failed" still awaiting delivery, and a
        # trailing quiescence "completed" must not launder a recorded "failed".
        keep_recorded = (status == "cancelled" and entry.status != "cancelled") or (
            status == "completed" and entry.status == "failed"
        )
        if not keep_recorded:
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
        return _deliver_subagent_completion(entry)
    entry.status = status
    entry.output = output
    entry.completed_at = time.time()
    return _deliver_subagent_completion(entry)


def _deliver_subagent_completion(entry: _SubagentWorkEntry) -> _SubagentDeliveryAck:
    """
    Push a terminal sub-agent payload into the parent session inbox.

    :param entry: Terminal sub-agent work entry to deliver.
    :returns: Delivery acknowledgement describing whether the payload is
        confirmed in the parent inbox.
    """
    if entry.delivered:
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
        )
    inbox = _session_inboxes_ref.get(entry.parent_session_id)
    if inbox is None:
        _logger.warning(
            "Sub-agent work completed but parent inbox is missing; parent=%s child=%s",
            entry.parent_session_id,
            entry.child_session_id,
        )
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX,
        )
    output = entry.output
    if output is None:
        # Only a completion needs the explicit no-output marker; a cancelled
        # dispatch legitimately has nothing to report.
        output = (
            "[System: sub-agent completed with no output]" if entry.status == "completed" else ""
        )
    inbox.put_nowait(
        {
            "type": "sub_agent",
            "work_id": entry.work_id,
            "task_id": entry.child_session_id,
            "handle_id": entry.child_session_id,
            "conversation_id": entry.child_session_id,
            "tool_name": entry.agent,
            "agent": entry.agent,
            "title": entry.title,
            "status": entry.status,
            "output": output,
        }
    )
    entry.delivered = True
    return _SubagentDeliveryAck(
        entry=entry,
        delivered=True,
        delivered_now=True,
        reason=_SUBAGENT_DELIVERY_DELIVERED,
    )


def reap_stalled_subagent_launches(
    *,
    now: float | None = None,
    timeout_s: float | None = None,
    mark_terminal: MarkSubagentTerminalAndWake | None = None,
) -> list[_SubagentWorkEntry]:
    """
    Fail sub-agent dispatches stuck in ``launching`` beyond the liveness budget.

    A dispatch with no running/waiting/terminal status acknowledgment can
    otherwise remain pending forever. Missing acknowledgment does not prove
    that the child process never started. Each reaped entry is
    marked ``failed`` and its failure is delivered to the parent inbox through
    ``mark_terminal``.

    :param now: Clock override for tests, e.g. ``time.time()``.
    :param timeout_s: Budget override for tests; defaults to
        :func:`resolve_subagent_launch_timeout_s`.
    :param mark_terminal: Terminal-delivery callback. Production passes the
        app's ``mark_subagent_terminal_and_wake`` seam so the reaped failure
        also schedules the parent wake POST — the sole signal that rouses an
        idle parent to drain its inbox. Defaults to the inbox-only
        :func:`mark_subagent_work_terminal`.
    :returns: The entries that were failed by this sweep.
    """
    budget = resolve_subagent_launch_timeout_s() if timeout_s is None else timeout_s
    if budget <= 0:
        return []
    deliver = mark_subagent_work_terminal if mark_terminal is None else mark_terminal
    current = time.time() if now is None else now
    reaped: list[_SubagentWorkEntry] = []
    for entry in list(_subagent_work_by_child.values()):
        if entry.status != "launching":
            continue
        if current - entry.created_at < budget:
            continue
        _logger.warning(
            "Sub-agent dispatch stuck in launching for %.0fs; failing it: parent=%s child=%s",
            current - entry.created_at,
            entry.parent_session_id,
            entry.child_session_id,
        )
        deliver(
            entry.child_session_id,
            status="failed",
            output=(
                f"Error: no start acknowledgment for sub-agent {entry.agent!r} "
                f"title {entry.title!r} within {budget:.0f}s of dispatch. "
                "The child may still be running; inspect its session before retrying."
            ),
        )
        reaped.append(entry)
    return reaped


async def run_subagent_launch_reaper(
    *,
    interval_s: float = SUBAGENT_LAUNCH_REAP_INTERVAL_S,
    mark_terminal: MarkSubagentTerminalAndWake | None = None,
    reconcile_pending: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """
    Periodically sweep for sub-agent dispatches wedged in ``launching``.

    Runs until cancelled; started by the runner entrypoint alongside the
    process manager. Sweep errors are logged and never end the loop.

    :param interval_s: Seconds between sweeps, e.g. ``30.0``.
    :param mark_terminal: Terminal-delivery callback forwarded to each sweep;
        the entrypoint passes the app's wake-scheduling seam so a reaped
        failure wakes the parent, not just its inbox.
    :param reconcile_pending: Refresh recovered work awaiting remote completion.
    :returns: None.
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            reap_stalled_subagent_launches(mark_terminal=mark_terminal)
            if reconcile_pending is not None:
                await reconcile_pending()
        except Exception:  # noqa: BLE001 — the sweep is a backstop; never die.
            _logger.warning("sub-agent launch reaper sweep failed", exc_info=True)


async def _wake_retry_sleep(seconds: float) -> None:
    """
    Sleep between sub-agent wake-POST retries.

    Indirection point so tests can stub the backoff without clobbering the
    process-wide ``asyncio.sleep`` (the ``no-global-asyncio-patch`` lint
    hook bans patching the module singleton).

    :param seconds: Seconds to wait before the next retry, e.g. ``0.5``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


def _wake_post_is_retryable(exc: httpx.HTTPError) -> bool:
    """
    Return whether a failed wake POST should be retried.

    Transport-level failures (connect/read errors, timeouts) are always
    retryable. A non-2xx response surfaces as :class:`httpx.HTTPStatusError`:
    5xx statuses are transient (notably the 503 ``RUNNER_UNAVAILABLE`` that
    Omnigent returns while the parent's runner tunnel is reconnecting), as
    are a few 4xx codes; every other 4xx is a permanent client-side rejection
    that retrying cannot fix.

    :param exc: HTTP error raised by the wake POST or ``raise_for_status``,
        e.g. an ``httpx.HTTPStatusError`` wrapping a 503 response.
    :returns: ``True`` if a bounded retry is worthwhile, else ``False``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        # Transport failure — the POST may never have reached Omnigent.
        return True
    status_code = exc.response.status_code
    if status_code >= 500:
        return True
    return status_code in _WAKE_POST_TRANSIENT_4XX


async def _deliver_subagent_wake_post(
    server_client: httpx.AsyncClient,
    parent_id: str,
    notice: str,
    *,
    created_by: str | None = None,
) -> bool:
    """
    POST a sub-agent wake notice with a bounded retry on transient failure.

    httpx does not raise on a non-2xx response, so a real 503
    ``RUNNER_UNAVAILABLE`` JSON response (routine while the parent's runner
    tunnel reconnects) would otherwise be treated as a successful delivery.
    This calls ``raise_for_status`` to turn any non-2xx into a failure and
    retries transient failures up to :data:`_WAKE_POST_MAX_ATTEMPTS` with
    exponential backoff, because the wake is the sole delivery signal for
    the last child of a fan-out. Permanent 4xx rejections stop immediately.

    :param server_client: Omnigent HTTP client for the runner subprocess.
    :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
    :param notice: The ``[System: ...]`` notice text to inject.
    :param created_by: Human actor that dispatched the completed child
        turn, if known.
    :returns: ``True`` if a 2xx was confirmed, ``False`` if every attempt
        failed (transport error, timeout, or non-2xx response).
    """
    attribution_created_by = created_by
    for attempt in range(1, _WAKE_POST_MAX_ATTEMPTS + 1):
        try:
            resp = await server_client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": notice}],
                    },
                    **(
                        {"created_by": attribution_created_by}
                        if attribution_created_by is not None
                        else {}
                    ),
                },
                # The server gates this injected wake at the parent's REQUEST
                # phase, which can PARK on a human ASK (e.g. session_cost_budget)
                # for up to the deciding policy's ``ask_timeout`` (default one
                # day). A 30s read budget severed that park after 30s → the
                # TimeoutError below retried → each retry re-posted the notice
                # and parked ANOTHER gate → duplicate approval cards, and the
                # gate never cleanly blocked. Hold the read budget at one day so
                # this POST waits for the real verdict (one held connection, one
                # card); fast connect so an unreachable parent runner still
                # fails out into the bounded retry below.
                timeout=_ASK_GATE_DELIVERY_TIMEOUT,
            )
            # Treat a non-2xx RESPONSE (e.g. a genuine 503 JSONResponse) as a
            # failure — httpx does not raise on status by itself.
            resp.raise_for_status()
            return True
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if (
                attribution_created_by is not None
                and isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 403
            ):
                _logger.debug(
                    "Sub-agent wake POST attribution rejected for parent=%s; "
                    "retrying without actor",
                    parent_id,
                    extra={"session_id": runner_primary_session_id()},
                )
                attribution_created_by = None
                continue
            last_attempt = attempt >= _WAKE_POST_MAX_ATTEMPTS
            retryable = isinstance(exc, asyncio.TimeoutError) or _wake_post_is_retryable(exc)
            _logger.debug(
                "Sub-agent wake POST attempt %d/%d for parent=%s failed (retryable=%s): %r",
                attempt,
                _WAKE_POST_MAX_ATTEMPTS,
                parent_id,
                retryable,
                exc,
                extra={"session_id": runner_primary_session_id()},
            )
            if last_attempt or not retryable:
                return False
            delay_s = min(
                _WAKE_POST_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)),
                _WAKE_POST_RETRY_MAX_DELAY_S,
            )
            await _wake_retry_sleep(delay_s)
    return False


def _subagent_delivery_not_confirmed_response(
    ack: _SubagentDeliveryAck,
    *,
    is_runner_known_subagent: bool,
) -> JSONResponse | None:
    """
    Build a 503 response when a known sub-agent result was not delivered.

    Top-level sessions also post terminal status but have no parent inbox, so
    an untracked status remains a no-op unless the runner knows this session
    was created as a sub-agent. For known sub-agents, Omnigent must not receive a
    2xx acknowledgement unless the terminal payload is confirmed in the
    parent's inbox — except a tracked entry whose parent is itself a
    sub-agent, which ``post_session_events`` acknowledges before calling here;
    the entry is retained and delivered only if this runner ever creates that
    parent's inbox.

    :param ack: Delivery acknowledgement returned by
        ``mark_subagent_work_terminal``.
    :param is_runner_known_subagent: Whether runner session state identifies
        the status sender as a sub-agent child.
    :returns: A 503 JSON response when delivery is not confirmed, or ``None``
        when the status can be acknowledged.
    """
    if ack.delivered:
        return None
    if ack.reason == _SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH:
        # A delayed op intentionally dropped because a newer send replaced the
        # dispatch: acknowledge so the forwarder does not retry, and never touch
        # the newer dispatch. Not a delivery failure.
        return None
    if ack.entry is None and not is_runner_known_subagent:
        return None
    reason = _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY if ack.entry is None else ack.reason
    detail_by_reason = {
        _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY: (
            "Sub-agent terminal status arrived, but the runner has no "
            "tracked work entry to deliver to the parent inbox."
        ),
        _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX: (
            "Sub-agent terminal status arrived, but the parent inbox is missing on this runner."
        ),
    }
    detail = detail_by_reason[reason]
    return JSONResponse(
        status_code=503,
        content={
            "error": "subagent_delivery_not_confirmed",
            "reason": reason,
            "detail": detail,
        },
    )


def _format_subagent_wake_notice(*, agent: str, title: str, status: str, pending: int) -> str:
    """
    Build the framework notice that wakes a parent after a child finishes.

    :param agent: Sub-agent name from the parent spec, e.g. ``"researcher"``.
    :param title: Child instance title supplied at dispatch, e.g. ``"auth"``.
    :param status: Terminal child status, e.g. ``"completed"``, ``"failed"``,
        or ``"cancelled"``.
    :param pending: Number of undrained items in the parent inbox, e.g. ``3``.
    :returns: A ``[System: ...]`` notice string, e.g. ``"[System: sub-agent
        researcher/auth finished (completed) — 1 result waiting in inbox. Call
        sys_read_inbox to collect.]"``.
    """
    noun = "result" if pending == 1 else "results"
    return (
        f"[System: sub-agent {agent}/{title} finished ({status}) — "
        f"{pending} {noun} waiting in inbox. Call sys_read_inbox to collect.]"
    )


# Max length of a child message preview mirrored to the parent stream.
# Matches the server-side ``_latest_message_preview`` truncation so the
# live runner-pushed preview and the snapshot preview look the same.
_CHILD_PREVIEW_MAX_CHARS = 150


@dataclasses.dataclass
class _ChildParentMeta:
    """Fan-out metadata for one child sub-agent session.

    Lets the runner mirror a child's status/preview deltas onto the
    PARENT's SSE stream — the child's own relay isn't running when only
    the parent is viewed, and the runner runs the child turn (affinity).

    :param parent_id: Parent session id whose stream receives the deltas.
    :param title: Child title ``"{tool}:{session_name}"`` — carried in
        status deltas so even a cold update has a display name.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    :param last_busy: Last busy value fanned out, used to coalesce
        duplicate status deltas. ``None`` until first publish.
    :param last_task_status: Last child-rail task status fanned out, e.g.
        ``"completed"``. Tracked separately so ``idle`` → ``failed`` emits
        even though both states are non-busy.
    :param last_error: Last child failure detail fanned out, used to emit a
        new parent update when only the error changes, and to clear stale
        errors on a later running/waiting edge.
    """

    parent_id: str
    title: str
    tool: str
    session_name: str
    last_busy: bool | None = None
    last_task_status: str | None = None
    last_error: tuple[str, str] | None = None


# child_session_id -> :class:`_ChildParentMeta`. Populated at spawn (see
# tool_dispatch._execute_subagent_tool), dropped when the child ends.
_child_session_parents: dict[str, _ChildParentMeta] = {}


def register_child_session(
    child_session_id: str,
    *,
    parent_session_id: str,
    title: str,
    tool: str,
    session_name: str,
) -> None:
    """
    Record a child→parent mapping for SSE status/preview fan-out.

    :param child_session_id: Child session id, e.g. ``"conv_child123"``.
    :param parent_session_id: Parent session id whose stream should
        receive the child's deltas, e.g. ``"conv_parent987"``.
    :param title: Child title, ``"{tool}:{session_name}"``.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    """
    _child_session_parents[child_session_id] = _ChildParentMeta(
        parent_id=parent_session_id,
        title=title,
        tool=tool,
        session_name=session_name,
    )


def unregister_child_session(child_session_id: str) -> None:
    """
    Drop a child→parent mapping when the child session ends.

    :param child_session_id: Child session id to forget.
    """
    _child_session_parents.pop(child_session_id, None)


def _session_status_to_task_status(status: object) -> str | None:
    """
    Map a ``session.status`` value to a child summary ``current_task_status``.

    The two vocabularies differ (session status vs. task status); this
    keeps the child rail's status text roughly in sync as ``busy`` flips.

    :param status: A ``session.status`` value, e.g. ``"running"``.
    :returns: ``"launching"`` / ``"in_progress"`` / ``"completed"`` /
        ``"failed"`` / ``"cancelled"``, or ``None`` for an unrecognized status (caller
        omits the field).
    """
    if status == "launching":
        return "launching"
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status in ("failed", "cancelled"):
        return str(status)
    return None


def _truncate_child_preview(text: str) -> str:
    """
    Truncate a child message preview to the cap with an ellipsis.

    Matches the server-side ``_latest_message_preview`` truncation so the
    live runner-pushed preview and the snapshot preview look the same.

    :param text: The child's latest assistant reply text.
    :returns: ``text`` truncated to :data:`_CHILD_PREVIEW_MAX_CHARS` with
        a trailing ellipsis when longer, else ``text`` unchanged.
    """
    if len(text) > _CHILD_PREVIEW_MAX_CHARS:
        return text[:_CHILD_PREVIEW_MAX_CHARS].rstrip() + "…"
    return text


# Module-level ref to _session_inboxes. Populated inside create_runner_app;
# used by the sub-agent work registry to deliver completions to the parent.
_session_inboxes_ref: dict[str, asyncio.Queue[_JsonObject]] = {}
