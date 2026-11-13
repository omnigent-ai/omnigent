"""Mcp startup tests for Codex forwarder."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
    _write_session_config,
)

# ── MCP startup status mirroring (issue #2058) ─────────────────────────


def _mcp_startup_event(
    name: str,
    status: str,
    *,
    error: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """
    Build a Codex ``mcpServer/startupStatus/updated`` envelope.

    :param name: MCP server name, e.g. ``"safe"``.
    :param status: Startup state, e.g. ``"starting"``.
    :param error: Optional failure detail.
    :param thread_id: Optional ``threadId`` param.
    :returns: Notification envelope dict.
    """
    params: dict = {"name": name, "status": status}
    if error is not None:
        params["error"] = error
    if thread_id is not None:
        params["threadId"] = thread_id
    return {"method": "mcpServer/startupStatus/updated", "params": params}


@pytest.mark.asyncio
async def test_mcp_startup_event_records_and_posts(tmp_path: Path) -> None:
    """
    An MCP startup notification updates the bridge map and mirrors it to AP.

    The bridge write is what unblocks the executor's first-turn gate; the
    ``external_mcp_startup`` post is what makes the web session show
    per-server startup state instead of appearing hung.
    """
    client = _RecordingClient()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event=_mcp_startup_event("safe", "starting", thread_id="thread_1"),
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    from omnigent.harnesses.codex_native.bridge import read_mcp_startup

    assert read_mcp_startup(tmp_path) == {"safe": {"status": "starting", "error": None}}
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_mcp_startup",
                "data": {"servers": {"safe": {"status": "starting", "error": None}}},
            },
        )
    ]


@pytest.mark.asyncio
async def test_mcp_startup_event_carries_failure_error(tmp_path: Path) -> None:
    """
    A ``failed`` update retains the error detail through bridge and post.

    The error text is what the web UI (and the executor's timeout text)
    surface for a server that never came up.
    """
    client = _RecordingClient()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event=_mcp_startup_event("safe", "failed", error="handshake failed"),
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    from omnigent.harnesses.codex_native.bridge import read_mcp_startup

    assert read_mcp_startup(tmp_path) == {
        "safe": {"status": "failed", "error": "handshake failed"}
    }
    assert client.posts[0][1]["data"]["servers"]["safe"]["error"] == "handshake failed"


@pytest.mark.asyncio
async def test_mcp_startup_event_for_other_thread_is_ignored(tmp_path: Path) -> None:
    """
    An MCP startup update naming a different thread is dropped.

    A child thread's (or stale thread's) startup must not overwrite the
    parent bridge map or flash the parent session's startup band.
    """
    client = _RecordingClient()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event=_mcp_startup_event("safe", "starting", thread_id="thread_other"),
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    from omnigent.harnesses.codex_native.bridge import read_mcp_startup

    assert read_mcp_startup(tmp_path) == {}
    assert client.posts == []


@pytest.mark.asyncio
async def test_mcp_startup_event_with_unknown_status_is_ignored(tmp_path: Path) -> None:
    """
    An unknown status value neither records nor posts.

    Guards against a future Codex enum change feeding a bogus status into
    the executor gate or the web UI.
    """
    client = _RecordingClient()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event=_mcp_startup_event("safe", "exploded"),
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    from omnigent.harnesses.codex_native.bridge import read_mcp_startup

    assert read_mcp_startup(tmp_path) == {}
    assert client.posts == []


@pytest.mark.asyncio
async def test_seed_mcp_startup_round_posts_config_servers(tmp_path: Path) -> None:
    """
    Seeding records the enabled config servers as ``starting`` and posts them.

    Codex delivers per-server startup edges only to the thread-owning
    connection, so this synthesized round is the only way the web session
    learns startup is in flight (issue #2058). Disabled servers must be
    excluded — codex does not boot them, and a permanently-"starting"
    band entry would never resolve.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup

    client = _RecordingClient()
    _write_session_config(
        tmp_path,
        '[mcp_servers.safe]\ncommand = "x"\n'
        '[mcp_servers.off]\ncommand = "y"\nenabled = false\n'
        '[mcp_servers.omnigent]\ncommand = "z"\n',
    )

    timer = await fwd._seed_mcp_startup_round(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
    )

    try:
        assert read_mcp_startup(tmp_path) == {
            "safe": {"status": "starting", "error": None},
            "omnigent": {"status": "starting", "error": None},
        }
        assert client.posts == [
            (
                "/v1/sessions/conv_x/events",
                {
                    "type": "external_mcp_startup",
                    "data": {
                        "servers": {
                            "safe": {"status": "starting", "error": None},
                            "omnigent": {"status": "starting", "error": None},
                        }
                    },
                },
            )
        ]
        assert timer is not None
    finally:
        if timer is not None:
            timer.cancel()


@pytest.mark.asyncio
async def test_seed_mcp_startup_round_skips_when_state_exists(tmp_path: Path) -> None:
    """
    A bridge dir that already carries round state is not reseeded.

    ``supervise_forwarder`` restarts on reconnect mid-session; reseeding
    then would flash a false "starting" band for servers that finished
    booting long ago. Only ``clear_bridge_state`` (each app-server
    launch) resets the map.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    _write_session_config(tmp_path, '[mcp_servers.safe]\ncommand = "x"\n')
    update_mcp_server_startup(tmp_path, "safe", "cancelled")

    timer = await fwd._seed_mcp_startup_round(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
    )

    assert timer is None
    assert client.posts == []
    assert read_mcp_startup(tmp_path) == {"safe": {"status": "cancelled", "error": None}}


@pytest.mark.asyncio
async def test_seed_rearms_settle_timer_when_round_still_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A reconnect that finds the round still pending re-arms the settle window.

    The previous forwarder's settle timer dies with its connection; if the
    reconnect only skipped reseeding, a missed idle edge would leave the
    band stuck on "starting" for the rest of the session. The re-armed
    timer must actually resolve the round: once it fires, the pending
    entries are dropped and the settled map is posted.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    update_mcp_server_startup(tmp_path, "safe", "starting")
    update_mcp_server_startup(tmp_path, "storage-console", "cancelled")

    async def _no_sleep(_seconds: float) -> None:
        """Collapse the settle window so the test observes the timer firing."""

    monkeypatch.setattr(fwd, "_sleep", _no_sleep)

    timer = await fwd._seed_mcp_startup_round(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
    )

    # No reseed/repost on reconnect — the recorded map is left intact...
    assert timer is not None
    assert client.posts == []
    assert read_mcp_startup(tmp_path)["safe"]["status"] == "starting"

    # ...but the re-armed timer settles the round when the window elapses.
    await timer
    assert read_mcp_startup(tmp_path) == {
        "storage-console": {"status": "cancelled", "error": None}
    }
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_mcp_startup",
                "data": {"servers": {"storage-console": {"status": "cancelled", "error": None}}},
            },
        )
    ]


@pytest.mark.asyncio
async def test_thread_idle_settles_synthesized_round(tmp_path: Path) -> None:
    """
    An idle ``thread/status/changed`` resolves the synthesized round.

    Codex defers turn execution until MCP startup settles, so a thread
    going idle after a turn proves the round ended. Unresolved
    ``starting`` entries are dropped (their real outcomes are only
    delivered to the thread owner) while locally-recorded ``cancelled``
    states survive, and the settled map is posted so the band clears.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    update_mcp_server_startup(tmp_path, "safe", "starting")
    update_mcp_server_startup(tmp_path, "storage-console", "cancelled")

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "thread/status/changed",
            "params": {"threadId": "thread_1", "status": {"type": "idle"}},
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    assert read_mcp_startup(tmp_path) == {
        "storage-console": {"status": "cancelled", "error": None}
    }
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_mcp_startup",
                "data": {"servers": {"storage-console": {"status": "cancelled", "error": None}}},
            },
        )
    ]


