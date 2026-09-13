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

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = body or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClient:
    """Captures what the forwarder would POST/PATCH to the Sessions API."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        body = json or {}
        self.posts.append((url, body))
        # Mirror the server's external_devin_subagent_start reply so the
        # forwarder can address the child's transcript to the minted id.
        if body.get("type") == "external_devin_subagent_start":
            agent_id = body["data"]["agent_id"]
            return _FakeResponse({"child_session_id": f"conv_child_{agent_id}"})
        return _FakeResponse()

    async def patch(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.patches.append((url, json or {}))
        return _FakeResponse()

    def _items(self, session_id: str, item_type: str) -> list[dict[str, Any]]:
        return [
            body["data"]["item_data"]
            for url, body in self.posts
            if url == f"/v1/sessions/{session_id}/events"
            and body.get("type") == "external_conversation_item"
            and body["data"]["item_type"] == item_type
        ]

    def items(self, item_type: str) -> list[dict[str, Any]]:
        return self._items("conv_abc", item_type)

    def child_items(self, child_id: str, item_type: str) -> list[dict[str, Any]]:
        return self._items(child_id, item_type)

    def events(self, event_type: str, *, session_id: str = "conv_abc") -> list[dict[str, Any]]:
        return [
            body["data"]
            for url, body in self.posts
            if url == f"/v1/sessions/{session_id}/events" and body.get("type") == event_type
        ]


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


# The one.txt sub-agent's task, verbatim as the run_subagent hook delivers it and
# as it appears as that sub-agent's first user node in the captured fixture.
_ONE_TASK = (
    "Write a file at /tmp/devin-sub2/work/one.txt whose contents are exactly:\n\n"
    "ONE\n\nUse your file-writing tool to create it. Report back when done."
)
_SUBAGENT_FIXTURE = json.loads(
    (Path(__file__).parent / "data" / "devin_subagent_nodes.json").read_text()
)


def _run_subagent_post(agent_id: str, task: str, title: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "run_subagent",
        "tool_input": {"title": title, "task": task, "is_background": True},
        "tool_use_id": "run_subagent_0",
        "tool_response": {
            "success": True,
            "output": f"Background subagent started with agent_id={agent_id}. You can wait…",
            "error": None,
        },
        "session_id": "possible-umbra",
        "prompt_id": "p1",
    }


@pytest.mark.asyncio
async def test_run_subagent_spawn_is_recorded(tmp_path: Path) -> None:
    # The agent_id rides the tool-result text; the task/title come from the input.
    client = _FakeClient()
    state, _turn = await _drive(
        client, [_run_subagent_post("690d786b", _ONE_TASK, "Write one.txt")], tmp_path
    )
    assert "690d786b" in state.subagents
    assert state.subagents["690d786b"]["task"] == _ONE_TASK
    assert state.subagents["690d786b"]["title"] == "Write one.txt"
    assert state.subagents["690d786b"]["mirrored"] is False


@pytest.mark.asyncio
async def test_completed_subagent_is_mirrored_as_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the forwarder's session-store read at the captured fixture instead of
    # a live Devin DB, so the mirror runs end-to-end deterministically.
    import omnigent.harnesses.devin_native.forwarder as fwd

    monkeypatch.setattr(fwd, "devin_sessions_db_path", lambda _env: tmp_path / "sessions.db")
    monkeypatch.setattr(fwd, "load_message_nodes", lambda _db, _sid: _SUBAGENT_FIXTURE)

    client = _FakeClient()
    payloads = [
        {"hook_event_name": "SessionStart", "source": "startup", "session_id": "possible-umbra"},
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "spawn a sub-agent",
            "session_id": "possible-umbra",
            "prompt_id": "p1",
        },
        _run_subagent_post("690d786b", _ONE_TASK, "Write one.txt"),
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "done",
            "session_id": "possible-umbra",
            "prompt_id": "p1",
        },
    ]
    state, _turn = await _drive(client, payloads, tmp_path)

    # A child session was minted for the sub-agent, keyed on Devin's agent_id.
    starts = [
        body["data"]
        for _u, body in client.posts
        if body.get("type") == "external_devin_subagent_start"
    ]
    assert [s["agent_id"] for s in starts] == ["690d786b"]
    assert starts[0]["title"] == "Write one.txt"

    child_id = "conv_child_690d786b"
    # Its full internal transcript landed in the child: the task prompt, the write
    # tool call with its real arguments, and the sub-agent's final message.
    messages = client.child_items(child_id, "message")
    assert messages[0]["role"] == "user" and "one.txt" in messages[0]["content"][0]["text"]
    assert messages[-1]["role"] == "assistant" and "one.txt" in messages[-1]["content"][0]["text"]
    calls = client.child_items(child_id, "function_call")
    write = next(c for c in calls if c["name"] == "write")
    assert json.loads(write["arguments"])["file_path"].endswith("one.txt")
    assert any(
        o["call_id"] == write["call_id"]
        for o in client.child_items(child_id, "function_call_output")
    )

    # The child is closed idle under the sub-agent's own turn id, and the mirror
    # is marked done so a re-run does not duplicate it.
    statuses = client.events("external_session_status", session_id=child_id)
    assert len(statuses) == 1 and statuses[0]["status"] == "idle"
    assert statuses[0]["response_id"] == "devin:subagent:690d786b"
    assert state.subagents["690d786b"]["mirrored"] is True


@pytest.mark.asyncio
async def test_subagent_not_mirrored_until_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A spawn whose completion notification is absent from the store must not be
    # mirrored yet — the transcript may still be growing.
    import omnigent.harnesses.devin_native.forwarder as fwd

    monkeypatch.setattr(fwd, "devin_sessions_db_path", lambda _env: tmp_path / "sessions.db")
    monkeypatch.setattr(fwd, "load_message_nodes", lambda _db, _sid: [])

    client = _FakeClient()
    payloads = [
        {"hook_event_name": "SessionStart", "source": "startup", "session_id": "possible-umbra"},
        _run_subagent_post("690d786b", _ONE_TASK, "Write one.txt"),
        {"hook_event_name": "Stop", "session_id": "possible-umbra", "prompt_id": "p1"},
    ]
    state, _turn = await _drive(client, payloads, tmp_path)
    assert not [b for _u, b in client.posts if b.get("type") == "external_devin_subagent_start"]
    assert state.subagents["690d786b"]["mirrored"] is False
