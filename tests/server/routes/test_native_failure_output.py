"""A failed native turn must not reuse a previous turn's assistant reply."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from omnigent.entities import ConversationItem, MessageData, PagedList
from omnigent.server.routes._sessions.orchestration import (
    _enrich_terminal_status_with_subagent_output,
)
from omnigent.stores.conversation_store import ConversationStore


def _message(role: str, text: str, response_id: str, *, is_meta: bool = False) -> ConversationItem:
    return ConversationItem(
        id=f"{response_id}_{role}",
        type="message",
        status="completed",
        response_id=response_id,
        created_at=1,
        data=MessageData(
            role=role,
            agent="worker" if role == "assistant" else None,
            is_meta=is_meta,
            content=[{"type": "text", "text": text}],
        ),
    )


async def _enrich(data: dict, items: list[ConversationItem]) -> dict:
    store = Mock(spec=ConversationStore)
    store.list_items.return_value = PagedList(data=items)
    return await _enrich_terminal_status_with_subagent_output(
        data, data["status"], "conv_test", store
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", [None, "resp_new"])
async def test_failed_turn_does_not_report_an_old_success(response_id: str | None) -> None:
    data = {"status": "failed"}
    if response_id is not None:
        data["response_id"] = response_id
    result = await _enrich(
        data,
        [
            _message("user", "Next task", "resp_new"),
            _message("assistant", "The previous task succeeded.", "resp_old"),
        ],
    )
    assert "output" not in result


@pytest.mark.asyncio
async def test_response_id_prevents_reusing_an_old_reply_without_a_user_item() -> None:
    result = await _enrich(
        {"status": "failed", "response_id": "resp_new"},
        [_message("assistant", "Previous reply", "resp_old")],
    )
    assert "output" not in result


@pytest.mark.asyncio
async def test_failed_turn_selects_its_own_detail_after_a_later_reply() -> None:
    result = await _enrich(
        {"status": "failed", "response_id": "resp_failed"},
        [
            _message("assistant", "Later turn succeeded", "resp_later"),
            _message("assistant", "Provider rejected this turn", "resp_failed"),
        ],
    )
    assert result["output"] == "Provider rejected this turn"


@pytest.mark.asyncio
async def test_legacy_failure_keeps_current_reply_and_ignores_meta_messages() -> None:
    result = await _enrich(
        {"status": "failed"},
        [
            _message("user", "Internal notice", "resp_meta", is_meta=True),
            _message("assistant", "Provider rejected this turn", "resp_current"),
            _message("user", "Current task", "resp_current"),
        ],
    )
    assert result["output"] == "Provider rejected this turn"


@pytest.mark.asyncio
async def test_wire_failure_reason_wins_over_stored_text() -> None:
    data = {"status": "failed", "response_id": "resp_new", "output": "Actual failure"}
    assert await _enrich(data, [_message("assistant", "Old reply", "resp_old")]) == data


@pytest.mark.asyncio
async def test_idle_enrichment_retains_its_existing_behavior() -> None:
    result = await _enrich(
        {"status": "idle"},
        [
            _message("user", "Follow-up", "resp_new"),
            _message("assistant", "Completed output", "resp_old"),
        ],
    )
    assert result["output"] == "Completed output"
