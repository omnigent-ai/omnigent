"""A Claude Code fork sub-agent must register under its parent session.

A fork inherits the parent's conversation, so its transcript also carries the
parent's spawning ``Agent`` call as a sidechain record. Treating that copy as a
second owner left the fork unregistered and made every poll re-read the parent
and sub-agent transcripts in full for the rest of the session.

Drives the reported journey with the real ``claude`` CLI in a runner-bound
claude-native session; skips when the CLI is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native import forwarder
from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _WORKING

_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'

# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# CLI boot + the scripted turn + the fork writing its meta file.
_FORK_SPAWN_TIMEOUT_S = 240.0
# The forwarder polls every 0.25 s; a registered fork reaches the rail in seconds.
_REGISTRATION_TIMEOUT_MS = 30_000
_DEFERRAL_LOG = "Deferring claude-native sub-agent with no resolved parent"
_REREAD_TICKS = 3
# Claude Code's tool_result for a background fork spawn.
_FORK_LAUNCHED_RESULT = "Async agent launched successfully"


def _claude_projects_dir() -> Path:
    """Resolve Claude Code's ``projects`` root the way the CLI does."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return root / "projects"


def _wait_for_external_session_id(base_url: str, session_id: str, deadline: float) -> str:
    """Poll the session until the runner records Claude's own session id."""
    while time.monotonic() < deadline:
        snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        external = snap.get("external_session_id")
        if external:
            return str(external)
        time.sleep(1.0)
    raise AssertionError(
        f"session {session_id} never captured an external_session_id; Claude Code's "
        "first turn did not complete, so no fork could have been spawned"
    )