@pytest.mark.asyncio
async def test_thread_active_status_does_not_settle_round(tmp_path: Path) -> None:
    """
    An ``active`` status edge must not settle the round.

    Codex flips the thread active the moment a turn is ACCEPTED — which
    happens mid-startup, before the round ends — so settling there would
    clear the band exactly when it matters most.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    update_mcp_server_startup(tmp_path, "safe", "starting")

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "thread/status/changed",
            "params": {"threadId": "thread_1", "status": {"type": "active", "activeFlags": []}},
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    assert read_mcp_startup(tmp_path) == {"safe": {"status": "starting", "error": None}}
    assert client.posts == []


@pytest.mark.asyncio
async def test_model_output_item_settles_synthesized_round(tmp_path: Path) -> None:
    """
    The first model-produced item resolves the synthesized round mid-turn.

    Codex defers turn execution until MCP startup settles, so an
    assistant-side item proves the round is over while the turn is still
    running. Without this signal, a server stuck ``starting`` (e.g. a
    misconfigured command that never handshakes) pins the "Starting MCP
    servers" band under a visibly working agent until the turn ends or
    the config-derived window elapses.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    update_mcp_server_startup(tmp_path, "safe", "starting")

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "item/started",
            "params": {
                "threadId": "thread_1",
                "item": {"id": "item_1", "type": "agentMessage", "text": ""},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    assert read_mcp_startup(tmp_path) == {}
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_mcp_startup", "data": {"servers": {}}},
        )
    ]


