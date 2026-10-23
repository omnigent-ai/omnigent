"""Failed or cancelled native turns must not reuse a previous assistant reply."""

from __future__ import annotations

import json
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
@pytest.mark.parametrize(
    "status", [{"status": "failed"}, {"status": "idle", "turn_outcome": "cancelled"}]
)
@pytest.mark.parametrize("response_id", [None, "resp_new"])
async def test_failed_turn_does_not_report_an_old_success(
    response_id: str | None, status: dict
) -> None:
    data = dict(status)
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
@pytest.mark.parametrize(
    "status", [{"status": "failed"}, {"status": "idle", "turn_outcome": "cancelled"}]
)
async def test_response_id_prevents_reusing_an_old_reply_without_a_user_item(status: dict) -> None:
    result = await _enrich(
        {**status, "response_id": "resp_new"},
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
@pytest.mark.parametrize(
    "status", [{"status": "failed"}, {"status": "idle", "turn_outcome": "cancelled"}]
)
async def test_legacy_failure_keeps_current_reply_and_ignores_meta_messages(status: dict) -> None:
    result = await _enrich(
        status,
        [
            _message("user", "Internal notice", "resp_meta", is_meta=True),
            _message("assistant", "Provider rejected this turn", "resp_current"),
            _message("user", "Current task", "resp_current"),
        ],
    )
    assert result["output"] == "Provider rejected this turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["output", "failure_detail", "both"])
@pytest.mark.parametrize("structured", [False, True])
async def test_wire_failure_reason_wins_over_stored_text(source: str, structured: bool) -> None:
    reason = "Actual failure"
    if structured:
        reason = json.dumps(
            {
                "error_code": "BAD_REQUEST",
                "message": json.dumps(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "array_above_max_length",
                            "param": "input[3].content",
                            "message": "Invalid input content count.",
                        }
                    }
                ),
            }
        )
    detail_source = "failure_detail" if source == "both" else source
    data = {"status": "failed", "response_id": "resp_new", detail_source: reason}
    if source == "both":
        data["output"] = json.dumps({"error": {"code": "prose-code", "param": "prose-param"}})
    result = await _enrich(data, [_message("assistant", "Old reply", "resp_old")])
    context = result.pop("failure_context")
    assert context["detail_source"] == f"external_status_{detail_source}"
    if structured:
        assert context["provider_error_type"] == "invalid_request_error"
        assert context["provider_error_code"] == "array_above_max_length"
        assert context["provider_error_param"] == "input[3].content"
    else:
        assert "provider_error_code" not in context
    assert result == {**data, "output": reason}


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_output", [None, "Now add the block on the new branch:"])
async def test_harness_failure_detail_wins_over_stored_prose(
    partial_output: str | None,
) -> None:
    data = {
        "status": "failed",
        "response_id": "resp_failed",
        "failure_detail": " API Error: 500 Overloaded ",
    }
    if partial_output is not None:
        data["output"] = partial_output
    result = await _enrich(
        data,
        [_message("assistant", "Now add the block on the new branch:", "resp_failed")],
    )
    assert result["output"] == "API Error: 500 Overloaded"
    assert result["failure_context"]["detail_source"] == "external_status_failure_detail"


@pytest.mark.asyncio
async def test_structured_source_evidence_wins_over_parsed_wire_text() -> None:
    result = await _enrich(
        {
            "status": "failed",
            "output": json.dumps({"error": {"code": "weaker-text-code", "param": "input"}}),
            "failure_context": {"provider_error_code": "source-code"},
        },
        [],
    )
    assert result["failure_context"]["provider_error_code"] == "source-code"
    assert result["failure_context"]["provider_error_param"] == "input"


@pytest.mark.asyncio
async def test_blank_failure_detail_falls_back_to_the_store() -> None:
    result = await _enrich(
        {"status": "failed", "response_id": "resp_failed", "failure_detail": "  "},
        [_message("assistant", "Provider rejected this turn", "resp_failed")],
    )
    assert result["output"] == "Provider rejected this turn"
    assert result["failure_context"]["detail_source"] == "assistant_output_fallback"


@pytest.mark.asyncio
async def test_failure_without_output_marks_missing_detail_and_keeps_category() -> None:
    result = await _enrich(
        {"status": "failed", "failure_context": {"native_error_category": "server_error"}}, []
    )
    assert "output" not in result
    assert result["failure_context"]["native_error_category"] == "server_error"
    assert result["failure_context"]["detail_source"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_prose", [False, True])
async def test_store_fallback_overrides_stale_claimed_provenance(structured_prose: bool) -> None:
    text = "I am waiting for a background task."
    if structured_prose:
        text = json.dumps({"error": {"code": "example-from-assistant-prose"}})
    result = await _enrich(
        {
            "status": "failed",
            "response_id": "resp_failed",
            "failure_context": {
                "native_error_category": "server_error",
                "detail_source": "missing",
            },
        },
        [_message("assistant", text, "resp_failed")],
    )
    assert result["failure_context"]["native_error_category"] == "server_error"
    assert result["failure_context"]["detail_source"] == "assistant_output_fallback"
    assert "provider_error_code" not in result["failure_context"]


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
