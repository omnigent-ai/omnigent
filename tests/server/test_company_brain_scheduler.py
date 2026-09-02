from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import IntegrationSelection
from omnigent.server.company_brain_scheduler import CompanyBrainScheduler
from omnigent.server.company_brain_service import CompanyBrainService
from omnigent.stores.company_brain_store import CompanyBrainStore


def _selection() -> IntegrationSelection:
    return IntegrationSelection(
        id="selection-1",
        workspace_id=77,
        connection_id="connection-1",
        external_resource_id="page-1",
        resource_name="Policies",
        resource_type="notion_page",
        source_url="https://www.notion.so/page-1",
        transform_profile="notion-page.v1",
        visibility_class="org-shared",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        timezone="UTC",
        state="active",
        last_synced_at=None,
        page_count=0,
        last_error=None,
        created_at=1,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_scheduled_fire_enters_selection_workspace() -> None:
    selection = _selection()

    class Store:
        def list_scheduled_selections_all_workspaces(self) -> list[IntegrationSelection]:
            return [selection]

    class Service:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        async def sync_selection(self, selection_id: str, *, trigger: str) -> Any:
            self.calls.append((selection_id, trigger, current_workspace_id()))
            return None

    service = Service()
    handles: list[object] = []
    scheduler = CompanyBrainScheduler(
        cast(CompanyBrainStore, Store()),
        cast(CompanyBrainService, service),
        now=lambda: 1_700_000_000.0,
        schedule_call=lambda delay, callback: handles.append((delay, callback)) or handles[-1],
        cancel_call=lambda handle: None,
    )
    await scheduler.start()

    assert await scheduler.fire(selection.id) is True
    assert service.calls == [(selection.id, "schedule", 77)]

    scheduler.update(replace(selection, state="paused"))
    assert await scheduler.fire(selection.id) is False
