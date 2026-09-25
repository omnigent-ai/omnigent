"""Harness turn-end edges, as the harnesses now send them, end the runner's holds.

Each test drives the real runner app (``create_runner_app``), its events route,
its status book, its native-turn hold (``app.state.has_active_work``) and the
real pane reaper. Only tmux, the pane watcher thread and the Omnigent server
client are faked (``tests/terminals/native_pane_rig.py``).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native import forwarder as claude_native_forwarder
from omnigent.native._native_post_delivery import post_external_session_status
from omnigent.runner import app as runner_app
from omnigent.runner.session_status import StatusSource
from omnigent.server.routes._sessions.orchestration import (
    _enrich_terminal_status_with_subagent_output,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _runner_client, _spec_resolver_returning
from tests.terminals.native_pane_rig import PaneRig, build_pane_rig

_STOP_FAILURE_TEXT = "API Error: 500 Overloaded"


def _native_spec(harness: str) -> AgentSpec:
    """An agent spec whose executor runs *harness*, e.g. ``"pi-native"``."""
    return AgentSpec(
        spec_version=1,
        name="native-turn-end-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


async def _create_session(client: httpx.AsyncClient, conv_id: str) -> None:
    created = await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag"})
    assert created.status_code == 201, created.text


async def _relay(client: httpx.AsyncClient, conv_id: str, data: dict[str, Any]) -> None:
    """POST what the server forwards for a forwarder's ``external_session_status``."""
    resp = await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={"type": "external_session_status", "data": data},
    )
    assert resp.status_code == 204, resp.text


async def _reap_if_idle(rig: PaneRig) -> None:
    """One reaper scan of a pane that has been silent for two idle windows."""
    rig.reaper._last_busy_at[rig.conv_id] = time.monotonic() - 2 * 3600.0
    await rig.reaper._scan_once()


async def _stop_failure_relay_data(bridge_dir: Path, conv_id: str) -> dict[str, Any]:
    """The ``data`` the server forwards for a claude-native ``StopFailure``.

    Built by the production pieces: the bridge's hook record, the forwarder's
    ``failure_detail``, the wire body, and the server's enrichment of it.
    """
    claude_native_bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "0d5c8f3e-7a51-4c7b-9d62-3f1e2a9b8c10",
            "error": "server_error",
            "last_assistant_message": _STOP_FAILURE_TEXT,
        },
    )
    record = claude_native_bridge.read_hook_events_since_with_position(bridge_dir, 0).records[-1]
    posted: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(_capture)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        await post_external_session_status(
            client,
            session_id=conv_id,
            status="failed",
            failure_detail=claude_native_forwarder._stop_failure_detail(record),
        )
    (body,) = posted
    assert body["type"] == "external_session_status"
    # A harness-reported reason wins before the store is read.
    return await _enrich_terminal_status_with_subagent_output(
        body["data"],
        "failed",
        conv_id,
        None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["relay_first", "file_first"])
async def test_a_claude_stop_failure_with_its_reason_ends_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    """A ``StopFailure`` edge that carries its own error text still ends the turn.

    Claude's status file has no failure literal: it returns to ``idle`` after a
    turn error as after a success. The relayed ``failed`` is the edge that says
    the turn failed, and it now carries ``failure_detail`` and the server's
    ``output``. The runner records it as a relayed edge whatever rides with
    it, in either order against the file's ``idle``: the runner hold lets go
    and the silent pane is reaped.

    :param order: Whether the relayed ``failed`` lands before the file's
        ``idle`` or after it.
    """
    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key="claude",
        spec_resolver=await _spec_resolver_returning(_native_spec("claude-native")),
    )
    app, conv_id = rig.app, rig.conv_id
    # A hook log of its own, under the (patched) claude bridge root.
    data = await _stop_failure_relay_data(tmp_path / "claude-bridge" / "hooks", conv_id)
    assert data["failure_detail"] == _STOP_FAILURE_TEXT
    assert data["output"] == _STOP_FAILURE_TEXT
    try:
        async with _runner_client(app) as client:
            await _create_session(client, conv_id)
            # The turn starts: Claude writes ``busy`` and the real poller reads it.
            rig.write_claude_status("busy")
            await rig.fire("on_tick")
            record = rig.book.current(conv_id)
            assert record is not None and record.status == "running"
            assert record.origin is StatusSource.STATUS_FILE
            assert app.state.has_active_work() is True

            if order == "file_first":
                rig.write_claude_status("idle")
                await rig.fire("on_tick")
                assert app.state.has_active_work() is False
            await _relay(client, conv_id, data)
            record = rig.book.current(conv_id)
            assert record is not None and record.status == "failed"
            assert record.origin is StatusSource.RELAY
            assert app.state.has_active_work() is False
            if order == "relay_first":
                rig.write_claude_status("idle")
                await rig.fire("on_tick")
                assert app.state.has_active_work() is False

            assert await rig.is_busy() is False
            await _reap_if_idle(rig)
            assert not rig.alive()
    finally:
        rig.drain()


