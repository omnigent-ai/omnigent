"""Tests for mirroring Devin's hook stream into an Omnigent conversation.

The payloads below are verbatim captures from devin 3000.10.21 (a single
``echo`` turn), so the mapping is asserted against the vendor's real wire shape
rather than a guess at it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.devin_native.forwarder import (
    _ForwardState,
    _handle_event,
    _read_state,
    _tool_output_text,
    _TurnState,
    _write_state,
)

_PROMPT_ID = "a288f722-4546-4b80-af63-7264e6516b5c"
_TURN = f"devin:turn:{_PROMPT_ID}"

_SESSION_START = {
    "hook_event_name": "SessionStart",
    "source": "startup",
    "session_id": "childish-receipt",
}
_USER_PROMPT = {
    "hook_event_name": "UserPromptSubmit",
    "prompt": "Run this exact shell command: echo OK",
    "session_id": "childish-receipt",
    "prompt_id": _PROMPT_ID,
}
_PRE_TOOL = {
    "hook_event_name": "PreToolUse",
    "tool_name": "exec",
    "tool_input": {"command": "echo OK"},
    "tool_use_id": "exec_0",
    "session_id": "childish-receipt",
    "prompt_id": _PROMPT_ID,
}
_POST_TOOL = {
    "hook_event_name": "PostToolUse",
    "tool_name": "exec",
    "tool_input": {"command": "echo OK"},
    "tool_use_id": "exec_0",
    "tool_response": {"success": True, "output": "OK\n\nExit code: 0", "error": None},
    "session_id": "childish-receipt",
    "prompt_id": _PROMPT_ID,
}
_STOP = {
    "hook_event_name": "Stop",
    "stop_hook_active": False,
    "last_assistant_message": "Output:\n\n```\nOK\n```",
    "session_id": "childish-receipt",
    "prompt_id": _PROMPT_ID,
}


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """Captures what the forwarder would POST/PATCH to the Sessions API."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.posts.append((url, json or {}))
        return _FakeResponse()

    async def patch(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.patches.append((url, json or {}))
        return _FakeResponse()

    def items(self, item_type: str) -> list[dict[str, Any]]:
        return [
            body["data"]["item_data"]
            for _url, body in self.posts
            if body.get("type") == "external_conversation_item"
            and body["data"]["item_type"] == item_type
        ]

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [body["data"] for _url, body in self.posts if body.get("type") == event_type]


async def _drive(
    client: _FakeClient,
    payloads: list[dict[str, Any]],
    bridge_dir: Path,
    state: _ForwardState | None = None,
) -> tuple[_ForwardState, _TurnState]:
    state = state or _ForwardState()
    turn = _TurnState()
    for payload in payloads:
        await _handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="devin-native-ui",
            payload=payload,
            state=state,
            turn=turn,
        )
    return state, turn


@pytest.mark.asyncio
async def test_full_turn_maps_to_omnigent_items(tmp_path: Path) -> None:
    client = _FakeClient()
    await _drive(client, [_SESSION_START, _USER_PROMPT, _PRE_TOOL, _POST_TOOL, _STOP], tmp_path)

    # Devin's session id is persisted so a later resume can reattach the TUI.
    assert client.patches == [
        ("/v1/sessions/conv_abc", {"external_session_id": "childish-receipt"})
    ]

    user = client.items("message")[0]
    assert user["role"] == "user"
    assert user["content"][0]["text"].startswith("Run this exact shell command")

    call = client.items("function_call")[0]
    assert call["name"] == "exec"
    assert call["call_id"] == "exec_0"
    assert json.loads(call["arguments"]) == {"command": "echo OK"}

    output = client.items("function_call_output")[0]
    assert output["call_id"] == "exec_0"
    assert "OK" in output["output"]

    assistant = client.items("message")[1]
    assert assistant["role"] == "assistant"
    assert assistant["agent"] == "devin-native-ui"
    assert "```" in assistant["content"][0]["text"]

    # The turn closes exactly once, carrying the same id every item used.
    statuses = client.events("external_session_status")
    assert statuses == [{"status": "idle", "response_id": _TURN}]


@pytest.mark.asyncio
async def test_tool_call_and_result_share_devins_tool_use_id(tmp_path: Path) -> None:
    # tool_use_id is what makes the web's tool card pair with its output without
    # any ordering heuristic.
    client = _FakeClient()
    await _drive(client, [_USER_PROMPT, _PRE_TOOL, _POST_TOOL, _STOP], tmp_path)
    assert client.items("function_call")[0]["call_id"] == "exec_0"
    assert client.items("function_call_output")[0]["call_id"] == "exec_0"


@pytest.mark.asyncio
async def test_every_item_carries_the_prompt_id_as_turn_id(tmp_path: Path) -> None:
    client = _FakeClient()
    await _drive(client, [_USER_PROMPT, _PRE_TOOL, _POST_TOOL, _STOP], tmp_path)
    response_ids = {
        body["data"].get("response_id")
        for _url, body in client.posts
        if body.get("type") == "external_conversation_item"
    }
    assert response_ids == {_TURN}


