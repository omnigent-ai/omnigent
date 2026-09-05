"""API route for the per-user LLM cost report (``omni usage``)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.entities import Conversation
from omnigent.runtime.policies.builder import load_session_tree, load_session_usage
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._sessions.helpers import (
    _resolve_harness_impl,
    _resolve_llm_model,
)
from omnigent.server.schemas import (
    CostAlert,
    CostAlertCreate,
    CostAlertUpdate,
    DailyCost,
    ProjectCost,
    SessionUsage,
    UsageForecast,
    UsageReport,
)
from omnigent.stores import ConversationStore
from omnigent.stores.project_store import ProjectStore

_EPOCH_DAY = "0000-00-00"


def _utc_today() -> str:
    """Return the current UTC calendar day as ``"YYYY-MM-DD"``."""
    from omnigent.db.utils import now_epoch

    return datetime.fromtimestamp(now_epoch(), tz=timezone.utc).date().isoformat()


def _day_offset(day_utc: str, *, days: int) -> str:
    """Return the UTC day *days* before *day_utc*, as ``"YYYY-MM-DD"``."""
    base = datetime.strptime(day_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (base - timedelta(days=days)).date().isoformat()


def _session_models(usage: dict[str, Any]) -> dict[str, float]:
    """
    Project a session's ``by_model`` map into a ``{model_id: cost_usd}`` dict.

    Mirrors the web session sidebar's per-model list: each model's recorded
    cost, keyed by the raw harness model id, shown faithfully. Models with no
    recorded cost are omitted. NOT guaranteed to sum to the session's
    ``total_cost_usd`` — see :class:`SessionUsage`.
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


# ── Forecasting ──────────────────────────────────────────────────


def _forecast_cost(
    daily_costs: list[tuple[str, float]],
    days_ahead: int = 30,
) -> UsageForecast | None:
    """
    Project future spend using a weighted moving average of recent daily costs.

    Uses the last 14 days of actual data (weighted more heavily toward recent
    days) to predict the next ``days_ahead`` days. Returns None when there is
    insufficient data (< 3 days).
    """
    if len(daily_costs) < 3:
        return None

    recent = daily_costs[-14:]
    weights = [i + 1 for i in range(len(recent))]
    total_weight = sum(weights)
    weighted_avg = sum(w * c for w, (_, c) in zip(weights, recent, strict=True)) / total_weight

    today = _utc_today()
    projected_daily: list[DailyCost] = []
    projected_total = 0.0
    base = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    for i in range(1, days_ahead + 1):
        day = (base + timedelta(days=i)).date().isoformat()
        projected_daily.append(DailyCost(day=day, cost_usd=round(weighted_avg, 4)))
        projected_total += weighted_avg

    if len(daily_costs) >= 7:
        first_half = daily_costs[: len(daily_costs) // 2]
        second_half = daily_costs[len(daily_costs) // 2 :]
        avg_first = sum(c for _, c in first_half) / len(first_half)
        avg_second = sum(c for _, c in second_half) / len(second_half)
        if avg_second > avg_first * 1.1:
            trend = "increasing"
        elif avg_second < avg_first * 0.9:
            trend = "decreasing"
        else:
            trend = "stable"
    else:
        trend = "stable"

    return UsageForecast(
        projected_cost_30d=round(projected_total, 2),
        projected_daily=projected_daily,
        trend=trend,
    )


# ── Report builder ───────────────────────────────────────────────


def _build_usage_report(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    include_page_details: bool = False,
    project_store: ProjectStore | None = None,
    since: str | None = None,
    until: str | None = None,
) -> UsageReport:
    """
    Build the usage report: a daily-rollup cost summary plus session detail.

    The summary (today / last 7 days / last 30 days / all-time) is always
    unfiltered. The per-session detail and daily timeline respect the optional
    ``since``/``until`` date range when provided.
    """
    rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL

    today = _utc_today()
    cost_today = conversation_store.sum_daily_cost(rollup_user, today)
    cost_7d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=6))
    cost_30d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=29))
    total = conversation_store.sum_daily_cost(rollup_user, _EPOCH_DAY)

    # Convert since/until to epoch seconds for session filtering
    since_epoch: float = 0
    until_epoch: float = float("inf")
    if since:
        since_epoch = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    if until:
        until_epoch = (
            datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            + timedelta(days=1)
            - timedelta(seconds=1)
        ).timestamp()

    # Build project name lookup
    project_names: dict[str, str] = {}
    if include_page_details and project_store is not None:
        try:
            projects = project_store.list(user_id=user_id)
            for p in projects:
                project_names[p.id] = p.name
        except (AttributeError, TypeError, ValueError):
            pass

    sessions: list[SessionUsage] = []
    project_costs: dict[str | None, dict[str, Any]] = {}
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            limit=200,
            after=after,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order="desc",
            sort_by="updated_at",
        )
        for conv in page.data:
            if conv.agent_id is None:
                continue
            # Filter by date range when specified
            if since and conv.updated_at < since_epoch:
                continue
            if until and conv.updated_at > until_epoch:
                continue

            usage = load_session_usage(conv.id, conversation_store)
            primary_harness = _resolve_session_harness(conv) if include_page_details else None
            other_harnesses = None
            if include_page_details:
                tree = load_session_tree(conv.id, conversation_store)
                other_harnesses = _collect_other_harnesses(primary_harness, tree, conv.id)

            session_cost = _session_cost(usage)
            proj_id = conv.project_id
            proj_name = project_names.get(proj_id) if proj_id else None

            sessions.append(
                SessionUsage(
                    id=conv.id,
                    created_at=conv.created_at,
                    updated_at=conv.updated_at,
                    title=conv.title,
                    cost_usd=session_cost,
                    models=_session_models(usage),
                    harness=primary_harness,
                    other_harnesses=other_harnesses,
                    llm_model=(
                        conv.model_override or _resolve_llm_model(conv)
                        if include_page_details
                        else None
                    ),
                    agent_name=conv.sub_agent_name if include_page_details else None,
                    project_id=proj_id if include_page_details else None,
                    project_name=proj_name if include_page_details else None,
                )
            )

            if include_page_details:
                key = proj_id
                if key not in project_costs:
                    project_costs[key] = {
                        "project_id": proj_id,
                        "project_name": proj_name,
                        "cost_usd": 0.0,
                        "session_count": 0,
                    }
                project_costs[key]["cost_usd"] += session_cost
                project_costs[key]["session_count"] += 1

        if not page.has_more:
            break
        after = page.last_id

    daily_costs_raw = (
        conversation_store.list_daily_costs(
            rollup_user,
            since or _EPOCH_DAY,
            until_day_utc=until,
        )
        if include_page_details
        else []
    )

    # Build forecast from all-time daily data
    forecast = None
    if include_page_details:
        all_daily = conversation_store.list_daily_costs(rollup_user, _EPOCH_DAY)
        forecast = _forecast_cost(all_daily)

    # Check alerts
    alerts_data = conversation_store.list_cost_alerts(rollup_user)
    alerts = [
        CostAlert(
            id=a["id"],
            threshold_usd=a["threshold_usd"],
            period=a["period"],
            enabled=a["enabled"],
            created_at=a["created_at"],
        )
        for a in alerts_data
    ]
    alert_triggered = False
    for a in alerts:
        if not a.enabled:
            continue
        if a.period == "daily" and cost_today >= a.threshold_usd:
            alert_triggered = True
            break
        if a.period == "monthly" and cost_30d >= a.threshold_usd:
            alert_triggered = True
            break

    project_list = sorted(
        [ProjectCost(**pc) for pc in project_costs.values()],
        key=lambda p: p.cost_usd,
        reverse=True,
    )

    return UsageReport(
        cost_today=cost_today,
        cost_last_7d=cost_7d,
        cost_last_30d=cost_30d,
        total_cost_usd=total,
        daily_costs=[DailyCost(day=d, cost_usd=c) for d, c in daily_costs_raw],
        sessions=sessions,
        projects=project_list,
        alerts=alerts,
        alert_triggered=alert_triggered,
        forecast=forecast,
    )


