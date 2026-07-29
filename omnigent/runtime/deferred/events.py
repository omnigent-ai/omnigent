"""
Session stream event publishers for deferred action state transitions.
"""

from __future__ import annotations

import logging
from typing import Any

from omnigent.runtime import session_stream
from omnigent.runtime.deferred.models import DeferredAction

_logger = logging.getLogger(__name__)


def _publish_deferred_event(event_type: str, session_id: str, action: DeferredAction) -> None:
    """Helper to publish deferred action state change event to session stream."""
    event_payload: dict[str, Any] = {
        "type": event_type,
        "action": action.to_dict(),
    }
    try:
        session_stream.publish(session_id, event_payload)
    except Exception as exc:
        _logger.warning("Failed to publish deferred action event %s: %s", event_type, exc)


def emit_deferred_created(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.created SSE stream event."""
    _publish_deferred_event("response.deferred.created", session_id, action)


def emit_deferred_approved(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.approved SSE stream event."""
    _publish_deferred_event("response.deferred.approved", session_id, action)


def emit_deferred_rejected(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.rejected SSE stream event."""
    _publish_deferred_event("response.deferred.rejected", session_id, action)


def emit_deferred_executed(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.executed SSE stream event."""
    _publish_deferred_event("response.deferred.executed", session_id, action)


def emit_deferred_expired(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.expired SSE stream event."""
    _publish_deferred_event("response.deferred.expired", session_id, action)


def emit_deferred_failed(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.failed SSE stream event."""
    _publish_deferred_event("response.deferred.failed", session_id, action)


def emit_deferred_hash_drift(session_id: str, action: DeferredAction) -> None:
    """Emit response.deferred.hash_drift SSE stream event."""
    _publish_deferred_event("response.deferred.hash_drift", session_id, action)