@pytest.mark.asyncio
async def test_a_pi_child_holds_its_parent_until_its_agent_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pi child's own ``agent_end`` idle, not its startup, releases its parent.

    Pi's startup no longer reports ``idle`` (readiness is not turn
    completion), so the child's turn edges are ``running`` at ``agent_start``
    and ``idle`` at ``agent_end``. Until that idle, the child's work keeps the
    parent's silent pane (the children hold) and the runner (the child's
    recorded ``running``), and the parent is not woken. The idle completes the
    child and wakes the parent; its result then waits in the parent's inbox for
    the turn the wake starts, so the parent's pane stays held.
    """
    spec_resolver = await _spec_resolver_returning(_native_spec("pi-native"))
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", spec_resolver=spec_resolver)
    app, parent = rig.app, rig.conv_id
    child = f"{parent}_child"
    wake_url = f"/v1/sessions/{parent}/events"
    wake_answer = asyncio.Event()
    wakes: list[str] = []
    base_post = rig.server.post

    async def _post(url: str, **kwargs: Any) -> Any:
        if url != wake_url:
            return await base_post(url, **kwargs)
        wakes.append(url)
        await wake_answer.wait()
        return httpx.Response(200, request=httpx.Request("POST", f"http://server{wake_url}"))

    monkeypatch.setattr(rig.server, "post", _post)
    try:
        async with _runner_client(app) as client:
            await _create_session(client, parent)
            await _create_session(client, child)
            runner_app.register_child_session(
                child, parent_session_id=parent, title="pi:worker", tool="pi", session_name="w"
            )
            entry = runner_app.register_subagent_work(
                parent_session_id=parent, child_session_id=child, agent="pi-native", title="w"
            )
            assert entry.status == "launching"
            assert await rig.is_busy() is True

            # agent_start: the child's queued prompt is running.
            await _relay(client, child, {"status": "running", "response_id": "pi-1-1"})
            assert entry.status == "running"
            assert app.state.has_active_work() is True
            # The parent's pane is silent for two idle windows; the child holds it.
            await _reap_if_idle(rig)
            assert rig.alive()
            assert wakes == []

            # agent_end: the only idle the child reports.
            await _relay(client, child, {"status": "idle", "response_id": "pi-1-1"})
            assert entry.status == "completed"
            assert app.state.has_active_work() is False
            for _ in range(200):
                if wakes:
                    break
                await asyncio.sleep(0.01)
            assert wakes == [wake_url]
            await _reap_if_idle(rig)
            assert rig.alive()
            # Accepted or not, the wake is owed until the parent takes its result.
            wake_answer.set()
            for _ in range(3):
                await asyncio.sleep(0)
            await _reap_if_idle(rig)
            assert rig.alive()
            assert wakes == [wake_url]
    finally:
        wake_answer.set()
        runner_app.unregister_subagent_work(child)
        runner_app.unregister_child_session(child)
        runner_app._session_event_queues_ref.pop(child, None)
        rig.drain()
