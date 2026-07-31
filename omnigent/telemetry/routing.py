"""Recorders for smart-routing usage telemetry.

The routing paths are spread across the turn dispatcher, the subagent
policy and the session PATCH handler.  Keeping the event construction
here means the pipeline's conventions — installation id, anonymised user
id, and the model-label allowlist — are applied in one place rather than
restated at every call site.

Both recorders are fire-and-forget and never raise: :func:`omnigent.telemetry.emit`
already no-ops when telemetry is disabled.
"""

from __future__ import annotations

import hashlib
import logging

from omnigent.telemetry.client import emit
from omnigent.telemetry.events import RoutingDecisionEvent, RoutingSettingChangedEvent
from omnigent.telemetry.model_labels import model_family, model_tier

_logger = logging.getLogger(__name__)

#: The only setting :func:`record_routing_setting_changed` reports today.
SETTING_SUBAGENT_ROUTING = "subagent_routing"


def _anon_user_id(installation_id: str | None, user_id: str | None) -> str | None:
    """Hash *user_id* into the pipeline's anonymised user identifier.

    :param installation_id: Server-side installation ID, used as salt.
    :param user_id: Requesting user's identifier. ``None`` yields ``None``.
    :returns: First 16 hex chars of ``sha256("<installation_id>:<user_id>")``.
    """
    if user_id is None:
        return None
    salt = f"{installation_id}:{user_id}" if installation_id else user_id
    return hashlib.sha256(salt.encode()).hexdigest()[:16]


def record_routing_decision(
    session_id: str,
    *,
    scope: str,
    harness: str | None,
    action: str,
    applied: bool,
    model: str | None,
    raw_model: str | None,
    overrode_agent_model: bool,
    decision_id: str,
) -> None:
    """Record one routing decision.

    :param session_id: Session the decision was made for.
    :param scope: ``"turn"``, ``"child_session"``, ``"session"`` or
        ``"native_subagent"``.
    :param harness: Harness the decision applies to.
    :param action: ``"allow"``, ``"rewrite"``, ``"redirect"`` or ``"deny"``.
    :param applied: ``True`` when the pick changed the spawn/turn.
    :param model: Picked servable model id.  Reduced to family/tier tokens
        before it leaves the process; never shipped verbatim.
    :param raw_model: The router's own-vocabulary pick, when it needed
        resolving.  Only its presence is recorded.
    :param overrode_agent_model: ``True`` when the router overrode a model
        the calling agent asked for.
    :param decision_id: Decision identity shared with the transcript chip.
    """
    try:
        from omnigent.telemetry.installation_id import get_installation_id

        emit(
            RoutingDecisionEvent(
                installation_id=get_installation_id(),
                session_id=session_id,
                scope=scope,
                harness=harness,
                action=action,
                applied=applied,
                model_family=model_family(model),
                model_tier=model_tier(model),
                raw_model_resolved=bool(raw_model),
                overrode_agent_model=overrode_agent_model,
                decision_id=decision_id,
            )
        )
    except Exception:
        _logger.debug("Routing decision telemetry failed; dropping event", exc_info=True)


def record_routing_setting_changed(
    session_id: str,
    *,
    setting: str,
    value: str,
    user_id: str | None,
) -> None:
    """Record a mid-session routing setting change.

    :param session_id: Session whose setting changed.
    :param setting: Setting name, e.g. :data:`SETTING_SUBAGENT_ROUTING`.
    :param value: ``"on"``, ``"off"`` or ``"default"``.
    :param user_id: Requesting user, anonymised before emission.
    """
    try:
        from omnigent.telemetry.installation_id import get_installation_id

        installation_id = get_installation_id()
        emit(
            RoutingSettingChangedEvent(
                installation_id=installation_id,
                session_id=session_id,
                anon_user_id=_anon_user_id(installation_id, user_id),
                setting=setting,
                value=value,
            )
        )
    except Exception:
        _logger.debug("Routing setting telemetry failed; dropping event", exc_info=True)
