"""Glitchy attention-librarian activity stream.

This module is an event-driven side channel over Omnigent's existing
session streams. It observes already-emitted per-session events and fans
compact activity records to subscribers when the session, agent, label, or
raw route is explicitly Glitchy / attention-librarian routed.

It does not poll snapshots, start a watchdog, or wake chat sessions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any

DEFAULT_THROTTLE_SECONDS = 30 * 60
MAX_EXCERPT_CHARS = 280

GLITCHY_ROUTES = {
    "attention_librarian",
    "control_room",
    "glitchy",
    "glitchy_attention",
    "glitchy_event",
    "librarian_event",
}

FAILURE_LABELS = {
    "diagnostic_child_wedged",
    "host_error",
    "red_error",
    "runner_disconnected",
    "runner_error",
    "runner_exited",
    "runner_failed",
    "runner_timeout",
    "stale_active_turn",
    "stale_active_work",
    "timeout",
}

RUNNER_FAILURE_LABELS = {
    "runner_disconnected",
    "runner_error",
    "runner_exited",
    "runner_failed",
    "runner_timeout",
}

NEEDS_CHOICE_LABELS = {
    "approval_required",
    "elicitation",
    "needs_choice",
    "needs_input",
    "waiting_for_choice",
    "waiting_for_user",
}

ERROR_STATUSES = {"error", "errored", "failed", "failure", "red", "timeout"}
ACTIVE_STATUSES = {"active", "in_progress", "pending", "queued", "running"}
WAITING_STATUSES = {"blocked_on_user", "needs_input", "waiting", "waiting_for_user"}

INTERVENTION_QUIET = "quiet_no_intervention"
INTERVENTION_OBSERVE = "observe"
INTERVENTION_OFFER_HELP = "offer_help"
INTERVENTION_NEEDS_CHOICE = "needs_choice"
INTERVENTION_BACKLOG = "backlog_candidate"

_DONE = object()
_subscribers: set[tuple[asyncio.Queue[dict[str, Any] | object], asyncio.AbstractEventLoop]] = set()
_session_contexts: dict[str, dict[str, Any]] = {}
_state: dict[str, Any] = {
    "dedupe": {},
    "recent_user_messages": {},
    "last_event": None,
}
_lock = threading.Lock()


def utc_now_iso() -> str:
    """Return a compact UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalized_text(value: Any) -> str:
    """Normalize route, label, status, and code text for matching."""
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def as_list(value: Any) -> list[Any]:
    """Return *value* as a list without treating strings as iterables."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return [value]


def parse_bool(value: Any) -> bool | None:
    """Parse loose bool-ish values used by hook payloads."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "background"}:
            return True
        if text in {"0", "false", "no", "off", "foreground"}:
            return False
    return None


