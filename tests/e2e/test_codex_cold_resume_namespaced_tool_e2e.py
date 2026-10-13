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
async def test_native_cold_resume_replays_namespaced_tool_call_with_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model receives the replayed call with the namespace it was issued under."""
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

    rollout = await write_cold_resume_rollout(
        _stored_session_items(),
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