@pytest.mark.asyncio
async def test_new_prompt_closes_a_turn_left_open(tmp_path: Path) -> None:
    # A turn whose Stop never arrived (TUI killed mid-turn) must not swallow the
    # next turn's status edge.
    client = _FakeClient()
    second = dict(_USER_PROMPT, prompt_id="second-prompt", prompt="again")
    await _drive(client, [_USER_PROMPT, _PRE_TOOL, second], tmp_path)
    assert client.events("external_session_status") == [{"status": "idle", "response_id": _TURN}]


@pytest.mark.asyncio
async def test_reasoning_and_usage_come_from_the_atif_export(tmp_path: Path) -> None:
    # Hooks carry neither reasoning nor tokens; the export Devin rewrites after
    # each turn is the only source.
    (tmp_path / "transcript.atif.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "agent": {"model_name": "SWE-2 High"},
                "steps": [
                    {"source": "system", "message": "prompt"},
                    {
                        "source": "agent",
                        "message": "Output",
                        "reasoning_content": "The user wants the command run.",
                    },
                ],
                "final_metrics": {
                    "total_prompt_tokens": 25_899,
                    "total_completion_tokens": 32,
                    "total_cached_tokens": 8_192,
                },
            }
        ),
        encoding="utf-8",
    )
    client = _FakeClient()
    state, _turn = await _drive(client, [_USER_PROMPT, _STOP], tmp_path)

    reasoning = client.items("reasoning")[0]
    assert reasoning["content"][0]["text"] == "The user wants the command run."

    usage = client.events("external_session_usage")[0]
    assert usage == {
        "cumulative_input_tokens": 25_899,
        "cumulative_output_tokens": 32,
        "cumulative_cache_read_input_tokens": 8_192,
        "model": "SWE-2 High",
    }
    assert state.input_tokens == 25_899


@pytest.mark.asyncio
async def test_usage_is_not_reposted_when_the_export_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "transcript.atif.json").write_text(
        json.dumps({"final_metrics": {"total_prompt_tokens": 10}}), encoding="utf-8"
    )
    client = _FakeClient()
    state, _t = await _drive(client, [_USER_PROMPT, _STOP], tmp_path)
    await _drive(client, [_USER_PROMPT, _STOP], tmp_path, state=state)
    # Cumulative totals only advance; an unchanged export must be a no-op.
    assert len(client.events("external_session_usage")) == 1


@pytest.mark.asyncio
async def test_missing_export_does_not_break_the_mirror(tmp_path: Path) -> None:
    client = _FakeClient()
    await _drive(client, [_USER_PROMPT, _STOP], tmp_path)
    assert client.items("message")[-1]["role"] == "assistant"
    assert client.events("external_session_usage") == []


@pytest.mark.asyncio
async def test_compaction_is_surfaced(tmp_path: Path) -> None:
    client = _FakeClient()
    await _drive(
        client,
        [{"hook_event_name": "PostCompaction", "summary": "we discussed the parser"}],
        tmp_path,
    )
    assert client.events("external_compaction_status") == [{"status": "completed"}]
    assert "we discussed the parser" in client.items("message")[0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_tool_result_without_its_call_still_posts_output(tmp_path: Path) -> None:
    # A PreToolUse hook that failed to record must not hide the tool's result.
    client = _FakeClient()
    await _drive(client, [_USER_PROMPT, _POST_TOOL, _STOP], tmp_path)
    assert client.items("function_call_output")[0]["call_id"] == "exec_0"


class TestToolOutputText:
    """Devin's ``tool_response`` becomes the text on a result card."""

    def test_prefers_output(self) -> None:
        assert _tool_output_text({"success": True, "output": "hi", "error": None}) == "hi"

    def test_falls_back_to_error(self) -> None:
        assert _tool_output_text({"success": False, "output": "", "error": "boom"}) == "boom"

    def test_successful_silence_reads_as_no_output(self) -> None:
        assert _tool_output_text({"success": True, "output": "", "error": None}) == "(no output)"

    def test_plain_string_passes_through(self) -> None:
        assert _tool_output_text("raw") == "raw"

    def test_none_is_empty(self) -> None:
        assert _tool_output_text(None) == ""


class TestStatePersistence:
    """A supervisor restart resumes rather than replaying the conversation."""

    def test_round_trip(self, tmp_path: Path) -> None:
        state = _ForwardState(
            hooks_offset=512, devin_session_id="fancy-spring", input_tokens=7, output_tokens=3
        )
        _write_state(tmp_path, state)
        loaded = _read_state(tmp_path)
        assert loaded.hooks_offset == 512
        assert loaded.devin_session_id == "fancy-spring"
        assert loaded.input_tokens == 7
        assert loaded.output_tokens == 3

    def test_absent_state_starts_at_the_beginning(self, tmp_path: Path) -> None:
        assert _read_state(tmp_path).hooks_offset == 0

    def test_corrupt_state_starts_at_the_beginning(self, tmp_path: Path) -> None:
        (tmp_path / "devin_forwarder_state.json").write_text("{not json", encoding="utf-8")
        assert _read_state(tmp_path).hooks_offset == 0
