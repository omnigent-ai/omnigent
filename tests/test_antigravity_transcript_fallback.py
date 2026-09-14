"""Regression checks for agy 1.2.x's session-scoped transcript read path."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import omnigent.inner.antigravity_native_executor as executor_mod
from omnigent.harnesses.antigravity_native import bridge, reader, transcript
from omnigent.harnesses.antigravity_native.bridge import (
    AntigravityNativeBridgeState,
    write_bridge_state,
)
from omnigent.harnesses.antigravity_native.stop_hook import (
    STOP_EVENTS_FILE,
    record_stop_event,
)

CONVERSATION_ID = "8bb3c819-e505-4812-b0f6-895bd2ec1f98"
OTHER_ID = "318ab05c-d9d7-45a4-b722-3d4f54bbe868"


def _session_files(bridge_dir: Path) -> tuple[Path, Path]:
    app = bridge_dir / "agy-home" / ".gemini" / "antigravity-cli"
    transcript_path = (
        app / "brain" / CONVERSATION_ID / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    transcript_path.parent.mkdir(parents=True)
    transcript_path.touch()
    cache = app / "cache" / "last_conversations.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"/scratch": CONVERSATION_ID}))
    return transcript_path, cache


def _step(index: int, source: str, kind: str, content: str, *, status: str = "DONE") -> str:
    return (
        json.dumps(
            {
                "step_index": index,
                "source": source,
                "type": kind,
                "status": status,
                "created_at": f"2026-09-13T02:00:{index:02d}Z",
                "content": content,
            }
        )
        + "\n"
    )


def test_transcript_discovery_rejects_ambiguous_and_symlinked_sources(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, cache = _session_files(bridge_dir)
    assert transcript.resolve_owned_transcript(bridge_dir) == transcript.TranscriptBinding(
        CONVERSATION_ID, transcript_path
    )

    cache.write_text(json.dumps({"/scratch": CONVERSATION_ID, "/other": OTHER_ID}))
    assert transcript.resolve_owned_transcript(bridge_dir) is None

    cache.write_text(json.dumps({"/scratch": CONVERSATION_ID}))
    transcript_path.unlink()
    foreign = tmp_path / "foreign-transcript.jsonl"
    foreign.write_text("{}\n")
    transcript_path.symlink_to(foreign)
    assert transcript.resolve_owned_transcript(bridge_dir) is None


def test_jsonl_tail_waits_for_complete_line_and_handles_replacement(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text('{"step_index":1', encoding="utf-8")
    tail = transcript.JsonlTail(path)
    assert tail.read() == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write("}\n")
    assert tail.read() == [{"step_index": 1}]
    assert tail.read() == []

    replacement = tmp_path / "replacement"
    replacement.write_text('{"step_index":1}\n{"step_index":2}\n')
    replacement.replace(path)
    assert tail.read() == [{"step_index": 1}, {"step_index": 2}]


def test_boundary_and_offsets_cover_only_complete_lines(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    first = _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>one</USER_REQUEST>")
    transcript_path.write_text(first + '{"incomplete":')
    assert transcript.transcript_boundary(bridge_dir, CONVERSATION_ID) == (
        transcript_path.stat().st_dev,
        transcript_path.stat().st_ino,
        len(first.encode()),
    )
    tail = transcript.JsonlTail(transcript_path, include_offsets=True)
    assert tail.read()[0]["_transcript_end_offset"] == len(first.encode())


def test_scoped_tail_rejects_later_symlink_swap(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(_step(1, "MODEL", "PLANNER_RESPONSE", "owned"))
    tail = transcript.JsonlTail(transcript_path, safe_root=bridge_dir)
    assert len(tail.read()) == 1
    transcript_path.unlink()
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text(_step(2, "MODEL", "PLANNER_RESPONSE", "foreign"))
    transcript_path.symlink_to(foreign)
    assert tail.read() == []


def test_baseline_tracks_inode_and_ignores_stale_cache(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(_step(0, "MODEL", "PLANNER_RESPONSE", "old"))
    transcript.prepare_transcript_capture(bridge_dir)
    binding = transcript.resolve_owned_transcript(bridge_dir)
    assert binding is not None
    assert not transcript.transcript_changed_since_launch(bridge_dir, binding)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(_step(0, "MODEL", "PLANNER_RESPONSE", "new"))
    replacement.replace(transcript_path)
    assert transcript.transcript_changed_since_launch(bridge_dir, binding)
    offset, identity = transcript.initial_tail_state(bridge_dir, CONVERSATION_ID)
    assert (
        transcript.JsonlTail(transcript_path, offset=offset, identity=identity).read()[0][
            "content"
        ]
        == "new"
    )


def test_bridge_registers_scoped_stop_hook(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge with spaces"
    bridge_dir.mkdir()
    path = bridge.write_transcript_stop_hook(bridge_dir, python_executable="/usr/bin/python3")
    hooks = json.loads(path.read_text())
    assert list(hooks) == ["omnigent-transcript-stop"]
    registration = hooks["omnigent-transcript-stop"]["Stop"]
    assert len(registration) == 1
    assert registration[0]["type"] == "command"
    assert shlex.split(registration[0]["command"]) == [
        "/usr/bin/python3",
        "-I",
        "-m",
        "omnigent.harnesses.antigravity_native.stop_hook",
        "--bridge-dir",
        str(bridge_dir),
    ]


def test_extract_user_request_keeps_embedded_closing_tag() -> None:
    content = (
        "<USER_REQUEST>Show the literal </USER_REQUEST> tag in the answer"
        "</USER_REQUEST><ADDITIONAL_METADATA>private</ADDITIONAL_METADATA>"
    )
    assert transcript.extract_user_request(content) == (
        "Show the literal </USER_REQUEST> tag in the answer"
    )
    assert transcript.extract_user_request("preamble<USER_REQUEST>bad</USER_REQUEST>") is None


def test_tui_interrupt_only_sends_escape_for_active_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge.write_tmux_target(tmp_path, socket_path=tmp_path / "tmux.sock", tmux_target="main")
    sent: list[tuple[str, ...]] = []
    pane = ["? for shortcuts"]
    monkeypatch.setattr(bridge, "_session_alive", lambda *_args: True)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: pane[0])
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    assert bridge.turn_is_idle_via_tui(tmp_path) is True
    assert bridge.interrupt_turn_via_tui(tmp_path) is False
    assert sent == []
    pane[0] = "esc to cancel"
    assert bridge.turn_is_idle_via_tui(tmp_path) is False
    assert bridge.interrupt_turn_via_tui(tmp_path) is True
    assert sent == [(str(tmp_path / "tmux.sock"), "send-keys", "-t", "main", "Escape")]


@pytest.mark.parametrize(
    ("reason", "failed", "cancelled"),
    [
        ("ERROR", True, False),
        ("USER_CANCELED", False, True),
        ("error", False, False),
        ("user_canceled", False, False),
        ("USER_CANCELLED", False, False),
    ],
)
def test_stop_hook_recognizes_only_observed_reasons(
    tmp_path: Path, reason: str, failed: bool, cancelled: bool
) -> None:
    assert record_stop_event(
        tmp_path,
        {"conversationId": CONVERSATION_ID, "fullyIdle": True, "terminationReason": reason},
    )
    recorded = json.loads((tmp_path / STOP_EVENTS_FILE).read_text())
    assert recorded["failed"] is failed
    assert recorded["cancelled"] is cancelled


def test_stop_hook_records_only_completion_metadata(tmp_path: Path) -> None:
    payload = {
        "conversationId": CONVERSATION_ID,
        "fullyIdle": True,
        "terminationReason": "NO_TOOL_CALL",
        "executionNum": 0,
        "transcriptPath": "/private/sensitive/transcript_full.jsonl",
    }
    assert record_stop_event(tmp_path, payload)
    recorded = json.loads((tmp_path / STOP_EVENTS_FILE).read_text())
    assert recorded == {
        "conversation_id": CONVERSATION_ID,
        "fully_idle": True,
        "failed": False,
        "cancelled": False,
        "transcript_boundary": None,
    }
    assert record_stop_event(
        tmp_path,
        {
            "conversationId": CONVERSATION_ID,
            "fullyIdle": True,
            "terminationReason": "ERROR",
            "executionNum": {"untrusted": "value"},
        },
    )
    assert json.loads((tmp_path / STOP_EVENTS_FILE).read_text().splitlines()[-1]) == {
        "conversation_id": CONVERSATION_ID,
        "fully_idle": True,
        "failed": True,
        "cancelled": False,
        "transcript_boundary": None,
    }


@pytest.mark.asyncio
async def test_two_transcript_turns_mirror_once_and_close_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(
            0,
            "USER_EXPLICIT",
            "USER_INPUT",
            "<USER_REQUEST>first</USER_REQUEST><ADDITIONAL_METADATA>secret</ADDITIONAL_METADATA>",
        )
        + _step(1, "MODEL", "PLANNER_RESPONSE", "first answer")
    )
    record_stop_event(
        bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True, "executionNum": 0}
    )
    events: list[reader.OutboundEvent] = []

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    ticks = 0

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            with transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _step(2, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>second</USER_REQUEST>")
                    + _step(3, "MODEL", "PLANNER_RESPONSE", "second answer")
                )
            record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    result = await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 5,
        committed_steps_out=None,
    )
    assert result is None
    statuses = [
        event.data.get("status")
        for event in events
        if event.event_type == "external_session_status"
    ]
    assert statuses == ["running", "idle", "running", "idle"]
    messages = [event.data for event in events if event.event_type == "external_conversation_item"]
    assert [item["item_data"]["content"][0]["text"] for item in messages] == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]
    assert [item["item_data"]["role"] for item in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert len({item["source_id"] for item in messages}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
async def test_discovery_rotates_used_binding_before_mirroring_new_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resumed: bool
) -> None:
    bridge_dir = tmp_path / "bridge"
    old_transcript, cache = _session_files(bridge_dir)
    if resumed:
        old_transcript.write_text(
            _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>old user</USER_REQUEST>")
            + _step(1, "MODEL", "PLANNER_RESPONSE", "old answer")
        )
    transcript.prepare_transcript_capture(bridge_dir)
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="old-session", conversation_id=CONVERSATION_ID),
    )
    sessions: dict[str, dict[str, object]] = {
        "old-session": {
            "agent_id": "antigravity",
            "runner_id": "runner-one",
            "labels": {"antigravity_native_bridge_id": "bridge-one"},
            "external_session_id": CONVERSATION_ID if resumed else None,
        }
    }
    messages: list[tuple[str, str]] = []
    transfers: list[dict[str, object]] = []
    finished = asyncio.Event()

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/sessions":
            sessions["new-session"] = body
            return httpx.Response(201, json={"id": "new-session"})
        session_id = request.url.path.split("/")[3]
        if request.method == "GET":
            return httpx.Response(200, json=sessions[session_id])
        if request.method == "PATCH":
            external_id = body.get("external_session_id")
            if external_id and sessions[session_id].get("external_session_id") not in (
                None,
                external_id,
            ):
                return httpx.Response(400)
            sessions[session_id].update(body)
        elif request.url.path.endswith("/transfer"):
            transfers.append(body)
        elif request.url.path.endswith("/events"):
            if body["type"] == "external_conversation_item":
                messages.append((session_id, body["data"]["item_data"]["content"][0]["text"]))
            elif body["type"] == "external_session_status" and body["data"]["status"] == "idle":
                finished.set()
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json={})

    @contextlib.asynccontextmanager
    async def open_client(*_args: object, **_kwargs: object) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handle)
        ) as client:
            yield client

    cleared = False

    async def sleep(_duration: float) -> None:
        nonlocal cleared
        if not cleared:
            cleared = True
            new_transcript = (
                old_transcript.parents[3]
                / OTHER_ID
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl"
            )
            new_transcript.parent.mkdir(parents=True)
            new_transcript.write_text(
                _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>new user</USER_REQUEST>")
                + _step(1, "MODEL", "PLANNER_RESPONSE", "new answer")
            )
            cache.write_text(json.dumps({"/scratch": OTHER_ID}))
            record_stop_event(bridge_dir, {"conversationId": OTHER_ID, "fullyIdle": True})
        await asyncio.sleep(0)

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_client)
    monkeypatch.setattr(reader, "_resolve_rpc_port", lambda _cascade: None)
    monkeypatch.setattr(reader, "_sleep", sleep)
    task = asyncio.create_task(
        reader.run_reader_with_bridge(
            base_url="http://test",
            headers={},
            auth=None,
            session_id="old-session",
            bridge_dir=bridge_dir,
        )
    )
    try:
        await asyncio.wait_for(finished.wait(), timeout=3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    expected_session = "new-session" if resumed else "old-session"
    assert messages == [(expected_session, "new user"), (expected_session, "new answer")]
    assert transfers == ([{"target_session_id": "new-session"}] if resumed else [])
    state = bridge.read_bridge_state(bridge_dir)
    assert state is not None
    assert (state.session_id, state.conversation_id) == (expected_session, OTHER_ID)
    assert sessions[expected_session]["external_session_id"] == OTHER_ID
    if resumed:
        assert sessions["old-session"]["external_session_id"] == CONVERSATION_ID


@pytest.mark.asyncio
async def test_resume_ignores_stop_at_same_file_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>before resume</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "before resume answer")
    )
    transcript.prepare_transcript_capture(bridge_dir)
    baseline_offset, baseline_identity = transcript.initial_tail_state(bridge_dir, CONVERSATION_ID)
    assert baseline_identity is not None
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="session-one", conversation_id=CONVERSATION_ID),
    )
    monkeypatch.setattr(executor_mod, "turn_is_idle_via_tui", lambda _bridge: True)
    monkeypatch.setattr(executor_mod, "wait_for_turn_idle_via_tui", lambda _bridge: True)
    assert await executor_mod.interrupt_bridge_turn(bridge_dir, expected_session_id="session-one")
    stale_stop = json.loads((bridge_dir / STOP_EVENTS_FILE).read_text())
    assert stale_stop["cancelled"] is True
    assert stale_stop["transcript_boundary"] == [*baseline_identity, baseline_offset]
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            _step(2, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>first</USER_REQUEST>")
            + _step(3, "MODEL", "PLANNER_RESPONSE", "first answer")
        )
    events: list[reader.OutboundEvent] = []
    ticks = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            assert [
                event.data["status"]
                for event in events
                if event.event_type == "external_session_status"
            ] == ["running"]
            record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
        if ticks == 2:
            assert [
                event.data["status"]
                for event in events
                if event.event_type == "external_session_status"
            ] == ["running", "idle"]
            with transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _step(
                        4,
                        "USER_EXPLICIT",
                        "USER_INPUT",
                        "<USER_REQUEST>second</USER_REQUEST>",
                    )
                    + _step(5, "MODEL", "PLANNER_RESPONSE", "second answer")
                )
        if ticks == 3:
            assert [
                event.data["status"]
                for event in events
                if event.event_type == "external_session_status"
            ] == ["running", "idle", "running"]
            record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 5,
        committed_steps_out=None,
    )
    assert [
        event.data["status"] for event in events if event.event_type == "external_session_status"
    ] == ["running", "idle", "running", "idle"]
    assert [
        event.data["item_data"]["content"][0]["text"]
        for event in events
        if event.event_type == "external_conversation_item"
    ] == ["first", "first answer", "second", "second answer"]


@pytest.mark.asyncio
async def test_batched_stop_markers_close_each_turn_after_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>first</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "first answer")
    )
    record_stop_event(
        bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True, "executionNum": 0}
    )
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            _step(2, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>second</USER_REQUEST>")
            + _step(3, "MODEL", "PLANNER_RESPONSE", "second answer")
        )
    record_stop_event(
        bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True, "executionNum": 0}
    )
    events: list[reader.OutboundEvent] = []

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", lambda _duration: _noop())
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=_stop_after_polls(4),
        committed_steps_out=None,
    )
    assert [event.event_type for event in events] == [
        "external_conversation_item",
        "external_session_status",
        "external_conversation_item",
        "external_session_status",
        "external_conversation_item",
        "external_session_status",
        "external_conversation_item",
        "external_session_status",
    ]


@pytest.mark.asyncio
async def test_two_user_inputs_inside_one_stop_boundary_are_one_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>start</USER_REQUEST>")
        + _step(1, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>steer</USER_REQUEST>")
        + _step(2, "MODEL", "PLANNER_RESPONSE", "finished")
    )
    record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
    events: list[reader.OutboundEvent] = []

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", lambda _duration: _noop())
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=_stop_after_polls(2),
        committed_steps_out=None,
    )
    statuses = [
        event.data["status"] for event in events if event.event_type == "external_session_status"
    ]
    assert statuses == ["running", "idle"]
    assert [
        event.data["item_data"]["content"][0]["text"]
        for event in events
        if event.event_type == "external_conversation_item"
    ] == ["start", "steer", "finished"]


@pytest.mark.asyncio
async def test_transcript_answer_waits_for_delayed_stop_hook_to_emit_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>question</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "answer")
    )
    events: list[reader.OutboundEvent] = []
    ticks = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 4:
            record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
        if ticks == 3:
            statuses = [
                event.data.get("status")
                for event in events
                if event.event_type == "external_session_status"
            ]
            assert statuses == ["running"]

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 7,
        committed_steps_out=None,
    )
    assert [event.event_type for event in events] == [
        "external_conversation_item",
        "external_session_status",
        "external_conversation_item",
        "external_session_status",
    ]


@pytest.mark.asyncio
async def test_next_user_waits_for_previous_stop_when_transcript_outruns_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>first</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "first answer")
    )
    events: list[reader.OutboundEvent] = []
    ticks = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
            with transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _step(2, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>second</USER_REQUEST>")
                    + _step(3, "MODEL", "PLANNER_RESPONSE", "second answer")
                )
        if ticks == 2:
            record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 5,
        committed_steps_out=None,
    )
    assert [event.event_type for event in events] == [
        "external_conversation_item",  # first user
        "external_session_status",  # running
        "external_conversation_item",  # first answer
        "external_session_status",  # first Stop
        "external_conversation_item",  # second user
        "external_session_status",  # running
        "external_conversation_item",  # second answer
        "external_session_status",  # second Stop
    ]


@pytest.mark.asyncio
async def test_transcript_retries_failed_post_before_advancing_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>hello</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "answer")
    )
    record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
    accepted: list[reader.OutboundEvent] = []
    attempted: list[reader.OutboundEvent] = []

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        attempted.append(event)
        if len(attempted) == 1:
            return False
        accepted.append(event)
        return True

    ticks = 0

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 4,
        committed_steps_out=None,
    )
    assert attempted[0].data["source_id"] == attempted[1].data["source_id"]
    assert [event.event_type for event in accepted] == [
        "external_conversation_item",
        "external_session_status",
        "external_conversation_item",
        "external_session_status",
    ]


@pytest.mark.asyncio
async def test_transcript_delivers_pending_turn_before_clear_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, cache = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>old user</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "old answer")
    )
    record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
    new_transcript = (
        bridge_dir
        / "agy-home"
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / OTHER_ID
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    new_transcript.parent.mkdir(parents=True)
    new_transcript.touch()
    accepted: list[reader.OutboundEvent] = []
    post_attempts = 0
    user_attempts = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        nonlocal post_attempts, user_attempts
        post_attempts += 1
        if event.event_type == "external_conversation_item":
            text = event.data["item_data"]["content"][0]["text"]
            if text == "old user":
                user_attempts += 1
                if user_attempts == 1:
                    cache.write_text(json.dumps({"/scratch": OTHER_ID}))
                if user_attempts < 3:
                    return False
        accepted.append(event)
        return True

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", lambda _duration: _noop())
    committed_turns: list[int] = []
    polls = 0

    def stop() -> bool:
        nonlocal polls
        polls += 1
        return polls > 3

    result = await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=stop,
        committed_steps_out=committed_turns,
    )
    assert result == OTHER_ID
    assert user_attempts == 3
    assert post_attempts == 6
    assert committed_turns == [1]
    assert [
        event.data["item_data"]["content"][0]["text"]
        for event in accepted
        if event.event_type == "external_conversation_item"
    ] == ["old user", "old answer"]


@pytest.mark.asyncio
async def test_failed_stop_exposes_safe_error_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>hello</USER_REQUEST>")
    )
    record_stop_event(
        bridge_dir,
        {
            "conversationId": CONVERSATION_ID,
            "fullyIdle": True,
            "terminationReason": "ERROR",
            "error": "secret detail from native terminal",
        },
    )
    events: list[reader.OutboundEvent] = []
    ticks = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 4,
        committed_steps_out=None,
    )
    failure = events[-1]
    assert failure.data["status"] == "failed"
    assert "native terminal" in failure.data["output"]
    assert "secret detail" not in failure.data["output"]


@pytest.mark.asyncio
async def test_cancelled_stop_keeps_cancelled_terminal_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>cancel me</USER_REQUEST>")
    )
    record_stop_event(
        bridge_dir,
        {
            "conversationId": CONVERSATION_ID,
            "fullyIdle": True,
            "terminationReason": "USER_CANCELED",
        },
    )
    events: list[reader.OutboundEvent] = []

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", lambda _duration: _noop())
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=_stop_after_polls(2),
        committed_steps_out=None,
    )
    status_events = [
        event.data for event in events if event.event_type == "external_session_status"
    ]
    assert status_events == [{"status": "running"}, {"status": "idle", "cancelled": True}]


@pytest.mark.asyncio
async def test_failed_stop_does_not_block_following_successful_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>first</USER_REQUEST>")
    )
    record_stop_event(
        bridge_dir,
        {"conversationId": CONVERSATION_ID, "fullyIdle": True, "terminationReason": "ERROR"},
    )
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            _step(1, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>second</USER_REQUEST>")
            + _step(2, "MODEL", "PLANNER_RESPONSE", "recovered")
        )
    record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
    events: list[reader.OutboundEvent] = []
    ticks = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 4,
        committed_steps_out=None,
    )
    assert [
        event.data["status"] for event in events if event.event_type == "external_session_status"
    ] == ["running", "failed", "running", "idle"]
    assert [
        event.data["item_data"]["content"][0]["text"]
        for event in events
        if event.event_type == "external_conversation_item"
    ] == ["first", "second", "recovered"]


@pytest.mark.asyncio
async def test_confirmed_interrupt_without_native_stop_closes_and_allows_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>cancel me</USER_REQUEST>")
    )
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="session-one", conversation_id=CONVERSATION_ID),
    )
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _cid: None)
    bridge.write_tmux_target(bridge_dir, socket_path=tmp_path / "tmux.sock", tmux_target="main")
    history = (
        "> Explain esc to cancel and ? for shortcuts\n"
        "The answer quotes esc to cancel and ? for shortcuts.\n"
    )
    pane = [history + "esc to cancel\n"]
    sent: list[tuple[str, ...]] = []

    def cancel(*args: str) -> None:
        sent.append(args)
        pane[0] = history + "? for shortcuts\n"

    monkeypatch.setattr(bridge, "_session_alive", lambda *_args: True)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: pane[0])
    monkeypatch.setattr(bridge, "_run_tmux", cancel)
    assert await executor_mod.interrupt_bridge_turn(bridge_dir, expected_session_id="session-one")
    assert sent == [(str(tmp_path / "tmux.sock"), "send-keys", "-t", "main", "Escape")]
    marker = json.loads((bridge_dir / STOP_EVENTS_FILE).read_text())
    assert marker["cancelled"] is True
    assert marker["transcript_boundary"][2] == transcript_path.stat().st_size
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            _step(1, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>next</USER_REQUEST>")
            + _step(2, "MODEL", "PLANNER_RESPONSE", "answer")
        )
    record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
    events: list[reader.OutboundEvent] = []

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", lambda _duration: _noop())
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=_stop_after_polls(3),
        committed_steps_out=None,
    )
    assert [
        event.data["status"] for event in events if event.event_type == "external_session_status"
    ] == ["running", "idle", "running", "idle"]
    status_events = [
        event.data for event in events if event.event_type == "external_session_status"
    ]
    assert status_events[1] == {"status": "idle", "cancelled": True}
    assert [
        event.data["item_data"]["content"][0]["text"]
        for event in events
        if event.event_type == "external_conversation_item"
    ] == ["cancel me", "next", "answer"]


@pytest.mark.asyncio
async def test_nonterminal_record_does_not_hide_later_done_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path, _ = _session_files(bridge_dir)
    transcript_path.write_text(
        _step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>hello</USER_REQUEST>")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "draft", status="RUNNING")
        + _step(1, "MODEL", "PLANNER_RESPONSE", "final")
    )
    record_stop_event(bridge_dir, {"conversationId": CONVERSATION_ID, "fullyIdle": True})
    events: list[reader.OutboundEvent] = []
    ticks = 0

    async def post(_client: object, _session_id: str, event: reader.OutboundEvent) -> bool:
        events.append(event)
        return True

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(reader, "_post_event", post)
    monkeypatch.setattr(reader, "_sleep", sleep)
    await reader._supervise_transcript(
        bridge_dir,
        transcript.TranscriptBinding(CONVERSATION_ID, transcript_path),
        "session-one",
        client=object(),  # type: ignore[arg-type]
        poll_interval_s=0,
        stop=lambda: ticks >= 4,
        committed_steps_out=None,
    )
    assert [
        event.data["item_data"]["content"][0]["text"]
        for event in events
        if event.event_type == "external_conversation_item"
    ] == ["hello", "final"]


@pytest.mark.asyncio
async def test_read_mode_label_is_upserted_for_both_transports() -> None:
    bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        assert request.method == "PATCH"
        assert request.url.path == "/v1/sessions/session-one"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://localhost"
    ) as client:
        await reader._record_read_mode(client, "session-one", transcript_fallback=True)
        await reader._record_read_mode(client, "session-one", transcript_fallback=False)
    assert bodies == [
        {"labels": {"antigravity_native_transcript_fallback": "1"}},
        {"labels": {"antigravity_native_transcript_fallback": "0"}},
    ]


async def _noop() -> None:
    return None


def _stop_after_polls(count: int):
    ticks = 0

    def stop() -> bool:
        nonlocal ticks
        ticks += 1
        return ticks > count

    return stop
