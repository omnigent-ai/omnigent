"""A transcript pause must not produce a false sub-agent completion notice.

Drive the real forwarder/server/runner/UI chain using Claude-shaped transcript
files. A child with an intermediate message and no completion record stays
unfinished after the five-second idle observation. The parent must not receive
a "finished (completed)" inbox notification.

Run: pytest tests/e2e_ui/agents/test_subagent_inactivity.py
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.forwarder import (
    _forward_available_subagents,
    _PostRetryTracker,
    _read_subagent_forward_state,
)
from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import open_right_rail, set_fallback_mock_llm

# Claude-side sub-agent identity, shaped like the real meta files
# (``agent-<hex>.meta.json`` + ``toolu_…`` spawn tool-use id).
_SUBAGENT_ID = "afe4a11b2c3d4e5f6"
_TOOL_USE_ID = "toolu_01IdleFalseDone"
_SUBAGENT_TYPE = "general-purpose"
_SUBAGENT_DESCRIPTION = "long-running background research task"

# The sub-agent's FIRST intermediate message — mid-task progress, not a
# result. On a buggy build this is the only text the child transcript holds
# when the false "completed" notice fires, and (per the ticket) the enriched
# payload the false inbox entry presents as the sub-agent's "result".
_INTERMEDIATE_TEXT = (
    "Starting on the research task: first I'll clone the repository and "
    "survey the code layout. This will take a while."
)

# Markers for the runner's sub-agent wake notice (see
# ``omnigent.runner.subagent_work._format_subagent_wake_notice``).
_FALSE_NOTICE_MARKER = "finished (completed)"
_NOTICE_PREFIX = "[System: sub-agent"

# Inactivity threshold in the forwarder is 5 s; tick well past it.
_LULL_TICKS = 8
_TICK_INTERVAL_S = 1.0
# The runner hands a retained completion to the parent while processing the
# nudge and fires its wake POST without awaiting it; give that POST time to land.
_NUDGE_REPLY_TIMEOUT_S = 90.0
_WAKE_LANDING_S = 15.0
_NUDGE_TEXT = "Still working — any update from the background sub-agent?"


def _write_claude_task_spawn_on_disk(root: Path) -> Path:
    """Lay out the on-disk shape Claude Code produces for a Task spawn.

    Creates the parent transcript JSONL (carrying the spawning ``Agent``
    tool-use record) and the sibling ``<stem>/subagents/`` directory with the
    sub-agent's ``agent-<id>.meta.json`` + ``agent-<id>.jsonl``. The
    sub-agent transcript holds its kickoff user record and ONE intermediate
    assistant message, then goes quiet — the mid-task lull (a long tool call
    still executing, no done record).

    :param root: Temp directory to build the tree under.
    :returns: The parent transcript path (the forwarder's watch root).
    """
    transcript_path = root / "project" / "parent-session.jsonl"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    spawn_record = {
        "isSidechain": False,
        "type": "assistant",
        "uuid": f"spawn-{_SUBAGENT_ID}",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": _TOOL_USE_ID,
                    "name": "Agent",
                    "input": {"description": _SUBAGENT_DESCRIPTION},
                }
            ],
        },
    }
    transcript_path.write_text(json.dumps(spawn_record) + "\n", encoding="utf-8")

    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / f"agent-{_SUBAGENT_ID}.meta.json").write_text(
        json.dumps(
            {
                "agentType": _SUBAGENT_TYPE,
                "description": _SUBAGENT_DESCRIPTION,
                "toolUseId": _TOOL_USE_ID,
            }
        ),
        encoding="utf-8",
    )
    subagent_records = [
        {
            "isSidechain": True,
            "type": "user",
            "uuid": "sa-user-1",
            "message": {"role": "user", "content": "go"},
        },
        {
            "isSidechain": True,
            "type": "assistant",
            "uuid": "sa-assistant-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _INTERMEDIATE_TEXT}],
            },
        },
    ]
    (subagents_dir / f"agent-{_SUBAGENT_ID}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in subagent_records) + "\n",
        encoding="utf-8",
    )
    return transcript_path


def _run_forwarder_tick(
    *,
    base_url: str,
    parent_session_id: str,
    bridge_dir: Path,
    transcript_path: Path,
    state: Any,
    trackers: tuple[_PostRetryTracker, _PostRetryTracker, _PostRetryTracker],
) -> Any:
    """Run one real forwarder sub-agent tick against the live server.

    Exactly the loop body ``forward_claude_transcript_to_session`` runs on the
    host machine in production: discovery of new ``agent-*.meta.json`` files,
    ``external_subagent_start`` registration, transcript item mirroring, and
    the ``subagent.status`` idle observation.

    :param base_url: Spawned Omnigent server base URL.
    :param parent_session_id: Parent conversation id.
    :param bridge_dir: Bridge dir for the forwarder's cursor files.
    :param transcript_path: Parent transcript JSONL path.
    :param state: Current ``SubagentForwardState``.
    :param trackers: ``(start, item, status)`` retry trackers, carried
        across ticks like the production loop does.
    :returns: The updated ``SubagentForwardState``.
    """
    start_tracker, item_tracker, status_tracker = trackers

    async def _tick() -> Any:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            return await _forward_available_subagents(
                client=client,
                parent_session_id=parent_session_id,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native",
                start_retry_tracker=start_tracker,
                item_retry_tracker=item_tracker,
                status_retry_tracker=status_tracker,
            )

    # The e2e_ui process keeps an asyncio loop running on the main thread,
    # so the tick gets its own loop on a worker thread — same isolation the
    # production forwarder has in its own process.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _tick()).result(timeout=60.0)


def _session_items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Return a session's persisted items from the live server.

    :param base_url: Spawned Omnigent server base URL.
    :param session_id: Conversation id to read.
    :returns: The raw item dicts, in transcript order.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items if isinstance(items, list) else []


def _item_text(item: dict[str, Any]) -> str:
    """Flatten one persisted item to searchable text.

    :param item: A raw session item dict.
    :returns: All string content found in the item, joined.
    """
    data = item.get("data") or {}
    parts: list[str] = []
    for source in (item, data):
        for key in ("content", "output", "arguments", "text"):
            value = source.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                for block in value:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
    return "\n".join(parts)


def _find_false_notice_text(texts: list[str]) -> str | None:
    """Return the false completion notice if any text carries one.

    :param texts: Searchable texts, e.g. flattened items or model requests.
    :returns: The notice, or ``None`` when no completion notice exists.
    """
    for text in texts:
        start = text.find(_NOTICE_PREFIX)
        if start != -1 and _FALSE_NOTICE_MARKER in text[start:]:
            end = text.find("]", start)
            return text[start : end + 1] if end != -1 else text[start:]
    return None


def _mock_llm_requests(mock_url: str) -> list[str]:
    """Return every request the mock LLM has captured, serialized for searching.

    :param mock_url: Mock LLM server base URL.
    :returns: One JSON string per captured request, oldest first.
    """
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=15.0)
    resp.raise_for_status()
    return [json.dumps(request) for request in resp.json()["requests"]]


def _parent_false_notice(
    base_url: str, session_id: str, mock_url: str, requests_before: int
) -> str | None:
    """Return a false completion notice the parent was handed, if any.

    The Claude-labelled parent persists injected messages only once Claude
    records them, so the notice is looked for in the model requests too.

    :param base_url: Spawned Omnigent server base URL.
    :param session_id: Parent conversation id.
    :param mock_url: Mock LLM server base URL.
    :param requests_before: Captured requests to skip, from before the lull.
    :returns: The notice text, or ``None`` when the parent never got one.
    """
    item_texts = [_item_text(item) for item in _session_items(base_url, session_id)]
    request_texts = _mock_llm_requests(mock_url)[requests_before:]
    return _find_false_notice_text(item_texts + request_texts)


def _assistant_reply_count(items: list[dict[str, Any]]) -> int:
    """Count the assistant messages in a session's items.

    :param items: The session's items.
    :returns: How many items are assistant messages.
    """
    return sum(
        1
        for item in items
        if item.get("type") == "message" and (item.get("data") or {}).get("role") == "assistant"
    )


def _nudge_and_find_false_notice(
    page: Page, base_url: str, session_id: str, mock_url: str, requests_before: int
) -> str | None:
    """Send the parent one follow-up turn and return a false completion notice if one lands.

    :param page: Page open on the parent session.
    :param base_url: Spawned Omnigent server base URL.
    :param session_id: Parent conversation id.
    :param mock_url: Mock LLM server base URL.
    :param requests_before: Captured requests to skip, from before the lull.
    :returns: The notice text, or ``None`` when none arrived after the nudge.
    """
    replies_before = _assistant_reply_count(_session_items(base_url, session_id))
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.fill(_NUDGE_TEXT)
    composer.press("Enter")

    def _nudge_processed() -> bool:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0).json()
        replied = _assistant_reply_count(snapshot.get("items") or []) > replies_before
        return (snapshot.get("status") == "idle" and replied) or _parent_false_notice(
            base_url, session_id, mock_url, requests_before
        ) is not None

    poll_until(_nudge_processed, timeout_s=_NUDGE_REPLY_TIMEOUT_S)
    landing_deadline = time.monotonic() + _WAKE_LANDING_S
    while True:
        notice = _parent_false_notice(base_url, session_id, mock_url, requests_before)
        if notice is not None or time.monotonic() >= landing_deadline:
            return notice
        time.sleep(_TICK_INTERVAL_S)


def test_midtask_lull_must_not_deliver_false_completion(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A still-running sub-agent's transcript lull must not complete it.

    Drives the real chain — forwarder inactivity tick → server relay →
    runner promotion → parent inbox/wake — over a Task sub-agent whose
    transcript has produced one intermediate message and then gone quiet (no
    done record). On a buggy build the lull is promoted to a terminal
    ``completed`` delivery and the false ``[System: sub-agent … finished
    (completed)]`` notice lands in the parent session while the sub-agent is
    still running, so this test FAILS there. Any fix that stops promoting the
    inactivity heuristic to a terminal completion makes it pass.
    """
    base_url, session_id = seeded_session

    # The parent is a mock-LLM agent; give every turn a canned reply so the
    # ordinary init turn and the "still working" nudges complete cleanly.
    set_fallback_mock_llm(
        mock_llm_server_url,
        "gpt-4o-mini",
        "Working on it — the background sub-agent is still running.",
    )

    # 1. One ordinary turn through the real composer, so the runner
    # initializes the parent session (creating its inbox) exactly as a live
    # parent has before any sub-agent finishes.
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill("Start the long-running background research task.")
    composer.press("Enter")
    expect(
        page.get_by_text("background sub-agent is still running", exact=False).first
    ).to_be_visible(timeout=90_000)

    # 2. Stamp the parent as a Claude Code session, the way ``omnigent
    # claude`` marks a real one, so the sub-agent events are accepted for it.
    patched = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
        timeout=10.0,
    )
    assert patched.status_code == 200, patched.text

    # 3. Claude Code spawns the Task sub-agent: the on-disk transcript tree
    # appears, holding the spawn record, the sub-agent's kickoff, and its
    # first intermediate message — then nothing (the mid-task lull).
    transcript_path = _write_claude_task_spawn_on_disk(tmp_path)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    # 4. The real forwarder runs its sub-agent ticks over the lull, exactly
    # as the production loop does on the host. Sanity: the sub-agent must be
    # registered and its intermediate message mirrored, or the lull below
    # would be vacuous.
    trackers = (_PostRetryTracker(), _PostRetryTracker(), _PostRetryTracker())
    state = _read_subagent_forward_state(bridge_dir)
    state = _run_forwarder_tick(
        base_url=base_url,
        parent_session_id=session_id,
        bridge_dir=bridge_dir,
        transcript_path=transcript_path,
        state=state,
        trackers=trackers,
    )
    entry = state.subagents.get(_SUBAGENT_ID)
    assert entry is not None and entry.child_conversation_id, (
        f"forwarder did not register the on-disk sub-agent; state={state!r}"
    )
    child_id = entry.child_conversation_id
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        child_items = _session_items(base_url, child_id)
        if any(_INTERMEDIATE_TEXT in _item_text(item) for item in child_items):
            break
        time.sleep(0.5)
    else:
        raise AssertionError(
            f"sub-agent's intermediate message never mirrored to child {child_id!r}"
        )

    # Show the sub-agent in the rail so the recording carries the live
    # "sub-agent exists and is running" state alongside the chat.
    open_right_rail(page)

    # 5. No new transcript records or completion record: the child is still
    # in a long tool call. Tick past five seconds to publish its idle observation.
    requests_before = len(_mock_llm_requests(mock_llm_server_url))
    for _ in range(_LULL_TICKS):
        state = _run_forwarder_tick(
            base_url=base_url,
            parent_session_id=session_id,
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            trackers=trackers,
        )
        time.sleep(_TICK_INTERVAL_S)
    idle_entry = state.subagents.get(_SUBAGENT_ID)
    assert idle_entry is not None and idle_entry.last_status == "idle", (
        "forwarder did not report idle over the lull; "
        f"last_status={idle_entry.last_status if idle_entry else None!r}"
    )

    # 6. Keep the parent working, exactly as the reported orchestrators were:
    # drive a follow-up turn through the composer. The runner hands over any
    # retained sub-agent completion and fires its wake while processing the
    # parent, so once the nudge is answered a false notice is already on its way.
    notice = _nudge_and_find_false_notice(
        page, base_url, session_id, mock_llm_server_url, requests_before
    )

    if notice is not None:
        # Reproduced. Let the false notice render on the open session page so
        # the failure footage ends on the user-visible outcome, then fail
        # with the live evidence. The promised "result" does not exist: the
        # child transcript still holds only its first intermediate message.
        with __import__("contextlib").suppress(AssertionError):
            expect(page.get_by_text(_FALSE_NOTICE_MARKER, exact=False).first).to_be_visible(
                timeout=45_000
            )
            page.wait_for_timeout(3_000)
        child_texts = [_item_text(item) for item in _session_items(base_url, child_id)]
        has_only_intermediate = any(_INTERMEDIATE_TEXT in text for text in child_texts)
        raise AssertionError(
            "A bare mid-task transcript lull (sub-agent still running, no "
            "done record) was promoted to a terminal 'completed' delivery: "
            f"the parent received the false notice {notice!r} while the child "
            f"sub-agent {child_id!r} was still mid-task "
            f"(its transcript holds only the intermediate message="
            f"{has_only_intermediate}, no completion). This is the false "
            "'sub-agent finished (completed)' notification bug."
        )

    # Passed: the lull produced no terminal completion. Guard against a fix
    # that silently suppressed the child registration instead of the false
    # terminal: the child conversation must still exist with its mirrored
    # intermediate message.
    child_items = _session_items(base_url, child_id)
    assert any(_INTERMEDIATE_TEXT in _item_text(item) for item in child_items), (
        "sub-agent child conversation lost its mirrored transcript items"
    )