def data_from(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a nested ``data`` dict when present."""
    data = raw.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def metadata_from(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return metadata from either ``metadata`` or ``data.metadata``."""
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        return dict(metadata)
    data = data_from(raw)
    metadata = data.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def mapping_from(value: Any) -> dict[str, Any]:
    """Return a plain dict when *value* is mapping-like."""
    return dict(value) if isinstance(value, Mapping) else {}


def normalized_labels(*values: Any) -> list[str]:
    """Normalize labels from lists, dicts, and scalar fields."""
    labels: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            labels.update(normalized_labels(value.keys(), value.values()))
            continue
        for item in as_list(value):
            if isinstance(item, Mapping):
                labels.update(
                    normalized_labels(
                        item.get("label"),
                        item.get("labels"),
                        item.get("code"),
                    )
                )
                continue
            text = normalized_text(item)
            if text:
                labels.add(text)
    return sorted(labels)


def coalesce(*values: Any, default: Any = "") -> Any:
    """Return the first non-empty value."""
    for value in values:
        if value is not None and value != "":
            return value
    return default


def stable_json(value: Any) -> str:
    """Return deterministic JSON for hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    """Return a stable SHA-256 hex digest for *value*."""
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp into UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_between(later: Any, earlier: Any) -> int | None:
    """Return non-negative whole seconds between two ISO timestamps."""
    later_dt = parse_datetime(later)
    earlier_dt = parse_datetime(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return max(0, int((later_dt - earlier_dt).total_seconds()))


def truncate_excerpt(value: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Collapse whitespace and truncate content for activity summaries."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def content_text(raw: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    """Extract user/assistant text from raw or nested event payloads."""
    nested_data = mapping_from(data.get("data"))
    nested_item_data = mapping_from(nested_data.get("data"))
    item = mapping_from(data.get("item"))
    content = coalesce(
        raw.get("content"),
        raw.get("text"),
        data.get("content"),
        data.get("text"),
        nested_data.get("content"),
        nested_data.get("text"),
        nested_item_data.get("content"),
        nested_item_data.get("text"),
        item.get("content"),
        item.get("text"),
        default="",
    )
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                parts.append(str(coalesce(item.get("text"), item.get("content"), default="")))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def is_status_ping(text: str) -> bool:
    """Return whether a prompt is a human status ping."""
    normalized = " ".join(text.strip().lower().replace("?", " ").replace("!", " ").split())
    return normalized in {
        "are you there",
        "are u there",
        "hello are you there",
        "still there",
        "you there",
    }


def register_session_context(
    session_id: str,
    *,
    labels: Mapping[str, str] | None = None,
    agent_name: str | None = None,
    session_title: str | None = None,
    route: str | None = None,
) -> None:
    """Register lightweight session metadata used by the publish hook."""
    label_map = dict(labels or {})
    context = {
        "labels": label_map,
        "agent_name": agent_name or "",
        "session_title": session_title or session_id,
        "route": route or _route_from_labels(label_map),
    }
    with _lock:
        _session_contexts[session_id] = context


def unregister_session_context(session_id: str) -> None:
    """Remove a cached session context."""
    with _lock:
        _session_contexts.pop(session_id, None)


def session_context(session_id: str) -> dict[str, Any]:
    """Return a copy of a registered session context."""
    with _lock:
        context = dict(_session_contexts.get(session_id, {}))
    if isinstance(context.get("labels"), Mapping):
        context["labels"] = dict(context["labels"])
    return context


def _route_from_labels(labels: Mapping[str, str]) -> str:
    route_keys = (
        "omnigent.activity_route",
        "omnigent.attention_route",
        "attention_route",
        "route",
    )
    for key in route_keys:
        value = labels.get(key)
        if value:
            return value
    return ""


def _context_is_glitchy(context: Mapping[str, Any]) -> bool:
    labels = context.get("labels") if isinstance(context.get("labels"), Mapping) else {}
    route = normalized_text(context.get("route") or _route_from_labels(labels))
    if route in GLITCHY_ROUTES:
        return True
    label_texts = normalized_labels(labels)
    if set(label_texts) & GLITCHY_ROUTES:
        return True
    agent_name = normalized_text(context.get("agent_name"))
    return "glitchy" in agent_name or "attention_librarian" in agent_name


def is_glitchy_routed(raw: Mapping[str, Any]) -> bool:
    """Return whether a raw event is explicitly routed to Glitchy."""
    data = data_from(raw)
    metadata = metadata_from(raw)
    if parse_bool(raw.get("glitchy")) is True or parse_bool(metadata.get("glitchy")) is True:
        return True
    route_values = (
        raw.get("route"),
        raw.get("attention_route"),
        raw.get("target"),
        data.get("route"),
        metadata.get("route"),
    )
    if any(normalized_text(value) in GLITCHY_ROUTES for value in route_values):
        return True
    labels = normalized_labels(
        raw.get("labels"),
        raw.get("label"),
        raw.get("error_code"),
        data.get("labels"),
        metadata.get("labels"),
    )
    if set(labels) & GLITCHY_ROUTES:
        return True
    agent_name = normalized_text(
        coalesce(raw.get("agent_name"), data.get("agent_name"), metadata.get("agent_name"))
    )
    if "glitchy" in agent_name or "attention_librarian" in agent_name:
        return True
    session_id = str(coalesce(raw.get("session_id"), raw.get("conversation_id"), default=""))
    return bool(session_id and _context_is_glitchy(session_context(session_id)))


def record_session_event(
    conversation_id: str,
    event: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any] | None:
    """Record one live Omnigent session-stream event as activity."""
    context = session_context(conversation_id)
    kind = _kind_from_session_event(event)
    if not kind:
        return None
    raw = {
        "session_id": conversation_id,
        "kind": kind,
        "source": "omnigent.session_stream",
        "data": dict(event),
        "labels": context.get("labels", {}),
        "session_title": context.get("session_title") or conversation_id,
        "agent_name": context.get("agent_name") or "",
        "route": context.get("route") or "",
    }
    if generated_at is not None:
        raw["timestamp"] = generated_at
    return record_activity_event(raw, generated_at=generated_at)


def record_activity_event(
    raw_event: Mapping[str, Any],
    *,
    generated_at: str | None = None,
    throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
) -> dict[str, Any] | None:
    """Normalize, classify, store, and publish one activity event."""
    if not is_glitchy_routed(raw_event):
        return None
    event = normalize_event(raw_event, generated_at=generated_at)
    with _lock:
        compact = classify_activity_event(event, _state, throttle_seconds=throttle_seconds)
        _update_state_after_event(_state, event, compact)
        subs = list(_subscribers)
    for queue, loop in subs:
        if compact.get("type") == "glitchy.activity":
            loop.call_soon_threadsafe(queue.put_nowait, compact)
    return compact


def normalize_event(
    raw: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Normalize varied Omnigent event shapes into activity fields."""
    data = data_from(raw)
    metadata = metadata_from(raw)
    error = mapping_from(
        coalesce(raw.get("error"), data.get("error"), metadata.get("error"), default={})
    )
    item = mapping_from(data.get("item"))
    timestamp = str(
        coalesce(
            raw.get("timestamp"),
            raw.get("created_at"),
            data.get("timestamp"),
            generated_at,
            utc_now_iso(),
        )
    )
    kind = normalized_text(
        coalesce(
            raw.get("kind"),
            raw.get("type"),
            data.get("kind"),
            data.get("type"),
            default="unknown",
        )
    )
    session_id = str(
        coalesce(
            raw.get("session_id"),
            raw.get("conversation_id"),
            raw.get("id"),
            data.get("session_id"),
            data.get("conversation_id"),
            metadata.get("session_id"),
            default="unknown",
        )
    )
    context = session_context(session_id)
    labels = normalized_labels(
        raw.get("labels"),
        data.get("labels"),
        metadata.get("labels"),
        raw.get("label"),
        raw.get("error_code"),
        data.get("error_code"),
        metadata.get("error_code"),
        error.get("code"),
        context.get("labels", {}),
    )
    status = normalized_text(
        coalesce(raw.get("status"), data.get("status"), metadata.get("status"), default="")
    )
    severity = normalized_text(
        coalesce(
            raw.get("severity"),
            data.get("severity"),
            metadata.get("severity"),
            "error" if error else "",
            default="info",
        )
    )
    error_code = normalized_text(
        coalesce(
            raw.get("error_code"),
            data.get("error_code"),
            metadata.get("error_code"),
            error.get("code"),
            default="",
        )
    )
    content = content_text(raw, data)
    if not content:
        content = str(
            coalesce(
                raw.get("message"),
                data.get("message"),
                metadata.get("message"),
                error.get("message"),
                error.get("detail"),
                default="",
            )
        )
    event = {
        "event_id": str(coalesce(raw.get("event_id"), raw.get("id"), default="")),
        "timestamp": timestamp,
        "route": str(
            coalesce(
                raw.get("route"),
                raw.get("attention_route"),
                data.get("route"),
                metadata.get("route"),
                context.get("route"),
                default="",
            )
        ),
        "kind": kind,
        "session_id": session_id,
        "session_title": str(
            coalesce(
                raw.get("session_title"),
                raw.get("title"),
                data.get("session_title"),
                data.get("title"),
                metadata.get("session_title"),
                context.get("session_title"),
                default=session_id,
            )
        ),
        "agent_name": str(
            coalesce(
                raw.get("agent_name"),
                data.get("agent_name"),
                metadata.get("agent_name"),
                context.get("agent_name"),
                default="",
            )
        ),
        "source": str(
            coalesce(
                raw.get("source"),
                data.get("source"),
                metadata.get("source"),
                default="",
            )
        ),
        "status": status,
        "severity": severity,
        "labels": labels,
        "background": (
            parse_bool(raw.get("background"))
            or parse_bool(data.get("background"))
            or parse_bool(metadata.get("background"))
            or False
        ),
        "content_excerpt": truncate_excerpt(content),
        "current_session_id": str(
            coalesce(raw.get("current_session_id"), metadata.get("current_session_id"), default="")
        ),
        "idle_seconds": coalesce(
            raw.get("idle_seconds"),
            data.get("idle_seconds"),
            metadata.get("idle_seconds"),
            default=None,
        ),
        "stale_seconds": coalesce(
            raw.get("stale_seconds"),
            data.get("stale_seconds"),
            metadata.get("stale_seconds"),
            default=None,
        ),
        "error_code": error_code,
        "runner_id": str(
            coalesce(
                raw.get("runner_id"),
                data.get("runner_id"),
                metadata.get("runner_id"),
                default="",
            )
        ),
        "tool_name": str(
            coalesce(
                raw.get("tool_name"),
                data.get("tool_name"),
                metadata.get("tool_name"),
                item.get("tool_name"),
                item.get("name"),
                default="",
            )
        ),
        "subagent_id": str(
            coalesce(
                raw.get("subagent_id"),
                data.get("subagent_id"),
                data.get("child_session_id"),
                metadata.get("subagent_id"),
                default="",
            )
        ),
    }
    if not event["event_id"]:
        event["event_id"] = stable_hash(
            {
                "timestamp": event["timestamp"],
                "kind": event["kind"],
                "session_id": event["session_id"],
                "labels": event["labels"],
                "status": event["status"],
                "content_excerpt": event["content_excerpt"],
            }
        )[:24]
    return event


def classify_activity_event(
    event: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
) -> dict[str, Any]:
    """Classify one normalized event into a compact activity record."""
    signals: list[str] = []
    intervention = INTERVENTION_QUIET
    activity_state = "waiting"
    visible = False

    kind = str(event["kind"])
    status = str(event.get("status") or "")

    if kind == "user_message":
        signals.append("new_prompt_activity")
        intervention = INTERVENTION_OBSERVE
        activity_state = "flowing"
        if event.get("current_session_id") and event["current_session_id"] != event["session_id"]:
            signals.append("another_session_prompt")
        if is_status_ping(str(event.get("content_excerpt") or "")):
            signals.extend(["status_ping", "abandonment_risk"])
            if _recent_status_ping_count(state, event) + 1 >= 2:
                signals.append("repeated_status_ping")
                intervention = INTERVENTION_OFFER_HELP
                activity_state = "frustrated"
                visible = True
    elif kind in {"attention_event", "attention"}:
        signals.append("attention_event")
        intervention = INTERVENTION_OBSERVE
        activity_state = "waiting"
    elif kind in {"tool_activity", "tool_event", "subagent_activity", "subagent_event"}:
        signals.append(kind)
        intervention = INTERVENTION_OBSERVE
        activity_state = "flowing" if status in ACTIVE_STATUSES or not status else "waiting"
    elif is_needs_choice(event):
        signals.append("needs_choice")
        intervention = INTERVENTION_NEEDS_CHOICE
        activity_state = "needs_choice"
        visible = True
    elif is_error_event(event):
        signals.append("red_error")
        if is_runner_failure(event):
            signals.append("runner_failure")
        activity_state = "blocked"
        intervention = INTERVENTION_OFFER_HELP
        visible = True
    elif is_stale_active(event):
        signals.append("stale_active_turn")
        activity_state = "blocked"
        intervention = INTERVENTION_OFFER_HELP
        visible = True
    elif kind in {"assistant_state", "session_state"} and status in ACTIVE_STATUSES:
        signals.append("flowing")
        activity_state = "flowing"
        intervention = INTERVENTION_OBSERVE
    elif kind in {"assistant_state", "session_state"} and (
        status in WAITING_STATUSES or status in {"completed", "idle"}
    ):
        signals.append("waiting")
        activity_state = "waiting"
        intervention = INTERVENTION_OBSERVE

    background = bool(event.get("background"))
    if is_runner_failure(event):
        background = True
    if background and (
        is_error_event(event) or is_stale_active(event) or is_runner_failure(event)
    ):
        activity_state = "background_noise"
        intervention = INTERVENTION_BACKLOG
        visible = False
        signals.append("backlog_candidate")

    signature = failure_signature(event)
    throttled, key, repeat_count = should_throttle_failure(
        state, event, signature, throttle_seconds
    )
    if throttled:
        signals.append("throttled_repeat")
        intervention = INTERVENTION_QUIET
        visible = False

    compact = {
        "type": "glitchy.activity",
        "event_id": event["event_id"],
        "timestamp": event["timestamp"],
        "route": event.get("route") or "attention_librarian",
        "kind": kind,
        "session_id": event["session_id"],
        "session_title": event["session_title"],
        "agent_name": event.get("agent_name", ""),
        "source": event.get("source", ""),
        "status": status,
        "severity": event.get("severity", "info"),
        "labels": sorted({str(label) for label in event.get("labels", [])}),
        "signals": sorted(set(signals)),
        "activity_state": activity_state,
        "intervention": intervention,
        "visible": visible,
        "throttled": throttled,
        "repeat_count": repeat_count,
        "dedupe_key": key,
        "failure_signature": signature,
        "background": background,
        "content_excerpt": event.get("content_excerpt", ""),
        "error_code": event.get("error_code", ""),
        "tool_name": event.get("tool_name", ""),
        "subagent_id": event.get("subagent_id", ""),
    }
    compact["summary"] = compact_summary(compact)
    return compact


def is_needs_choice(event: Mapping[str, Any]) -> bool:
    """Return whether the event represents a human choice gate."""
    if set(event.get("labels", [])) & NEEDS_CHOICE_LABELS:
        return True
    if event["kind"] in {
        "approval_request",
        "elicitation_request",
        "needs_choice",
        "permission_request",
    }:
        return True
    return event.get("status") in {
        "awaiting_choice",
        "blocked_on_user",
        "needs_input",
        "waiting_for_user",
    }


def is_error_event(event: Mapping[str, Any]) -> bool:
    """Return whether the event carries visible failure semantics."""
    if event.get("severity") in {"critical", "error", "red"}:
        return True
    if event.get("status") in ERROR_STATUSES:
        return True
    if set(event.get("labels", [])) & {"error", "exception", "red_error"}:
        return True
    return event["kind"] in {"host_error", "runner_error", "runner_timeout", "session_timeout"}


def is_runner_failure(event: Mapping[str, Any]) -> bool:
    """Return whether the event is a runner failure class."""
    if set(event.get("labels", [])) & RUNNER_FAILURE_LABELS:
        return True
    if event["kind"] in {"host_error", "runner_error", "runner_timeout", "session_timeout"}:
        return True
    return event.get("error_code") in RUNNER_FAILURE_LABELS


def is_stale_active(event: Mapping[str, Any]) -> bool:
    """Return whether the event indicates stale active work."""
    if set(event.get("labels", [])) & {"stale_active_turn", "stale_active_work"}:
        return True
    return (
        event["kind"] in {"assistant_state", "session_state"}
        and event.get("status") in ACTIVE_STATUSES
        and bool(event.get("stale_seconds") or event.get("idle_seconds"))
    )


def failure_signature(event: Mapping[str, Any]) -> str:
    """Return a compact signature for throttled incident classes."""
    labels = set(event.get("labels", []))
    if event.get("error_code"):
        labels.add(str(event["error_code"]))
    for label in sorted(FAILURE_LABELS):
        if label in labels:
            return label
    if is_runner_failure(event):
        return "runner_failure"
    if is_stale_active(event):
        return "stale_active_turn"
    if is_error_event(event):
        return "red_error"
    return ""


def dedupe_key(event: Mapping[str, Any], signature: str) -> str:
    """Return a dedupe key for an incident signature."""
    runner = event.get("runner_id") or "runner_unknown"
    source = event.get("source") or event.get("kind") or "source_unknown"
    return "|".join([str(event["session_id"]), str(runner), str(source), signature])


def should_throttle_failure(
    state: Mapping[str, Any],
    event: Mapping[str, Any],
    signature: str,
    throttle_seconds: int,
) -> tuple[bool, str, int]:
    """Return throttling verdict, key, and repeat count for an incident."""
    if not signature:
        return False, "", 1
    key = dedupe_key(event, signature)
    dedupe_state = state.get("dedupe")
    prior = dedupe_state.get(key) if isinstance(dedupe_state, Mapping) else None
    if not isinstance(prior, Mapping):
        return False, key, 1
    repeat_count = int(prior.get("repeat_count", 1)) + 1
    elapsed = seconds_between(event["timestamp"], prior.get("last_observed_at", ""))
    if elapsed is None or elapsed <= throttle_seconds:
        return True, key, repeat_count
    return False, key, repeat_count


def _recent_status_ping_count(state: Mapping[str, Any], event: Mapping[str, Any]) -> int:
    recent = state.get("recent_user_messages")
    if not isinstance(recent, Mapping):
        return 0
    messages = recent.get(event["session_id"])
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if is_status_ping(str(message.get("content_excerpt", ""))):
            elapsed = seconds_between(event["timestamp"], message.get("timestamp", ""))
            if elapsed is None or elapsed <= 10 * 60:
                count += 1
    return count


def _update_state_after_event(
    state: dict[str, Any],
    event: Mapping[str, Any],
    compact: Mapping[str, Any],
) -> None:
    """Update in-memory dedupe and recent-prompt state."""
    if event["kind"] == "user_message":
        recent = state.setdefault("recent_user_messages", {})
        messages = recent.setdefault(event["session_id"], [])
        messages.append(
            {
                "timestamp": event["timestamp"],
                "content_excerpt": event.get("content_excerpt", ""),
                "event_id": event["event_id"],
            }
        )
        recent[event["session_id"]] = messages[-50:]
    key = compact.get("dedupe_key")
    if key:
        dedupe = state.setdefault("dedupe", {})
        prior = dedupe.get(key) if isinstance(dedupe.get(key), Mapping) else {}
        dedupe[key] = {
            "first_seen_at": prior.get("first_seen_at", event["timestamp"]),
            "last_observed_at": event["timestamp"],
            "last_event_id": event["event_id"],
            "repeat_count": compact.get("repeat_count", 1),
            "failure_signature": compact.get("failure_signature", ""),
            "session_id": event["session_id"],
            "session_title": event.get("session_title", ""),
            "intervention": compact.get("intervention", ""),
        }
    state["last_event"] = {
        "event_id": compact.get("event_id"),
        "timestamp": compact.get("timestamp"),
        "session_id": compact.get("session_id"),
        "intervention": compact.get("intervention"),
        "signals": compact.get("signals"),
    }


def compact_summary(compact: Mapping[str, Any]) -> str:
    """Return a human-readable one-line summary."""
    signals = (
        ", ".join(compact.get("signals", [])) if compact.get("signals") else compact["kind"]
    )
    repeat = (
        f"; repeat_count={compact['repeat_count']}"
        if compact.get("repeat_count", 1) > 1
        else ""
    )
    return (
        f"{compact['intervention']}: {compact['session_title']} "
        f"({compact['session_id']}): {signals}{repeat}"
    )


async def subscribe(
    *,
    visible_only: bool = False,
    heartbeat_interval_s: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Subscribe to live classified Glitchy activity events."""
    queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    entry = (queue, loop)
    with _lock:
        _subscribers.add(entry)
    try:
        while True:
            if heartbeat_interval_s is None:
                item = await queue.get()
            else:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval_s)
                except asyncio.TimeoutError:
                    yield {"type": "glitchy.activity.heartbeat"}
                    continue
            if item is _DONE:
                return
            assert isinstance(item, dict)
            if visible_only and not item.get("visible"):
                continue
            yield item
    finally:
        with _lock:
            _subscribers.discard(entry)


def close_all() -> None:
    """Close every active activity subscriber."""
    with _lock:
        subs = list(_subscribers)
    for queue, loop in subs:
        loop.call_soon_threadsafe(queue.put_nowait, _DONE)


def reset_for_tests() -> None:
    """Clear process-global state for focused tests."""
    with _lock:
        _subscribers.clear()
        _session_contexts.clear()
        _state.clear()
        _state.update({"dedupe": {}, "recent_user_messages": {}, "last_event": None})


def _kind_from_session_event(event: Mapping[str, Any]) -> str:
    """Map Omnigent SSE event types to activity kinds."""
    event_type = str(event.get("type") or "")
    if event_type == "session.input.consumed":
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        item_type = data.get("type")
        item_data = data.get("data") if isinstance(data.get("data"), Mapping) else {}
        if item_type == "message":
            role = item_data.get("role")
            return "user_message" if role == "user" else "assistant_message"
        if item_type in {"function_call", "function_call_output"}:
            return "tool_event"
        return (
            "attention_event"
            if item_type == "attention_event"
            else str(item_type or event_type)
        )
    if event_type == "response.output_item.done":
        item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
        item_type = item.get("type")
        if item_type in {"function_call", "function_call_output"}:
            return "tool_event"
        if item_type == "message":
            role = item.get("role")
            return "user_message" if role == "user" else "assistant_message"
        return str(item_type or event_type)
    if event_type == "session.status":
        return "session_state"
    if event_type in {
        "response.created",
        "response.in_progress",
        "response.completed",
        "response.failed",
    }:
        return "assistant_state"
    if event_type == "response.elicitation_request":
        return "elicitation_request"
    if event_type == "response.elicitation_resolved":
        return "elicitation_resolved"
    if event_type == "session.child_session.updated":
        return "subagent_event"
    if event_type.startswith("session.resource.") or event_type == "session.terminal.activity":
        return "tool_event"
    if event_type in {"response.error", "turn.failed"}:
        return "runner_error"
    if event_type in {"session.interrupted", "response.cancelled"}:
        return "session_state"
    return ""
