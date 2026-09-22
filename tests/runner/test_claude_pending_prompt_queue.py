"""Native questions hold follow-up messages outside the harness turn lifetime."""

from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


class _SnapshotHarnessClient(_ScriptedHarnessClient):
    """Capture each turn's input before subsequent history appends."""

    on_dispatch: Callable[[], None] | None = None

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        if self.on_dispatch is not None:
            self.on_dispatch()
        return super().stream(method, url, json=copy.deepcopy(json), timeout=timeout)


def _build_app() -> tuple[FastAPI, _SnapshotHarnessClient]:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    harness = _SnapshotHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, harness


def _message(text: str) -> dict[str, Any]:
    return {"type": "message", "content": [{"type": "input_text", "text": text}]}


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
def pending_prompt(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"pending": True}

    async def skip_terminal_launch(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(runner_app, "_launch_native_terminal", skip_terminal_launch)
    monkeypatch.setattr(
        claude_native_bridge, "has_pending_user_prompt", lambda _: state["pending"]
    )
    monkeypatch.setattr(runner_app, "_CLAUDE_PENDING_PROMPT_POLL_S", 0.01)
    return state


@pytest.mark.asyncio
async def test_question_buffers_messages_and_resumes_fifo(
    monkeypatch: pytest.MonkeyPatch, pending_prompt: dict[str, bool], tmp_path: Path
) -> None:
    """An inactive delivery turn does not permit typing over a pending question."""
    app, harness = _build_app()
    sid = uuid.uuid4().hex
    inspected_dirs: list[Path] = []

    async def bridge_id(**_: Any) -> str:
        return "label-owned-bridge"

    def inspect(bridge_dir: Path) -> bool:
        inspected_dirs.append(bridge_dir)
        return pending_prompt["pending"]

    monkeypatch.setattr(runner_app, "_claude_native_bridge_id_for_session", bridge_id)
    monkeypatch.setattr(claude_native_bridge, "bridge_dir_for_bridge_id", lambda b: tmp_path / b)
    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", inspect)
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
        )
        assert created.status_code == 201
        first = await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        waiter = app.state.claude_prompt_waiters[sid]
        second = await client.post(f"/v1/sessions/{sid}/events", json=_message("second"))
        assert first.status_code == second.status_code == 202
        assert first.json()["status"] == second.json()["status"] == "buffered"
        assert app.state.claude_prompt_waiters[sid] is waiter
        assert sid not in app.state.active_turns
        assert app.state.has_active_work()
        await asyncio.sleep(0.04)
        assert harness.posted_bodies == []
        assert len(app.state.session_message_buffers[sid]) == 2

        pending_prompt["pending"] = False
        await _until(
            lambda: (
                len(harness.posted_bodies) == 2
                and sid not in app.state.active_turns
                and sid not in app.state.claude_prompt_waiters
            )
        )

    assert not app.state.session_message_buffers.get(sid)
    assert set(inspected_dirs) == {tmp_path / "label-owned-bridge"}
    first_body, second_body = harness.posted_bodies
    assert first_body["content"][-1]["content"][0]["text"] == "first"
    assert second_body["content"][-1]["content"][0]["text"] == "second"


@pytest.mark.asyncio
async def test_each_buffered_drain_checks_for_new_question(
    pending_prompt: dict[str, bool],
) -> None:
    """A question appearing after the first queued delivery protects the second."""
    app, harness = _build_app()
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex})
        app.state.active_turns[sid] = None
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await client.post(f"/v1/sessions/{sid}/events", json=_message("second"))
        app.state.active_turns.pop(sid)
        await app.state.check_and_start_next_turn(sid)
        assert harness.posted_bodies == []
        assert len(app.state.session_message_buffers[sid]) == 2

        def ask_again() -> None:
            pending_prompt["pending"] = True

        harness.on_dispatch = ask_again
        pending_prompt["pending"] = False
        await _until(lambda: len(harness.posted_bodies) == 1 and sid not in app.state.active_turns)
        await asyncio.sleep(0.04)
        assert len(harness.posted_bodies) == 1
        assert len(app.state.session_message_buffers[sid]) == 1

        harness.on_dispatch = None
        pending_prompt["pending"] = False
        await _until(
            lambda: (
                len(harness.posted_bodies) == 2
                and sid not in app.state.active_turns
                and sid not in app.state.claude_prompt_waiters
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["interrupt", "stop_session", "delete"])
async def test_control_cancels_question_waiter_and_queued_input(
    action: str, monkeypatch: pytest.MonkeyPatch, pending_prompt: dict[str, bool]
) -> None:
    """Explicit controls remain immediate and cannot restart a discarded message."""
    app, harness = _build_app()
    sid = uuid.uuid4().hex
    controls: list[str] = []
    monkeypatch.setattr(
        claude_native_bridge, "inject_interrupt", lambda *_a, **_k: controls.append("interrupt")
    )
    monkeypatch.setattr(
        claude_native_bridge, "kill_session", lambda *_a, **_k: controls.append("stop_session")
    )
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex})
        await client.post(f"/v1/sessions/{sid}/events", json=_message("do not deliver"))
        waiter = app.state.claude_prompt_waiters[sid]
        if action == "delete":
            response = await client.delete(f"/v1/sessions/{sid}")
        else:
            response = await client.post(f"/v1/sessions/{sid}/events", json={"type": action})
        assert response.status_code == (200 if action == "delete" else 204)
        if action != "delete":
            assert controls == [action]
        pending_prompt["pending"] = False
        await _until(waiter.done)
        await asyncio.sleep(0.04)
        assert sid not in app.state.claude_prompt_waiters
        assert sid not in app.state.session_message_buffers
        assert harness.posted_bodies == []


@pytest.mark.asyncio
@pytest.mark.parametrize("from_waiter", [False, True])
async def test_stop_during_drain_probe_does_not_restart_session(
    from_waiter: bool, monkeypatch: pytest.MonkeyPatch, pending_prompt: dict[str, bool]
) -> None:
    """Stopping during the final pane read discards the in-flight queue snapshot."""
    app, harness = _build_app()
    sid = uuid.uuid4().hex
    probe_started = threading.Event()
    release_probe = threading.Event()
    calls = 0

    def inspect(_: Path) -> bool:
        nonlocal calls
        calls += 1
        if from_waiter and calls == 1:
            return True
        if calls == (3 if from_waiter else 1):
            probe_started.set()
            assert release_probe.wait(5)
        return False

    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", inspect)
    monkeypatch.setattr(claude_native_bridge, "kill_session", lambda *_a, **_k: None)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex})
        if not from_waiter:
            app.state.active_turns[sid] = None
        await client.post(f"/v1/sessions/{sid}/events", json=_message("do not restart"))
        drain = None
        if not from_waiter:
            app.state.active_turns.pop(sid)
            drain = asyncio.create_task(app.state.check_and_start_next_turn(sid))
        try:
            await _until(probe_started.is_set)
            response = await client.post(
                f"/v1/sessions/{sid}/events", json={"type": "stop_session"}
            )
            assert response.status_code == 204
        finally:
            release_probe.set()
        if drain is not None:
            await drain
        await asyncio.sleep(0.04)
        assert sid not in app.state.claude_prompt_waiters
        assert sid not in app.state.session_message_buffers
        assert harness.posted_bodies == []