@pytest.mark.asyncio
async def test_model_output_settles_the_round_only_once(tmp_path: Path) -> None:
    """
    Only the first model item pays the settle path; later items skip it.

    The round is seeded once per forwarder connection (never on thread
    rotation), so once model output settles it the outcome cannot change
    — every later item in the session would otherwise re-read the bridge
    file for nothing. The state flag short-circuits them, which this
    pins by re-populating the map behind the flag: a second item must
    leave it untouched.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    update_mcp_server_startup(tmp_path, "safe", "starting")

    item_event = {
        "method": "item/completed",
        "params": {
            "threadId": "thread_1",
            "item": {"id": "item_1", "type": "agentMessage", "text": "hi"},
        },
    }

    async def deliver() -> None:
        """Feed the model-output item through the forwarder dispatch."""
        await fwd._handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            event=item_event,
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            expected_thread_id="thread_1",
            forwarder_state=state,
        )

    await deliver()
    assert state.mcp_startup_settled is True
    settle_posts = [p for p in client.posts if p[1]["type"] == "external_mcp_startup"]
    assert len(settle_posts) == 1

    # Re-populate the map behind the flag: without the short-circuit the
    # next item would read it, settle again, and post a second time.
    update_mcp_server_startup(tmp_path, "safe", "starting")
    await deliver()

    assert read_mcp_startup(tmp_path) == {"safe": {"status": "starting", "error": None}}
    assert len([p for p in client.posts if p[1]["type"] == "external_mcp_startup"]) == 1


@pytest.mark.asyncio
async def test_user_message_item_does_not_settle_round(tmp_path: Path) -> None:
    """
    The turn's ``userMessage`` item must not settle the round.

    Like the thread-active edge, the user message materializes when a
    turn is merely ACCEPTED — which happens mid-startup — so settling on
    it would clear the band during the genuine pre-turn wait.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    update_mcp_server_startup(tmp_path, "safe", "starting")

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "item/started",
            "params": {
                "threadId": "thread_1",
                "item": {"id": "item_0", "type": "userMessage", "text": "hi"},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    assert read_mcp_startup(tmp_path) == {"safe": {"status": "starting", "error": None}}
    assert client.posts == []


@pytest.mark.asyncio
async def test_other_thread_item_does_not_settle_round(tmp_path: Path) -> None:
    """
    An item on an unrecognized thread must not settle the parent round.

    MCP startup is bridge-level state surfaced on the parent session;
    another thread's activity proves nothing about this round.
    """
    from omnigent.harnesses.codex_native.bridge import read_mcp_startup, update_mcp_server_startup

    client = _RecordingClient()
    update_mcp_server_startup(tmp_path, "safe", "starting")

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "item/started",
            "params": {
                "threadId": "thread_other",
                "item": {"id": "item_1", "type": "agentMessage", "text": ""},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_1",
    )

    assert read_mcp_startup(tmp_path) == {"safe": {"status": "starting", "error": None}}
    assert client.posts == []


def test_settle_timeout_tracks_slowest_configured_server(tmp_path: Path) -> None:
    """
    The settle window follows the slowest ``startup_timeout_sec`` in config.

    Codex bounds each server's spawn+handshake by that budget, so the
    round cannot outlive it; a config without budgets falls back to the
    codex default, and an absurd budget is capped.
    """
    _write_session_config(
        tmp_path,
        '[mcp_servers.fast]\ncommand = "x"\n'
        '[mcp_servers.slow]\ncommand = "y"\nstartup_timeout_sec = 120\n',
    )
    assert fwd._mcp_startup_settle_timeout_seconds(tmp_path) == 135.0

    _write_session_config(tmp_path, '[mcp_servers.fast]\ncommand = "x"\n')
    assert fwd._mcp_startup_settle_timeout_seconds(tmp_path) == 25.0

    _write_session_config(
        tmp_path,
        '[mcp_servers.slow]\ncommand = "y"\nstartup_timeout_sec = 100000\n',
    )
    assert fwd._mcp_startup_settle_timeout_seconds(tmp_path) == 240.0

    # A disabled server's budget must not stretch the window: codex never
    # boots it, and the seed excludes it from the synthesized round.
    _write_session_config(
        tmp_path,
        '[mcp_servers.fast]\ncommand = "x"\n'
        '[mcp_servers.off]\ncommand = "y"\nenabled = false\nstartup_timeout_sec = 120\n',
    )
    assert fwd._mcp_startup_settle_timeout_seconds(tmp_path) == 25.0
