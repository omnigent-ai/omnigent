from __future__ import annotations

from typing import Any, cast

from omnigent.db.db_models import workspace_scope
from omnigent.entities import IntegrationSelection
from omnigent.server.company_brain_service import CompanyBrainService
from omnigent.server.scheduled.scheduler import ScheduledTaskScheduler
from omnigent.stores.company_brain_store import CompanyBrainStore


class _SelectionScheduleSource:
    def __init__(self, store: CompanyBrainStore) -> None:
        self._store = store

    def list_active_all_workspaces(self) -> list[Any]:
        return cast(list[Any], self._store.list_scheduled_selections_all_workspaces())


class CompanyBrainScheduler:
    def __init__(
        self,
        store: CompanyBrainStore,
        service: CompanyBrainService,
        **timing: Any,
    ) -> None:
        async def on_fire(workspace_id: int, selection_id: str) -> None:
            with workspace_scope(workspace_id):
                await service.sync_selection(selection_id, trigger="schedule")

        self._scheduler = ScheduledTaskScheduler(
            _SelectionScheduleSource(store),
            on_fire,
            **timing,
        )

    async def start(self) -> None:
        await self._scheduler.start()

    def stop(self) -> None:
        self._scheduler.stop()

    def add(self, selection: IntegrationSelection) -> None:
        if selection.rrule:
            self._scheduler.add(cast(Any, selection))

    def update(self, selection: IntegrationSelection) -> None:
        if selection.rrule:
            self._scheduler.update(cast(Any, selection))
        else:
            self._scheduler.remove(selection.id)

    def remove(self, selection_id: str) -> None:
        self._scheduler.remove(selection_id)

    async def fire(self, selection_id: str) -> bool:
        return await self._scheduler.fire(selection_id)

    def next_run_at(self, selection_id: str) -> str | None:
        return self._scheduler.next_run_at(selection_id)
