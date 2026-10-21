"""Replay order tests for Codex forwarder."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.asyncio
async def test_resume_replay_keeps_tool_pair_ahead_of_live_completion() -> None:
    """Replay holds one delivery scope so live items cannot split a tool pair."""

    class _BlockingFirstPostClient(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            if not self.posts:
                self.entered.set()
                await self.release.wait()
            return await super().post(url, json=json, timeout=timeout)

    client = _BlockingFirstPostClient()
    state = fwd._CodexForwarderState()
    tracker = fwd._CodexElicitationTaskTracker()
    locks_token = fwd._conversation_item_locks.set({})
    try:
        replay = asyncio.create_task(
            fwd._replay_resume_response(
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=Path(),
                response={
                    "result": {
                        "thread": {
                            "id": "thread_1",
                            "turns": [
                                {
                                    "id": "turn_1",
                                    "items": [
                                        {
                                            "type": "commandExecution",
                                            "id": "call_1",
                                            "command": "pwd",
                                            "aggregatedOutput": "/repo\n",
                                            "exitCode": 0,
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                },
                usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
                elicitation_tracker=tracker,
                forwarder_state=state,
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=5.0)
        live = asyncio.create_task(
            fwd._handle_completed_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                {
                    "threadId": "thread_1",
                    "turnId": "turn_2",
                    "item": {"type": "agentMessage", "id": "item_2", "text": "new reply"},
                },
                forwarder_state=state,
            )
        )
        await asyncio.sleep(0)
        assert not live.done()

        client.release.set()
        await asyncio.gather(replay, live)
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await tracker.close()

    assert [body["data"]["source_id"] for _url, body in client.posts] == [
        "thread_1:turn_1:call_1:call",
        "thread_1:turn_1:call_1:output",
        "thread_1:turn_2:item_2",
    ]


@pytest.mark.asyncio
async def test_subscribe_reserves_delivery_order_before_post_resume_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live completion cannot overtake replay while resume metadata sync waits."""

    class _ResumeClient:
        async def request(self, _method: str, _params: dict) -> dict:
            return {
                "result": {
                    "thread": {
                        "id": "thread_1",
                        "turns": [
                            {
                                "id": "turn_1",
                                "items": [
                                    {
                                        "type": "agentMessage",
                                        "id": "item_1",
                                        "text": "replayed",
                                    }
                                ],
                            }
                        ],
                    }
                }
            }

    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_1",
        ),
    )
    sync_entered = asyncio.Event()
    release_sync = asyncio.Event()

    async def blocking_model_sync(*_args: object, **_kwargs: object) -> None:
        sync_entered.set()
        await release_sync.wait()

    monkeypatch.setattr(fwd, "_sync_model_change", blocking_model_sync)
    monkeypatch.setattr(fwd, "_sync_codex_approval_mode_change", AsyncMock())
    monkeypatch.setattr(fwd, "_refresh_model_from_config", MagicMock())
    monkeypatch.setattr(fwd, "_refresh_developer_instructions_from_config", MagicMock())

    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    locks_token = fwd._conversation_item_locks.set({})
    try:
        subscribe = asyncio.create_task(
            fwd._subscribe_until_ready(
                _ResumeClient(),  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                thread_id="thread_1",
                usage_coalescer=usage,
                elicitation_tracker=tracker,
                forwarder_state=state,
            )
        )
        await asyncio.wait_for(sync_entered.wait(), timeout=5.0)
        live = asyncio.create_task(
            fwd._handle_completed_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                {
                    "threadId": "thread_1",
                    "turnId": "turn_2",
                    "item": {"type": "agentMessage", "id": "item_2", "text": "live"},
                },
                forwarder_state=state,
            )
        )
        await asyncio.sleep(0)
        assert not live.done()
        assert client.posts == []

        release_sync.set()
        await asyncio.gather(subscribe, live)
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    assert [body["data"]["source_id"] for _url, body in client.posts] == [
        "thread_1:turn_1:item_1",
        "thread_1:turn_2:item_2",
    ]


@pytest.mark.asyncio
async def test_terminal_boundary_waits_for_replay_before_clearing_active_turn(
    tmp_path: Path,
) -> None:
    """A blocked replay keeps the crash journal until its item is acknowledged."""

    class _BlockingFirstPostClient(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            if not self.posts:
                self.entered.set()
                await self.release.wait()
            return await super().post(url, json=json, timeout=timeout)

    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_1",
        ),
    )
    client = _BlockingFirstPostClient()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    locks_token = fwd._conversation_item_locks.set({})
    try:
        replay = asyncio.create_task(
            fwd._replay_resume_response(
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                response={
                    "result": {
                        "thread": {
                            "id": "thread_1",
                            "turns": [
                                {
                                    "id": "turn_1",
                                    "items": [
                                        {
                                            "type": "agentMessage",
                                            "id": "item_1",
                                            "text": "replayed",
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                },
                usage_coalescer=usage,
                elicitation_tracker=tracker,
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=5.0)
        terminal = asyncio.create_task(
            fwd._handle_terminal_turn_boundary(
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                method="turn/completed",
                params={"turn": {"id": "turn_1", "status": "completed", "items": []}},
                usage_coalescer=usage,
                delta_coalescer=None,
                elicitation_tracker=tracker,
                codex_client=None,
                forwarder_state=None,
            )
        )
        await asyncio.sleep(0)
        assert not terminal.done()
        assert read_bridge_state(tmp_path).active_turn_id == "turn_1"  # type: ignore[union-attr]

        client.release.set()
        await asyncio.gather(replay, terminal)
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    assert read_bridge_state(tmp_path).active_turn_id is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_turn_started_waits_for_replay_before_replacing_active_turn(tmp_path: Path) -> None:
    """A newer turn cannot overwrite the interrupted-turn crash journal."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_1",
        ),
    )
    client = _RecordingClient()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    lock_entered = asyncio.Event()
    release_lock = asyncio.Event()

    async def hold_replay_scope() -> None:
        async with fwd._conversation_item_delivery_scope("conv_x"):
            lock_entered.set()
            await release_lock.wait()

    locks_token = fwd._conversation_item_locks.set({})
    try:
        replay = asyncio.create_task(hold_replay_scope())
        await asyncio.wait_for(lock_entered.wait(), timeout=5.0)
        turn_started = asyncio.create_task(
            fwd._maybe_handle_turn_event(
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                method="turn/started",
                params={"turn": {"id": "turn_2"}},
                usage_coalescer=usage,
                delta_coalescer=None,
                elicitation_tracker=tracker,
                codex_client=None,
                forwarder_state=None,
            )
        )
        await asyncio.sleep(0)
        assert not turn_started.done()
        assert read_bridge_state(tmp_path).active_turn_id == "turn_1"  # type: ignore[union-attr]

        release_lock.set()
        assert await turn_started is True
        await replay
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    assert read_bridge_state(tmp_path).active_turn_id == "turn_2"  # type: ignore[union-attr]
