"""Replay order tests for Codex forwarder."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
async def test_shell_reconnect_preserves_a_newer_unresolved_submission(tmp_path: Path) -> None:
    """Old shell history is mirrored without resolving an uncorrelated newer submission."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(tmp_path / "codex-home"),
            pending_shell_command_id="new_submission",
        ),
    )
    native_client = MagicMock()
    native_client.is_subscribed_to_thread.return_value = False
    native_client.request = AsyncMock(
        return_value={
            "result": {
                "thread": {
                    "id": "thread_1",
                    "turns": [
                        {
                            "id": "turn_shell",
                            "status": "completed",
                            "items": [
                                {
                                    "id": "shell_1",
                                    "type": "commandExecution",
                                    "source": "userShell",
                                    "command": "echo recovered",
                                    "status": "completed",
                                    "aggregatedOutput": "recovered\n",
                                    "exitCode": 0,
                                }
                            ],
                        }
                    ],
                }
            }
        }
    )
    client = _RecordingClient()
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        await fwd._subscribe_until_ready(
            native_client,
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            thread_id="thread_1",
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
            elicitation_tracker=tracker,
        )
    finally:
        await tracker.close()

    native_client.request.assert_awaited_once_with("thread/resume", {"threadId": "thread_1"})
    bridge = read_bridge_state(tmp_path)
    assert bridge is not None
    assert bridge.pending_shell_command_id == "new_submission"
    assert bridge.active_turn_id is None
    assert bridge.active_shell_turn_id is None
    assert any(
        body["data"].get("item_data", {}).get("stdout") == "recovered\n"
        for _, body in client.posts
    )


