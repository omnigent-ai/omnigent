"""API route for the per-user LLM cost report (``omni usage``)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.entities import Conversation
from omnigent.errors import restart_on_stale_cursor
from omnigent.runtime.policies.builder import load_session_usage, load_session_usage_and_tree
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._errors import STALE_CURSOR_RESPONSE
from omnigent.server.routes._sessions.helpers import (
    _resolve_harness_impl,
    _resolve_llm_model,
)
from omnigent.server.schemas import DailyCost, SessionUsage, UsageReport
from omnigent.stores import ConversationStore

# The daily rollup floor for an "all-time" sum: earlier than any real row, so
# ``sum_daily_cost`` with this lower bound totals every recorded day.
_EPOCH_DAY = "0000-00-00"

# Upper bound for one requested page of session detail on ``GET /v1/usage``.
MAX_SESSION_PAGE_SIZE = 200


def _utc_today() -> str:
    """Return the current UTC calendar day as ``"YYYY-MM-DD"``."""
    from omnigent.db.utils import now_epoch

    return datetime.fromtimestamp(now_epoch(), tz=timezone.utc).date().isoformat()


def _day_offset(day_utc: str, *, days: int) -> str:
    """Return the UTC day *days* before *day_utc*, as ``"YYYY-MM-DD"``."""
    base = datetime.strptime(day_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (base - timedelta(days=days)).date().isoformat()


def _day_bound_epoch(value: str, *, param: str, end_of_day: bool) -> int:
    """
    Convert a ``YYYY-MM-DD`` query value to a UTC epoch bound.

    :param value: The raw query value, e.g. ``"2026-10-01"``.
    :param param: The query parameter name, for the error message.
    :param end_of_day: Return the day's last second instead of its first.
    :returns: Unix epoch seconds.
    :raises HTTPException: 422 when *value* is not a calendar date.
    """
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {param!r} date {value!r}; expected YYYY-MM-DD.",
        ) from exc
    if end_of_day:
        day += timedelta(days=1, seconds=-1)
    return int(day.timestamp())


def _session_models(usage: dict[str, Any]) -> dict[str, float]:
    """
    Project a session's ``by_model`` map into a ``{model_id: cost_usd}`` dict.

    Mirrors the web session sidebar's per-model list: each model's recorded
    cost, keyed by the raw harness model id, shown faithfully. Models with no
    recorded cost are omitted. NOT guaranteed to sum to the session's
    ``total_cost_usd`` — see :class:`SessionUsage`.

    :param usage: A subtree-summed ``session_usage`` dict.
    :returns: Per-model cost map (empty when no per-model cost was recorded).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict):
        return {}
    models: dict[str, float] = {}
    for name, bucket in by_model.items():
        if not isinstance(bucket, dict) or "total_cost_usd" not in bucket:
            continue
        try:
            models[str(name)] = float(bucket["total_cost_usd"])
        except (TypeError, ValueError):
            continue
    return models


def _collect_other_harnesses(
    primary: str | None,
    tree: list[Conversation],
    root_id: str,
) -> list[str] | None:
    """Distinct harnesses used by sub-agents, excluding the primary."""
    seen: set[str] = set()
    for conv in tree:
        if conv.id == root_id:
            continue
        h = _resolve_session_harness(conv)
        if h and h != primary:
            seen.add(h)
    return sorted(seen) if seen else None


def _resolve_session_harness(conv: Conversation) -> str | None:
    """
    Best-effort harness resolution for the usage report.

    Checks, in order: (1) per-session harness override, (2) the
    ``omnigent.wrapper`` label stamped at creation by native wrappers,
    (3) agent-spec resolution via the runtime (works when the server has
    a live agent cache). Returns a display-friendly name or ``None``.
    """
    if conv.harness_override:
        return conv.harness_override
    wrapper = conv.labels.get(WRAPPER_LABEL_KEY)
    if wrapper:
        # Strip the "-ui" suffix for display: "claude-code-native-ui" → "claude-code-native"
        return wrapper.removesuffix("-ui") if wrapper.endswith("-ui") else wrapper
    return _resolve_harness_impl(conv)