def create_usage_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    feature_flags: FeatureFlags | None = None,
    project_store: ProjectStore | None = None,
) -> APIRouter:
    """
    Create the per-user usage-report router.

    The report is user-scoped, not session-scoped, so it lives in its own
    router rather than under the sessions router.
    """
    flags = feature_flags or resolve_feature_flags()
    router = APIRouter()

    @router.get("/usage", response_model=UsageReport)
    async def get_usage(
        request: Request,
        since: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        until: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ) -> UsageReport:
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(
            _build_usage_report,
            conversation_store,
            user_id,
            include_page_details=flags.enabled(Feature.USAGE_PAGE),
            project_store=project_store,
            since=since,
            until=until,
        )

    # ── Cost alert CRUD ──────────────────────────────────────────

    @router.post("/usage/alerts", response_model=CostAlert)
    async def create_alert(request: Request, body: CostAlertCreate) -> CostAlert:
        user_id = require_user(request, auth_provider)
        rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL
        alert_id = uuid.uuid4().hex
        result = await asyncio.to_thread(
            conversation_store.create_cost_alert,
            alert_id,
            rollup_user,
            body.threshold_usd,
            body.period,
        )
        return CostAlert(
            id=result["id"],
            threshold_usd=result["threshold_usd"],
            period=result["period"],
            enabled=result["enabled"],
            created_at=result["created_at"],
        )

    @router.get("/usage/alerts", response_model=list[CostAlert])
    async def list_alerts(request: Request) -> list[CostAlert]:
        user_id = require_user(request, auth_provider)
        rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL
        results = await asyncio.to_thread(
            conversation_store.list_cost_alerts,
            rollup_user,
        )
        return [
            CostAlert(
                id=a["id"],
                threshold_usd=a["threshold_usd"],
                period=a["period"],
                enabled=a["enabled"],
                created_at=a["created_at"],
            )
            for a in results
        ]

    @router.delete("/usage/alerts/{alert_id}")
    async def delete_alert(request: Request, alert_id: str) -> dict[str, bool]:
        user_id = require_user(request, auth_provider)
        rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL
        deleted = await asyncio.to_thread(
            conversation_store.delete_cost_alert,
            alert_id,
            rollup_user,
        )
        return {"deleted": deleted}

    @router.patch("/usage/alerts/{alert_id}", response_model=CostAlert)
    async def update_alert(
        request: Request,
        alert_id: str,
        body: CostAlertUpdate,
    ) -> CostAlert:
        user_id = require_user(request, auth_provider)
        rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL
        result = await asyncio.to_thread(
            conversation_store.update_cost_alert,
            alert_id,
            rollup_user,
            enabled=body.enabled,
            threshold_usd=body.threshold_usd,
        )
        if result is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Alert not found")
        return CostAlert(
            id=result["id"],
            threshold_usd=result["threshold_usd"],
            period=result["period"],
            enabled=result["enabled"],
            created_at=result["created_at"],
        )

    return router