@pytest.mark.parametrize("parent_turn_id", [None, "turn_1"], ids=["standalone", "auxiliary"])
@pytest.mark.asyncio
async def test_running_shell_replay_waits_for_actual_output(
    tmp_path: Path, parent_turn_id: str | None
) -> None:
    """A resume snapshot is still running and must not consume its completion ID."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=parent_turn_id,
            pending_shell_command_id="submission_1",
            pending_shell_parent_turn_id=parent_turn_id,
        ),
    )
    item = {
        "type": "commandExecution",
        "id": "shell_1",
        "source": "userShell",
        "command": "/bin/zsh -lc 'printf shell-output'",
        "status": "inProgress",
        "aggregatedOutput": None,
        "exitCode": None,
    }
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    try:
        await fwd._replay_resume_response(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            response={
                "result": {
                    "thread": {
                        "id": "thread_1",
                        "turns": [{"id": "turn_1", "status": "inProgress", "items": [item]}],
                    }
                }
            },
            usage_coalescer=usage,
            elicitation_tracker=tracker,
            forwarder_state=state,
        )
        terminal_items = [
            body["data"]["item_data"]
            for _, body in client.posts
            if body["type"] == "external_conversation_item"
        ]
        assert terminal_items == [{"kind": "input", "input": "printf shell-output"}]
        bridge = read_bridge_state(tmp_path)
        assert bridge is not None
        assert bridge.pending_shell_command_id == "submission_1"
        assert bridge.active_shell_turn_id is None

        completion = {
            "method": "item/completed",
            "params": {
                "threadId": "thread_1",
                "turnId": "turn_1",
                "item": {
                    **item,
                    "status": "completed",
                    "aggregatedOutput": "shell-output",
                    "exitCode": 0,
                },
            },
        }
        for _ in range(2):
            await fwd._handle_event(
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                event=completion,
                usage_coalescer=usage,
                elicitation_tracker=tracker,
                forwarder_state=state,
            )
    finally:
        await tracker.close()

    terminal_items = [
        body["data"]["item_data"]
        for _, body in client.posts
        if body["type"] == "external_conversation_item"
    ]
    assert terminal_items == [
        {"kind": "input", "input": "printf shell-output"},
        {"kind": "output", "stdout": "shell-output", "stderr": None},
    ]


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
        def is_subscribed_to_thread(self, _thread_id: str) -> bool:
            return True

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
@pytest.mark.parametrize("resume_status", [None, "inProgress"])
async def test_terminal_boundary_waits_for_replay_before_clearing_active_turn(
    tmp_path: Path,
    resume_status: str | None,
) -> None:
    """A blocked replay keeps the crash journal until its item is acknowledged."""

    class _BlockingFirstPostClient(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.status_entered = asyncio.Event()
            self.release_status = asyncio.Event()

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
            if json["type"] == "external_session_status" and json["data"]["status"] == "running":
                self.status_entered.set()
                await self.release_status.wait()
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
                                    "status": resume_status,
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
        if resume_status == "inProgress":
            await asyncio.wait_for(client.status_entered.wait(), timeout=5.0)
            assert not terminal.done()
            assert read_bridge_state(tmp_path).active_turn_id == "turn_1"  # type: ignore[union-attr]
            client.release_status.set()
        await asyncio.gather(replay, terminal)
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    assert read_bridge_state(tmp_path).active_turn_id is None  # type: ignore[union-attr]

    statuses = [
        body["data"]["status"]
        for _url, body in client.posts
        if body["type"] == "external_session_status"
    ]
    assert statuses == (["running", "idle"] if resume_status == "inProgress" else ["idle"])


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


def _live_completion(text: str = "live") -> dict:
    return {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "item": {"type": "agentMessage", "id": "item_live", "text": text},
    }


@pytest.mark.asyncio
async def test_subscribe_parked_on_a_not_ready_thread_does_not_block_live_posts(
    tmp_path: Path,
) -> None:
    """A fresh thread's parked subscription leaves the delivery scope free."""

    class _NotReadyClient:
        def __init__(self) -> None:
            self.calls = 0

        def is_subscribed_to_thread(self, _thread_id: str) -> bool:
            return False

        async def request(self, _method: str, _params: dict) -> dict:
            self.calls += 1
            raise RuntimeError("no rollout found for thread id thread_1")

    native = _NotReadyClient()
    client = _RecordingClient()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    ready_signal = asyncio.Event()
    locks_token = fwd._conversation_item_locks.set({})
    subscribe = asyncio.create_task(
        fwd._subscribe_until_ready(
            native,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            thread_id="thread_1",
            usage_coalescer=usage,
            elicitation_tracker=tracker,
            ready_signal=ready_signal,
        )
    )
    try:
        while native.calls == 0:
            await asyncio.sleep(0)
        await asyncio.wait_for(
            fwd._handle_completed_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                _live_completion(),
                forwarder_state=fwd._CodexForwarderState(),
            ),
            timeout=5.0,
        )
    finally:
        subscribe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscribe
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    assert [body["data"]["source_id"] for _url, body in client.posts] == [
        "thread_1:turn_1:item_live"
    ]