def _session_cost(usage: dict[str, Any]) -> float:
    """
    Read a session's authoritative cumulative cost, or ``0.0`` when unpriced.

    ``total_cost_usd`` is present only on priced sessions (the "priced ⟺ key
    present" contract); an absent or malformed value reads as ``0.0``.
    """
    if "total_cost_usd" not in usage:
        return 0.0
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        return 0.0


def _list_conversation_page(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    limit: int,
    after: str | None,
    updated_at_min: int | None,
    updated_at_max: int | None,
):
    """One cursor page of the caller's top-level, agent-bearing sessions."""
    return conversation_store.list_conversations(
        limit=limit,
        after=after,
        accessible_by=user_id,
        has_agent_id=True,
        kind="default",
        order="desc",
        sort_by="updated_at",
        updated_at_min=updated_at_min,
        updated_at_max=updated_at_max,
    )


def _fetch_session_rows(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    limit: int | None,
    after: str | None,
    updated_at_min: int | None,
    updated_at_max: int | None,
) -> tuple[list[Conversation], bool, str | None]:
    """
    Return ``(rows, has_more, last_id)`` for the requested session window.

    A positive *limit* returns one cursor page, and a stale *after* surfaces as
    :class:`~omnigent.errors.StaleCursorError` (the API's 400 ``stale_cursor``).
    ``limit=None`` preserves the complete legacy report: every matching session
    is returned and ``has_more`` is always ``False``.
    """
    if limit is not None:
        page = _list_conversation_page(
            conversation_store,
            user_id,
            limit=limit,
            after=after,
            updated_at_min=updated_at_min,
            updated_at_max=updated_at_max,
        )
        return list(page.data), page.has_more, page.last_id
    return _fetch_all_session_rows(
        conversation_store,
        user_id,
        after=after,
        updated_at_min=updated_at_min,
        updated_at_max=updated_at_max,
    )


@restart_on_stale_cursor
def _fetch_all_session_rows(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    after: str | None,
    updated_at_min: int | None,
    updated_at_max: int | None,
) -> tuple[list[Conversation], bool, str | None]:
    """
    Walk every matching session for the unbounded legacy response.

    A session deleted mid-walk invalidates the internal page cursor; the whole
    walk restarts (via :func:`restart_on_stale_cursor`) so the result never
    reflects a partial pass. ``has_more`` is always ``False``.
    """
    rows: list[Conversation] = []
    cursor = after
    last_id: str | None = None
    while True:
        page = _list_conversation_page(
            conversation_store,
            user_id,
            limit=MAX_SESSION_PAGE_SIZE,
            after=cursor,
            updated_at_min=updated_at_min,
            updated_at_max=updated_at_max,
        )
        rows.extend(page.data)
        last_id = page.last_id or last_id
        if not page.has_more:
            break
        cursor = page.last_id
    return rows, False, last_id