def _fork_metas(subagents_dir: Path) -> list[Path]:
    """Return the ``agent-*.meta.json`` files describing fork sub-agents."""
    metas: list[Path] = []
    for meta_path in sorted(subagents_dir.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and (meta.get("agentType") == "fork" or meta.get("isFork")):
            metas.append(meta_path)
    return metas


def _child_sessions(base_url: str, session_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data if isinstance(data, list) else []


def _correlation_reads_per_tick(
    *,
    transcript_path: Path,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    ticks: int,
) -> list[int]:
    """Run the runner's sub-agent poll over the on-disk tree; return bytes re-parsed per tick.

    Posts go to an accepting mock server so nothing reaches the live parent.
    Only the transcript reads made to correlate spawn ids are counted.
    """
    reads: list[int] = []
    original = forwarder._tool_use_ids_in_transcript

    def counting(path: Path, **kwargs: Any) -> set[str]:
        reads.append(path.stat().st_size)
        return original(path, **kwargs)

    monkeypatch.setattr(forwarder, "_tool_use_ids_in_transcript", counting)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            # A registered sub-agent's transcript is mirrored as event batches.
            return httpx.Response(202, json=[{"item_id": f"item-{i}"} for i in range(len(body))])
        if body.get("type") == "external_subagent_start":
            child = f"conv_{body['data']['subagent_id']}"
            return httpx.Response(202, json={"queued": False, "child_session_id": child})
        return httpx.Response(202, json={})

    async def run() -> list[int]:
        trackers = tuple(forwarder._PostRetryTracker(base_delay_s=0.0) for _ in range(3))
        state = forwarder.SubagentForwardState(subagents={})
        per_tick: list[int] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            for _ in range(ticks):
                before = len(reads)
                state = await forwarder._forward_available_subagents(
                    client=client,
                    parent_session_id="conv_parent",
                    bridge_dir=bridge_dir,
                    transcript_path=transcript_path,
                    state=state,
                    agent_name="claude-native-ui",
                    start_retry_tracker=trackers[0],
                    item_retry_tracker=trackers[1],
                    status_retry_tracker=trackers[2],
                )
                per_tick.append(sum(reads[before:]))
        return per_tick

    # The e2e_ui process keeps an asyncio loop on the main thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run()).result(timeout=120.0)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_claude_fork_subagent_registers_under_parent(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fork Claude Code spawns shows up under its parent in the Agents rail."""
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is required for the fork sub-agent e2e")
    base_url, session_id = native_claude_mock_session

    reset_mock_llm(mock_llm_server_url)
    go_token = f"fork-go-{uuid.uuid4().hex[:6]}"
    worker_token = f"fork-worker-{uuid.uuid4().hex[:8]}"
    result_token = f"fork-result-{uuid.uuid4().hex[:8]}"
    fork_args = json.dumps(
        {
            "description": "Fork worker",
            "prompt": f"Reply with exactly {worker_token} and finish.",
            "subagent_type": "fork",
        }
    )
    # The composer message (matched by go_token) spawns one fork; the extra
    # copy absorbs Claude Code's title request, which quotes the message.
    configure_mock_llm(
        mock_llm_server_url,
        [{"tool_calls": [{"name": "Agent", "arguments": fork_args}]}] * 2,
        key="fork-spawn",
        match=go_token,
    )
    # Longer tokens outrank go_token: the fork's request carries its prompt, and
    # the parent's follow-ups carry the launch tool_result or the fork's reply.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"fork reply {result_token}"}] * 3,
        key="fork-worker",
        match=worker_token,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "fork launched"}] * 3,
        key="fork-launched",
        match=_FORK_LAUNCHED_RESULT,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "fork result received"}] * 3,
        key="fork-done",
        match=result_token,
    )
    set_fallback_mock_llm(mock_llm_server_url, "default", "ok")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "ok")

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    terminal_segment = page.get_by_test_id("view-mode-terminal")
    expect(terminal_segment).to_be_enabled(timeout=30_000)
    terminal_segment.click()
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    chat_segment = page.get_by_test_id("view-mode-chat")
    expect(chat_segment).to_be_enabled(timeout=30_000)
    chat_segment.click()

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"spawn the fork worker now {go_token}")
    page.get_by_role("button", name="Send", exact=True).click()

    # Premise: Claude Code really spawned a fork (its meta file names the type).
    deadline = time.monotonic() + _FORK_SPAWN_TIMEOUT_S
    external_session_id = _wait_for_external_session_id(base_url, session_id, deadline)
    projects_dir = _claude_projects_dir()
    transcript_path: Path | None = None
    fork_metas: list[Path] = []
    while time.monotonic() < deadline:
        transcript_path = next(projects_dir.glob(f"*/{external_session_id}.jsonl"), None)
        if transcript_path is not None:
            subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
            fork_metas = _fork_metas(subagents_dir)
            if fork_metas:
                break
        page.wait_for_timeout(1_000)
    if not fork_metas:
        with contextlib.suppress(Exception):
            items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=15.0).json()
            print(f"--- session items at spawn-timeout ---\n{json.dumps(items)[-4000:]}\n---")
    assert transcript_path is not None and fork_metas, (
        f"Claude Code wrote no fork .meta.json under {projects_dir} for Claude session "
        f"{external_session_id}; the journey did not reach the state the registration "
        "assertion needs."
    )
    print(f"fork meta files: {[m.name for m in fork_metas]}")
    for meta_path in fork_metas:
        print(f"{meta_path.name}: {meta_path.read_text(encoding='utf-8')}")

    # Let the turn settle so the runner's forwarder has polled the fork's meta
    # file many times before the rail is inspected.
    expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)
    page.wait_for_timeout(5_000)

    # Re-read evidence: the same poll over the unchanged on-disk tree.
    with caplog.at_level(logging.DEBUG, logger=forwarder.__name__):
        reread_per_tick = _correlation_reads_per_tick(
            transcript_path=transcript_path,
            bridge_dir=tmp_path / "bridge",
            monkeypatch=monkeypatch,
            ticks=_REREAD_TICKS,
        )
    deferrals = [r.getMessage() for r in caplog.records if _DEFERRAL_LOG in r.getMessage()]
    print(f"correlation bytes re-parsed per tick: {reread_per_tick}")
    print(f"deferral log lines: {len(deferrals)}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator(_SUBAGENT_ROW)
    try:
        expect(rows).to_have_count(len(fork_metas), timeout=_REGISTRATION_TIMEOUT_MS)
    except AssertionError as exc:
        children = _child_sessions(base_url, session_id)
        raise AssertionError(
            f"Claude Code spawned {len(fork_metas)} fork sub-agent(s) "
            f"({[m.name for m in fork_metas]}) but the parent shows {rows.count()} "
            f"sub-agent row(s) and {len(children)} child session(s): the fork was never "
            "registered. Re-running the forwarder's sub-agent poll over the unchanged "
            f"transcripts re-parsed {reread_per_tick} bytes on {_REREAD_TICKS} consecutive "
            f"ticks and logged {len(deferrals)} '{_DEFERRAL_LOG}' line(s) at DEBUG."
        ) from exc

    # A registered fork leaves no spawn id to correlate on a repeat poll.
    assert reread_per_tick[1:] == [0] * (_REREAD_TICKS - 1), (
        f"forwarder re-parsed transcripts on repeat polls over unchanged files: {reread_per_tick}"
    )
