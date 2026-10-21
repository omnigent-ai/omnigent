"""Authorize explicit preference handoff before creating any session artifacts."""

from __future__ import annotations

from fastapi import HTTPException, Request

from omnigent.preference_session import encode_preference_snapshot
from omnigent.preference_startup import PreferenceLaunch


def validate_session_preferences(
    request: Request,
    user_id: str | None,
    snapshot: PreferenceLaunch | None,
    *,
    labels: dict[str, str],
) -> None:
    if snapshot is None:
        return
    store = getattr(request.app.state, "preference_store", None)
    if store is None:
        from omnigent.server.auth import RESERVED_USER_LOCAL, local_single_user_enabled

        # An ephemeral single-user server can execute an offline, already frozen choice.
        if user_id is not None and not (
            user_id == RESERVED_USER_LOCAL and local_single_user_enabled()
        ):
            raise HTTPException(409, "This server does not support saved session preferences")
    elif snapshot.shared.identity != store.identity(user_id):
        raise HTTPException(403, "Session preferences belong to another account or workspace")
    if labels.get("omnigent.preferences.temporary") == "true":
        raise HTTPException(422, "Temporary sessions cannot adopt shared preferences")
    try:
        encode_preference_snapshot(snapshot.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(422, "Session preferences require an adopted workspace") from exc