@pytest.mark.asyncio
async def test_unanswered_subscribe_resume_times_out_and_unblocks_live_posts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume Codex never answers is retried, with a growing bound, instead of
    holding the scope forever."""

    class _HangingFirstResumeClient:
        def __init__(self) -> None:
            self.params: list[dict] = []

        def is_subscribed_to_thread(self, _thread_id: str) -> bool:
            return True

        async def request(self, _method: str, params: dict) -> dict:
            self.params.append(params)
            if len(self.params) <= 3:
                await asyncio.Event().wait()
            return {"result": {"thread": {"id": "thread_1", "turns": []}}}

    monkeypatch.setattr(fwd, "_SUBSCRIBE_RESUME_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(fwd, "_SUBSCRIBE_RESUME_MAX_TIMEOUT_SECONDS", 0.04)
    native = _HangingFirstResumeClient()
    client = _RecordingClient()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    locks_token = fwd._conversation_item_locks.set({})
    caplog.set_level(logging.WARNING, logger=fwd.__name__)
    try:
        subscribe = asyncio.create_task(
            fwd._subscribe_until_ready(
                native,  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                thread_id="thread_1",
                usage_coalescer=usage,
                elicitation_tracker=tracker,
            )
        )
        while not native.params:
            await asyncio.sleep(0)
        await asyncio.wait_for(
            fwd._handle_completed_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                _live_completion(),
                forwarder_state=fwd._CodexForwarderState(),
            ),
            timeout=5.0,
        )
        await asyncio.wait_for(subscribe, timeout=5.0)
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    # The retry is still a reconnect, not a fresh not-ready thread.
    assert native.params == [{"threadId": "thread_1", "excludeTurns": True}] * 4
    # Each timed-out attempt doubles the next bound, up to the max.
    assert [
        record.args[1]  # type: ignore[index]
        for record in caplog.records
        if "did not answer" in record.getMessage()
    ] == [0.02, 0.04, 0.04]
    assert [body["data"]["source_id"] for _url, body in client.posts] == [
        "thread_1:turn_1:item_live"
    ]


@pytest.mark.asyncio
async def test_timed_out_replay_resume_retries_with_turns_and_keeps_replay_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reconnect owing replay keeps the delivery scope across a timed-out attempt:
    a newer live turn cannot replace the recovery cursor or post ahead of replay."""

    class _SlowThenAnsweringClient:
        def __init__(self) -> None:
            self.params: list[dict] = []
            self.first_entered = asyncio.Event()
            self.retry_entered = asyncio.Event()
            self.release_retry = asyncio.Event()

        def is_subscribed_to_thread(self, _thread_id: str) -> bool:
            return True

        async def request(self, _method: str, params: dict) -> dict:
            self.params.append(params)
            if len(self.params) == 1:
                self.first_entered.set()
                await asyncio.Event().wait()
            self.retry_entered.set()
            await self.release_retry.wait()
            return {
                "result": {
                    "thread": {
                        "id": "thread_1",
                        "turns": [
                            {
                                "id": "turn_1",
                                "items": [
                                    {"type": "agentMessage", "id": "item_1", "text": "replayed"}
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
    monkeypatch.setattr(fwd, "_SUBSCRIBE_RESUME_TIMEOUT_SECONDS", 0.05)
    native = _SlowThenAnsweringClient()
    client = _RecordingClient()
    tracker = fwd._CodexElicitationTaskTracker()
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    locks_token = fwd._conversation_item_locks.set({})
    try:
        subscribe = asyncio.create_task(
            fwd._subscribe_until_ready(
                native,  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                thread_id="thread_1",
                usage_coalescer=usage,
                elicitation_tracker=tracker,
            )
        )
        await asyncio.wait_for(native.first_entered.wait(), timeout=5.0)
        # Queued during the attempt that times out, so they would run in the gap
        # before the retry if the scope were released.
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
        live = asyncio.create_task(
            fwd._handle_completed_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                {
                    "threadId": "thread_1",
                    "turnId": "turn_2",
                    "item": {"type": "agentMessage", "id": "item_2", "text": "live"},
                },
                forwarder_state=fwd._CodexForwarderState(),
            )
        )
        await asyncio.wait_for(native.retry_entered.wait(), timeout=5.0)
        assert not turn_started.done()
        assert not live.done()
        # A restart now would still replay the interrupted turn.
        assert read_bridge_state(tmp_path).active_turn_id == "turn_1"  # type: ignore[union-attr]

        native.release_retry.set()
        await asyncio.wait_for(asyncio.gather(subscribe, turn_started, live), timeout=5.0)
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        await usage.close()
        await tracker.close()

    assert native.params == [{"threadId": "thread_1"}, {"threadId": "thread_1"}]
    assert [
        body["data"]["source_id"]
        for _url, body in client.posts
        if body["type"] == "external_conversation_item"
    ] == ["thread_1:turn_1:item_1", "thread_1:turn_2:item_2"]
