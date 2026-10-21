"""Short-lived grants for preference RPCs inside an authenticated owner action."""

from __future__ import annotations

from contextvars import Context, copy_context
from dataclasses import dataclass, field
from functools import partial
from typing import Literal, cast

from fastapi import HTTPException

from omnigent.errors import OmnigentError
from omnigent.preference_broker import PreferenceRead, PreferenceReply, PreferenceRequest
from omnigent.preference_client import MAX_PENDING_OPERATIONS
from omnigent.preference_controls import PreferenceApply, PreferenceControl
from omnigent.server.preference_requests import run_preference_work
from omnigent.stores.preference_store import PreferenceStore


@dataclass
class PreferenceBrokerGrant:
    store: PreferenceStore
    user_id: str | None
    installation_id: str
    control: PreferenceControl
    context: Context = field(default_factory=copy_context)
    remaining: int = 2 * MAX_PENDING_OPERATIONS
    busy: bool = False

    async def execute(self, request: PreferenceRequest) -> PreferenceReply:
        """Only an explicit sync or reviewed import can publish this host's edits."""
        if self.remaining <= 0:
            return PreferenceReply(status=429)
        self.remaining -= 1
        try:
            if isinstance(request, PreferenceRead):
                page = await run_preference_work(
                    self.user_id,
                    partial(
                        self.store.read,
                        user_id=self.user_id,
                        limit=request.limit,
                        cursor=request.cursor,
                        protocol_version=cast(Literal[1, 2], request.protocol_version),
                    ),
                )
                return PreferenceReply(status=200, page=page)
            operation = request.operation
            if (
                self.control.action not in ("apply_import", "sync")
                or operation.identity != self.store.identity(self.user_id)
                or operation.installation_id != self.installation_id
            ):
                return PreferenceReply(status=403)
            if isinstance(self.control, PreferenceApply):
                reviewed = next(
                    (
                        change.operation
                        for change in self.control.preview.changes
                        if change.operation.operation_id == operation.operation_id
                    ),
                    None,
                )
                if (
                    reviewed is None
                    or operation.model_copy(
                        update={"observed_revision": reviewed.observed_revision}
                    )
                    != reviewed
                ):
                    return PreferenceReply(status=403)
            result = await run_preference_work(
                self.user_id, partial(self.store.apply, operation, user_id=self.user_id)
            )
            return PreferenceReply(status=200, result=result)
        except HTTPException as exc:
            return PreferenceReply(status=exc.status_code)
        except OmnigentError as exc:
            return PreferenceReply(status=exc.http_status)
        except Exception:  # noqa: BLE001 — do not expose database errors to the host
            return PreferenceReply(status=503)
