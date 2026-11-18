"""Pure regression coverage for bounded, private-data-free budget diagnostics."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tests.e2e.test_codex_subagent_session_budget_e2e import (
    _budget_request_evidence,
    _budget_state_summary,
)


def test_budget_evidence_is_independent_of_private_content() -> None:
    outputs = []
    for private in ("synthetic-private-a", "synthetic-private-b-雪🚀"):
        sensitive = dict.fromkeys(
            (
                "prompt",
                "text",
                "content",
                "arguments",
                "output",
                "headers",
                "env",
                "config",
                "tokens",
                "path",
                "id",
                "call_id",
                "namespace",
                "metadata",
            ),
            private,
        )
        requests = [
            {
                "input": [
                    {
                        "role": "assistant",
                        "type": "function_call",
                        "name": "exec_command",
                        **sensitive,
                    },
                    {"role": private, "type": private, "name": private, **sensitive},
                ],
                **sensitive,
            }
        ]
        states = {
            "parent": {"status": "idle", "pending_elicitations": [sensitive], **sensitive},
            "child": {
                "current_task_status": "in_progress",
                "busy": True,
                "pending_elicitations_count": 3,
                **sensitive,
            },
        }
        before = copy.deepcopy((requests, states))
        output = _budget_request_evidence(requests, states)
        assert "synthetic-private" not in output
        assert (requests, states) == before
        outputs.append(output)

    assert outputs[0] == outputs[1]
    evidence = json.loads(outputs[0])
    assert evidence["parent_at_cost_check"]["pending_elicitations_count"] == 1
    assert evidence["child_at_discovery"]["pending_elicitations_count"] == 3
    assert evidence["child_at_discovery"]["busy"] is True
    assert evidence["requests"][0]["items"][0]["name"] == "exec_command"
    unknown = evidence["requests"][0]["items"][1]
    assert all(unknown[key] == "other" for key in ("role", "type", "name"))


@pytest.mark.parametrize(
    "items,item_count,indices",
    [
        pytest.param(None, "other", [], id="null-input"),
        pytest.param(True, "other", [], id="boolean-input"),
        pytest.param(42, "other", [], id="numeric-input"),
        pytest.param({"text": "synthetic-private"}, "other", [], id="mapping-input"),
        pytest.param("synthetic-private", "other", [], id="string-input"),
        pytest.param([], 0, [], id="empty-input"),
        pytest.param(
            [None, True, 42, {"role": [], "type": {}, "name": 7}],
            4,
            [0, 1, 2, 3],
            id="malformed-items-and-unhashable-labels",
        ),
    ],
)
def test_budget_evidence_handles_malformed_items(
    items: Any, item_count: int | str, indices: list[int]
) -> None:
    output = _budget_request_evidence([{"input": items}], {})
    request = json.loads(output)["requests"][0]
    assert "synthetic-private" not in output
    assert request["item_count"] == item_count
    assert [item["item_index"] for item in request["items"]] == indices
    assert all(
        item[key] == "other" for item in request["items"] for key in ("role", "type", "name")
    )


@pytest.mark.parametrize(
    "request_count,item_count,request_indices,item_indices",
    [
        pytest.param(0, 0, [], [], id="empty-ledger"),
        pytest.param(4, 4, [0, 1, 2, 3], [0, 1, 2, 3], id="exact-tail-limit"),
        pytest.param(5, 6, [1, 2, 3, 4], [2, 3, 4, 5], id="independent-tail-limits"),
        pytest.param(
            128, 129, [124, 125, 126, 127], [125, 126, 127, 128], id="dense-ascii-byte-bound"
        ),
    ],
)
def test_budget_evidence_keeps_recent_structure_within_ascii_bound(
    request_count: int, item_count: int, request_indices: list[int], item_indices: list[int]
) -> None:
    item = {
        "role": "developer",
        "type": "function_call_output",
        "name": "mcp__omnigent.sys_session_send",
        "content": "synthetic-private-雪🚀" * 2048,
    }
    state = {
        "status": "waiting",
        "current_task_status": "in_progress",
        "busy": False,
        "pending_elicitations_count": 999_999,
    }
    output = _budget_request_evidence(
        [{"input": [item] * item_count} for _ in range(request_count)],
        {"parent": state, "child": state},
    )
    assert output.isascii()
    assert len(output.encode("ascii")) <= 4096
    assert "synthetic-private" not in output
    evidence = json.loads(output)
    assert evidence["request_count"] == request_count
    assert evidence["requests_omitted"] == request_count - len(request_indices)
    assert [request["request_index"] for request in evidence["requests"]] == request_indices
    for request in evidence["requests"]:
        assert request["item_count"] == item_count
        assert request["items_omitted"] == item_count - len(item_indices)
        assert [item["item_index"] for item in request["items"]] == item_indices
        assert all(item["name"] == "mcp__omnigent.sys_session_send" for item in request["items"])


@pytest.mark.parametrize(
    "count,expected",
    [
        (None, "unavailable"),
        (True, "unavailable"),
        (-1, "unavailable"),
        (0, 0),
        (1, 1),
        (999_999, 999_999),
        (1_000_000, "unavailable"),
        (1.0, "unavailable"),
        ("2", "unavailable"),
    ],
)
def test_budget_state_summary_bounds_elicitation_counts(count: Any, expected: int | str) -> None:
    assert (
        _budget_state_summary({"pending_elicitations_count": count})["pending_elicitations_count"]
        == expected
    )


def test_budget_state_summary_counts_pending_list_and_rejects_unknown_metadata() -> None:
    assert _budget_state_summary(
        {
            "status": "synthetic-private",
            "current_task_status": [],
            "busy": 1,
            "pending_elicitations": [{"text": "synthetic-private"}, "synthetic-private"],
            "pending_elicitations_count": 99,
            "extra": "synthetic-private",
        }
    ) == {
        "status": "other",
        "current_task_status": "other",
        "busy": "other",
        "pending_elicitations_count": 2,
    }
    launching = _budget_state_summary({"status": "launching", "current_task_status": "launching"})
    assert launching["status"] == "launching"
    assert launching["current_task_status"] == "launching"