@restart_on_stale_cursor
def _compute_breakdowns(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    updated_at_min: int | None,
    updated_at_max: int | None,
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Sum each session's subtree cost by primary harness and by model.

    Aggregates every in-range session subtree (not just the loaded page),
    bucketing sessions with no resolved harness under ``"Unknown"``. A deletion
    mid-walk restarts the whole pass via :func:`restart_on_stale_cursor` so the
    sums never reflect a partial walk.

    :param conversation_store: Store to enumerate sessions from.
    :param user_id: The caller / ACL scope (``None`` in single-user mode).
    :param updated_at_min: Inclusive lower bound on ``updated_at`` (epoch s).
    :param updated_at_max: Inclusive upper bound on ``updated_at`` (epoch s).
    :returns: ``(harness_breakdown, model_breakdown)``.
    """
    harness_breakdown: dict[str, float] = {}
    model_breakdown: dict[str, float] = {}
    after: str | None = None
    while True:
        page = _list_conversation_page(
            conversation_store,
            user_id,
            limit=MAX_SESSION_PAGE_SIZE,
            after=after,
            updated_at_min=updated_at_min,
            updated_at_max=updated_at_max,
        )
        for conv in page.data:
            if conv.agent_id is None:
                continue
            usage = load_session_usage(
                conv.id, conversation_store, root_conversation_id=conv.root_conversation_id
            )
            harness = _resolve_session_harness(conv) or "Unknown"
            harness_breakdown[harness] = harness_breakdown.get(harness, 0.0) + _session_cost(usage)
            for model, cost in _session_models(usage).items():
                model_breakdown[model] = model_breakdown.get(model, 0.0) + cost
        if not page.has_more:
            break
        after = page.last_id
    return harness_breakdown, model_breakdown


def _build_usage_report(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    include_page_details: bool = False,
    limit: int | None = None,
    after: str | None = None,
    updated_at_min: int | None = None,
    updated_at_max: int | None = None,
) -> UsageReport:
    """
    Build the usage report: a daily-rollup cost summary plus a page of sessions.

    The summary (today / last 7 days / last 30 days / all-time) is summed
    from the per-user daily-cost rollup (``user_daily_cost``), which
    attributes spend to the UTC day it occurred on — so the windows reflect
    when spend actually happened, not merely a session's last-activity time.

    The per-session detail is a separate view over each top-level session's
    cumulative ``session_usage`` (rolled up across its sub-agent subtree via
    :func:`load_session_usage_and_tree`), newest activity first, carrying the
    authoritative session cost and the per-model breakdown. It is one cursor
    page: *limit* rows after the *after* session, optionally restricted to
    sessions last active within ``[updated_at_min, updated_at_max]``. ``limit``
    of ``None`` returns every matching session (the pre-pagination contract).
    An *after* session that no longer exists raises
    :class:`~omnigent.errors.StaleCursorError`, which the API maps to a 400
    ``stale_cursor`` response so the client restarts from the first page.

    The harness and model breakdowns for the selected range ride only the first
    page of a report built for the release-gated web Usage page; they are
    computed by a single server-side pass (:func:`_compute_breakdowns`) over the
    in-range sessions, which the paginated client cannot sum from its one page.

    :param conversation_store: Store to read the rollup and sessions from.
    :param user_id: The caller / ACL scope. ``None`` in single-user mode maps
        to the reserved local owner the daily rollup and grants are keyed by.
    :param include_page_details: Populate the timeline, breakdowns and
        display metadata used only by the release-gated web Usage page.
    :param limit: Maximum sessions in this page; ``None`` for unbounded.
    :param after: Session id this page starts after; ``None`` for the first.
    :param updated_at_min: Inclusive lower bound on ``updated_at`` (epoch s).
    :param updated_at_max: Inclusive upper bound on ``updated_at`` (epoch s).
    :returns: The populated :class:`UsageReport`.
    """
    # The daily rollup and session-permission grants key spend by the resolved
    # owner, which is the reserved local sentinel in single-user mode (where
    # require_user yields None). Map None -> "local" so the summary reads the
    # same rows the write path recorded.
    rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL

    today = _utc_today()
    cost_today = conversation_store.sum_daily_cost(rollup_user, today)
    cost_7d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=6))
    cost_30d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=29))
    total = conversation_store.sum_daily_cost(rollup_user, _EPOCH_DAY)

    rows, has_more, last_id = _fetch_session_rows(
        conversation_store,
        user_id,
        limit=limit,
        after=after,
        updated_at_min=updated_at_min,
        updated_at_max=updated_at_max,
    )
    sessions: list[SessionUsage] = []
    for conv in rows:
        if conv.agent_id is None:
            continue
        # Pass the already-listed root so one tree load serves both the session
        # cost sum and the other-harness roll-up below.
        usage, tree = load_session_usage_and_tree(
            conv.id,
            conversation_store,
            root_conversation_id=conv.root_conversation_id,
        )
        primary_harness = _resolve_session_harness(conv) if include_page_details else None
        other_harnesses = (
            _collect_other_harnesses(primary_harness, tree, conv.id)
            if include_page_details
            else None
        )
        sessions.append(
            SessionUsage(
                id=conv.id,
                created_at=conv.created_at,
                updated_at=conv.updated_at,
                title=conv.title,
                cost_usd=_session_cost(usage),
                models=_session_models(usage),
                harness=primary_harness,
                other_harnesses=other_harnesses,
                llm_model=(
                    conv.model_override or _resolve_llm_model(conv)
                    if include_page_details
                    else None
                ),
                agent_name=conv.sub_agent_name if include_page_details else None,
            )
        )

    # The web page reads the breakdowns from its first page only, so cursor
    # pages (and legacy unbounded callers) skip the server-side pass.
    harness_breakdown: dict[str, float] = {}
    model_breakdown: dict[str, float] = {}
    if include_page_details and after is None and limit is not None:
        harness_breakdown, model_breakdown = _compute_breakdowns(
            conversation_store,
            user_id,
            updated_at_min=updated_at_min,
            updated_at_max=updated_at_max,
        )

    daily_costs_raw = (
        conversation_store.list_daily_costs(rollup_user, _EPOCH_DAY)
        if include_page_details
        else []
    )

    return UsageReport(
        cost_today=cost_today,
        cost_last_7d=cost_7d,
        cost_last_30d=cost_30d,
        total_cost_usd=total,
        daily_costs=[DailyCost(day=d, cost_usd=c) for d, c in daily_costs_raw],
        sessions=sessions,
        sessions_has_more=has_more,
        sessions_last_id=last_id,
        harness_breakdown=harness_breakdown,
        model_breakdown=model_breakdown,
    )


def create_usage_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    feature_flags: FeatureFlags | None = None,
) -> APIRouter:
    """
    Create the per-user usage-report router.

    The report is user-scoped, not session-scoped, so it lives in its own
    router rather than under the sessions router.

    :param conversation_store: Store for the daily rollup and session reads.
    :param auth_provider: Auth provider for user identity. ``None`` disables
        auth (single-user / local mode).
    :param feature_flags: Immutable deployment release-feature snapshot.
        When omitted, resolves ``OMNIGENT_FEATURES`` at router construction.
    :returns: The configured router (mounted under ``/v1``).
    """
    flags = feature_flags or resolve_feature_flags()
    router = APIRouter()

    @router.get(
        "/usage",
        response_model=UsageReport,
        responses={200: {"model": UsageReport}, **STALE_CURSOR_RESPONSE},
    )
    async def get_usage(
        request: Request,
        limit: int | None = None,
        after: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> UsageReport:
        """
        Aggregate the calling user's LLM spend across their sessions.

        require_user, not get_user_id: the aggregation scopes to the caller,
        so a request slipping through as ``None`` in multi-user mode would
        read another scope. Fail closed with 401 instead (``user_id`` is
        ``None`` only when auth is disabled — the single-user / local case).

        :param limit: Sessions per page, clamped to
            ``[1, MAX_SESSION_PAGE_SIZE]``. Omit for the unbounded legacy
            response; the web page requests one bounded page at a time.
        :param after: Cursor: the previous page's ``sessions_last_id``.
        :param since: Only sessions last active on or after this UTC day
            (``YYYY-MM-DD``).
        :param until: Only sessions last active on or before this UTC day
            (``YYYY-MM-DD``).
        """
        user_id = require_user(request, auth_provider)
        updated_at_min = (
            _day_bound_epoch(since, param="since", end_of_day=False) if since else None
        )
        updated_at_max = _day_bound_epoch(until, param="until", end_of_day=True) if until else None
        page_limit = max(1, min(limit, MAX_SESSION_PAGE_SIZE)) if limit is not None else None
        return await asyncio.to_thread(
            _build_usage_report,
            conversation_store,
            user_id,
            include_page_details=flags.enabled(Feature.USAGE_PAGE),
            limit=page_limit,
            after=after,
            updated_at_min=updated_at_min,
            updated_at_max=updated_at_max,
        )

    return router
