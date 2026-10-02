"""Portable notification text and platform envelopes for white-label clients."""

from __future__ import annotations

import time
from typing import Any, Literal

from omnigent.util.session_lifecycle import title_without_closed_marker

PushKind = Literal["completed", "failed", "needs_input"]
Platform = Literal["android", "ios"]
_BODIES = {
    "completed": "Agent finished and is ready for your input.",
    "failed": "Agent stopped with an error.",
    "needs_input": "Agent is asking for your input.",
}
_REASONS = {
    "runner_unavailable": "Runner unavailable",
}


def failure_reason(code: str | None) -> str | None:
    return _REASONS.get(code or "")


def _shown_reason(kind: PushKind, reason: str | None) -> str | None:
    return reason if kind == "failed" and reason in _REASONS.values() else None


def format_notification(
    kind: PushKind, title: str, reason: str | None = None, preview: str | None = None
) -> tuple[str, str]:
    display_title = (title_without_closed_marker(title) or "").strip() or "New session"
    body = _BODIES[kind]
    if shown_reason := _shown_reason(kind, reason):
        body = f"Agent stopped with an error: {shown_reason}"
    if preview:
        body += "\n" + preview[:120]
    return display_title, body


def message_payload(
    *,
    platform: Platform,
    token: str,
    session_id: str,
    kind: PushKind,
    title: str,
    reason: str | None = None,
    preview: str | None = None,
) -> dict[str, Any]:
    display_title, body = format_notification(kind, title, reason, preview)
    data = {"kind": kind, "session_id": session_id, "title": display_title}
    if shown_reason := _shown_reason(kind, reason):
        data["reason"] = shown_reason
    if preview:
        data["preview"] = preview[:120]
    message: dict[str, Any] = {"token": token, "data": data}
    if platform == "android":
        message["android"] = {"priority": "high", "ttl": "3600s", "collapse_key": session_id}
    else:
        message["apns"] = {
            "headers": {
                "apns-expiration": str(int(time.time()) + 3600),
                "apns-collapse-id": session_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
            },
            "payload": {"aps": {"alert": {"title": display_title, "body": body}}},
        }
    return {"message": message}
