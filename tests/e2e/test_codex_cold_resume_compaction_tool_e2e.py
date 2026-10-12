"""E2E: Codex cold resume preserves a tool call spanning compaction.

This reproduces the production failure with the real native Codex harness:

1. Omnigent history contains a function call, a compaction captured while the
   tool is still running, and the matching function output after compaction.
2. The production cold-resume path rebuilds the local Codex rollout from that
   server history.
3. The production native app-server wrapper starts the installed ``codex``
   binary, preloads the rebuilt thread, and submits a real turn.
4. A loopback Responses provider records the context Codex actually sends.

Before the fix, rollout synthesis discards the pre-compaction function call.
Codex then reports an orphan function output, removes it during context
normalization, and sends neither side of the completed tool interaction to the
provider. The test requires the real provider request to contain the matched
call/output pair in order, proving resumed context did not lose the result.

The provider is loopback-only, so the test needs no credentials or network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._codex_loopback_resume import run_cold_resume_turn, write_cold_resume_rollout
from tests.e2e._harness_probes import cli_unavailable_reason

_THREAD_ID = "019e96aa-0be2-7343-8d3b-6f914d60936b"
_SESSION_ID = "conv_cold_resume_inflight_tool"
_CALL_ID = "call_tool_spanning_compaction"
_TOOL_OUTPUT = "delayed-tool-output-survived-cold-resume"


def _stored_session_items() -> list[dict[str, Any]]:
    """Return the persisted ordering observed when compaction races a tool."""
    return [
        {
            "id": "msg_1",
            "response_id": "codex_turn_slow_tool",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Run the slow diagnostic."}],
        },
        {
            "id": "fc_1",
            "response_id": "codex_turn_slow_tool",
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "printf delayed"}),
            "call_id": _CALL_ID,
        },
        {
            "id": "cmp_1",
            "response_id": "compact_1",
            "type": "compaction",
            "summary": "The user requested a slow diagnostic command.",
            "last_item_id": "fc_1",
            "token_count": 42,
            "window_id": "01a070e2-2665-7d62-9b74-973decf239b7",
            # The snapshot was taken while the tool was running, so it does
            # not yet contain either half of the in-flight interaction.
            "compacted_messages": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Run the slow diagnostic."}],
                }
            ],
        },
        {
            "id": "fco_1",
            "response_id": "codex_turn_slow_tool",
            "type": "function_call_output",
            "call_id": _CALL_ID,
            "output": _TOOL_OUTPUT,
        },
        {
            "id": "msg_2",
            "response_id": "codex_turn_slow_tool",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "The slow diagnostic completed."}],
        },
    ]


@pytest.mark.posix_only
@pytest.mark.timeout(300)
async def test_native_cold_resume_keeps_tool_output_that_finishes_after_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model receives both halves of a tool interaction after cold resume."""
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
    call_indexes = [
        index
        for index, item in enumerate(actual_input)
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == _CALL_ID
    ]
    output_indexes = [
        index
        for index, item in enumerate(actual_input)
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == _CALL_ID
        and _TOOL_OUTPUT in str(item.get("output"))
    ]
    assert len(call_indexes) == len(output_indexes) == 1 and call_indexes[0] < output_indexes[0], (
        "cold resume lost the matched tool interaction while Codex normalized "
        f"the rebuilt context: input={json.dumps(actual_input, indent=2)}; "
        f"stderr={turn.stderr!r}"
    )
    assert not any("Orphan function call output" in line for line in turn.stderr), turn.stderr
