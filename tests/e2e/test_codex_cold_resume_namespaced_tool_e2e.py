"""E2E: Codex cold resume replays a namespaced tool call with its namespace.

A Codex session imported from ChatGPT/OpenUI can contain ``function_call``
items for tools outside the default namespace (``sleep`` in ``container``),
and the Responses backend rejects the next turn unless each such call is
round-tripped with its ``namespace`` field. This drives the production
cold-resume path with the real installed ``codex`` binary and a loopback
Responses provider that records the request Codex actually sends; that request
must retain the replayed call's namespace. No credentials or network needed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.conversation import CompactionData
from tests.e2e._codex_loopback_resume import run_cold_resume_turn, write_cold_resume_rollout
from tests.e2e._harness_probes import cli_unavailable_reason

_THREAD_ID = "019e96aa-0be2-7343-8d3b-6f914d60936c"
_SESSION_ID = "conv_cold_resume_namespaced_tool"
_CALL_ID = "call_sleep_in_container"
_NAMESPACE = "container"


def _stored_session_items() -> list[dict[str, Any]]:
    """Return imported history whose tool call lives outside the default namespace."""
    return [
        {
            "id": "msg_1",
            "response_id": "codex_turn_sleep",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Wait two seconds, then report back."}],
        },
        {
            "id": "fc_1",
            "response_id": "codex_turn_sleep",
            "type": "function_call",
            "name": "sleep",
            "namespace": _NAMESPACE,
            "arguments": json.dumps({"seconds": 2}),
            "call_id": _CALL_ID,
        },
        {
            "id": "fc_2",
            "response_id": "codex_turn_sleep",
            "type": "function_call",
            "name": "get_status",
            "namespace": _NAMESPACE,
            "arguments": "{}",
            "call_id": "call_status",
        },
        {
            "id": "fco_2",
            "response_id": "codex_turn_sleep",
            "type": "function_call_output",
            "call_id": "call_status",
            "output": "ready",
        },
        {
            "id": "fco_1",
            "response_id": "codex_turn_sleep",
            "type": "function_call_output",
            "call_id": _CALL_ID,
            "output": "slept for 2s",
        },
        {
            "id": "msg_2",
            "response_id": "codex_turn_sleep",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Done waiting."}],
        },
    ]


@pytest.mark.posix_only
@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "history_layout", ["flat", "compacted", "open_compacted", "partial_compacted"]
)
async def test_native_cold_resume_replays_namespaced_tool_call_with_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_layout: str,
) -> None:
    """The model receives namespaced calls and their parallel outputs in call order."""
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    source_home = tmp_path / "source-codex-home"
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    child_home = tmp_path / "home"
    for path in (source_home, codex_home, bridge_dir, workspace, child_home):
        path.mkdir()
    # Keep the real harness from importing the developer's Codex config,
    # login, hooks, or plugins into this isolated native process.
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    codex = shutil.which("codex")
    assert codex is not None

    items = _stored_session_items()
    if history_layout != "flat":
        snapshot_items = items if history_layout == "compacted" else items[:3]
        if history_layout == "partial_compacted":
            snapshot_items = [items[0], items[2]]
        snapshot = CompactionData(
            summary="Parallel tools are running.",
            last_item_id=snapshot_items[-1]["id"],
            token_count=1,
            compacted_messages=snapshot_items,
        )
        items = [
            *(items[:3] if history_layout == "partial_compacted" else []),
            {"type": "compaction", **snapshot.model_dump(exclude_none=True)},
            *items[3 if history_layout == "partial_compacted" else len(snapshot_items) :],
        ]
    rollout = await write_cold_resume_rollout(
        items,
        session_id=_SESSION_ID,
        thread_id=_THREAD_ID,
        codex_home=codex_home,
        workspace=workspace,
        codex_path=codex,
    )
    assert rollout.exists()

    turn = await run_cold_resume_turn(
        codex_path=codex,
        codex_home=codex_home,
        bridge_dir=bridge_dir,
        workspace=workspace,
        child_home=child_home,
        thread_id=_THREAD_ID,
        session_id=_SESSION_ID,
        prompt="Continue after the cold resume.",
    )

    assert turn.terminal_event.get("method") == "turn/completed", turn.terminal_event
    actual_input = turn.model_input()
    replayed_calls = [
        item
        for item in actual_input
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == _CALL_ID
    ]
    assert len(replayed_calls) == 1, (
        "cold resume did not replay the imported tool call: "
        f"input={json.dumps(actual_input, indent=2)}; stderr={turn.stderr!r}"
    )
    assert replayed_calls[0].get("name") == "sleep"
    assert replayed_calls[0].get("namespace") == _NAMESPACE, (
        "cold resume dropped the tool namespace the Responses backend requires: "
        f"call={json.dumps(replayed_calls[0])}; stderr={turn.stderr!r}"
    )
    calls = [item for item in actual_input if item.get("type") == "function_call"]
    outputs = [item for item in actual_input if item.get("type") == "function_call_output"]
    expected_order = (
        ["call_status", _CALL_ID]
        if history_layout == "partial_compacted"
        else [_CALL_ID, "call_status"]
    )
    assert [item["call_id"] for item in calls] == expected_order
    assert [item["call_id"] for item in outputs] == expected_order
    assert {item["call_id"]: item["output"] for item in outputs} == {
        _CALL_ID: "slept for 2s",
        "call_status": "ready",
    }
    status_call = next(item for item in calls if item["call_id"] == "call_status")
    assert status_call["name"] == "get_status"
    assert status_call["namespace"] == _NAMESPACE
