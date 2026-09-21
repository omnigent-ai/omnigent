"""Readiness must belong to one live session binding and tunnel generation."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from omnigent.server.routes._sessions.common import _RelayHandle
from omnigent.server.session_readiness import SessionReadinessObserver


class Rig:
    def __init__(self, *, timeout: float = 1) -> None:
        self.connection = object()
        self.registry = Mock()
        self.registry.get.side_effect = lambda _: self.connection
        self.conversation = SimpleNamespace(id="session_ready", runner_id="runner_ready")
        self.store = Mock()
        self.store.get_conversation.side_effect = lambda _: self.conversation
        self.relay_task = asyncio.create_task(asyncio.Event().wait())
        self.relay = _RelayHandle(
            runner_id="runner_ready",
            task=self.relay_task,
            ready=asyncio.Event(),
            connection=self.connection,
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.payload = {"initialized": True, "input_ready": True, "harness": "test"}
        self.calls = 0
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.respond), base_url="http://runner"
        )
        self.observer = SessionReadinessObserver(
            self.registry,
            self.store,
            lambda _: self.relay,
            timeout=timeout,
            poll_interval=0.001,
        )

    async def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/session_ready/readiness"
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return httpx.Response(200, json=self.payload)

    def start(self) -> asyncio.Task[None]:
        self.observer.initialized(self.conversation, self.client, self.connection)  # type: ignore[arg-type]
        return self.observer._observations["session_ready"].task

    async def close(self) -> None:
        await self.observer.shutdown()
        self.relay_task.cancel()
        await asyncio.gather(self.relay_task, return_exceptions=True)
        await self.client.aclose()


def events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.event_name for r in caplog.records if hasattr(r, "event_name")]


@pytest.mark.asyncio
async def test_waits_for_relay_and_input_and_emits_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    rig = Rig()
    try:
        task = rig.start()
        await asyncio.sleep(0)
        assert rig.calls == 0
        rig.relay.ready.set()
        await asyncio.wait_for(rig.entered.wait(), 1)
        assert "session_runner_ready" not in events(caplog)
        rig.release.set()
        await task
        assert rig.start() is task
        assert rig.calls == 1
        assert events(caplog).count("session_runner_ready") == 1
        row = next(
            r for r in caplog.records if getattr(r, "event_name", None) == "session_runner_ready"
        )
        assert row.session_id == "session_ready"
        assert row.attributes["runner_id"] == "runner_ready"
    finally:
        await rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["reconnect", "rebind", "relay_drop", "relay_replace", "delete"])
async def test_late_probe_cannot_mark_stale_state_ready(
    race: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig = Rig(timeout=0.03)
    try:
        rig.relay.ready.set()
        task = rig.start()
        await rig.entered.wait()
        if race == "reconnect":
            rig.connection = object()
        elif race == "rebind":
            rig.conversation.runner_id = "replacement"
        elif race == "delete":
            rig.conversation = None
        elif race == "relay_drop":
            rig.relay.ready.clear()
        else:
            rig.relay = _RelayHandle(
                "runner_ready", rig.relay_task, asyncio.Event(), rig.connection
            )
        rig.release.set()
        await task
        assert "session_runner_ready" not in events(caplog)
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_reconnect_requires_new_relay_then_can_succeed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    rig = Rig()
    try:
        rig.relay.ready.set()
        old = rig.start()
        await rig.entered.wait()
        rig.connection = object()
        new = rig.start()
        assert new is not old
        rig.release.set()
        await asyncio.sleep(0)
        assert "session_runner_ready" not in events(caplog)
        rig.relay = _RelayHandle("runner_ready", rig.relay_task, asyncio.Event(), rig.connection)
        rig.relay.ready.set()
        await new
        assert events(caplog).count("session_runner_ready") == 1
    finally:
        await rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized,input_ready", [(False, True), (True, False)])
async def test_incomplete_runner_response_times_out(
    initialized: bool,
    input_ready: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    rig = Rig(timeout=0.025)
    try:
        rig.payload.update(initialized=initialized, input_ready=input_ready)
        rig.relay.ready.set()
        rig.release.set()
        await rig.start()
        assert "session_runner_ready" not in events(caplog)
        assert "session_readiness_timeout" in events(caplog)
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_unsupported_is_explicit_not_success(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    rig = Rig()
    try:
        rig.payload["supported"] = False
        rig.relay.ready.set()
        rig.release.set()
        await rig.start()
        assert events(caplog) == ["session_readiness_unavailable"]
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_disconnect_cancels_observation() -> None:
    rig = Rig()
    try:
        task = rig.start()
        rig.observer.invalidate_runner("runner_ready")
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert rig.observer._observations.get("session_ready") is None
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_disabled_observer_starts_no_work() -> None:
    rig = Rig()
    try:
        rig.observer._enabled = lambda: False
        rig.observer.initialized(rig.conversation, rig.client, rig.connection)  # type: ignore[arg-type]
        assert not rig.observer._tasks
        assert rig.calls == 0
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_workspace_scopes_do_not_share_readiness() -> None:
    from omnigent.db.db_models import workspace_scope

    rig = Rig()
    try:
        with workspace_scope(101):
            first = rig.start()
        with workspace_scope(202):
            second = rig.start()
            assert second is not first
            rig.observer.invalidate_runner("runner_ready")
        await asyncio.gather(second, return_exceptions=True)
        assert second.cancelled()
        assert not first.cancelled()
    finally:
        await rig.close()
