import asyncio
from collections.abc import AsyncIterator

import httpx
import respx
from omnigent_slack.omnigent import (
    AuthRequiredError,
    HostUnavailableError,
    OmnigentClient,
    OmnigentClientPool,
    OmnigentError,
    RunnerUnavailableError,
    ServerUnreachableError,
    extract_assistant_text,
    extract_elicitation_request,
    extract_output_file,
    extract_policy_denied,
    extract_todos,
    is_terminal_event,
    iter_sse_events,
)


def test_is_terminal_event_only_ends_on_session_idle_or_failed() -> None:
    # Per-response completions are NOT terminal: an orchestrator emits one each
    # time it ends a turn to wait on a sub-agent, then resumes the same turn.
    assert not is_terminal_event({"type": "response.completed"})
    assert not is_terminal_event({"type": "turn.completed"})
    assert not is_terminal_event({"type": "response.output_text.delta", "delta": "x"})
    assert not is_terminal_event({"type": "session.status", "status": "running"})
    assert not is_terminal_event({"type": "session.status", "status": "waiting"})

    # The session settling is the authoritative turn boundary.
    assert is_terminal_event({"type": "session.status", "status": "idle"})
    assert is_terminal_event({"type": "session.status", "status": "failed"})

    # Explicit failure/cancel still ends the turn as a fallback.
    assert is_terminal_event({"type": "response.failed"})
    assert is_terminal_event({"type": "turn.cancelled"})


async def _lines(values: list[str]) -> AsyncIterator[str]:
    for value in values:
        yield value


async def test_iter_sse_events_parses_json_and_done() -> None:
    events = [
        event
        async for event in iter_sse_events(
            _lines(
                [
                    "event: response.output_text.delta",
                    'data: {"delta":"hel"}',
                    "",
                    'data: {"type":"response.output_text.delta","delta":"lo"}',
                    "",
                    "data: [DONE]",
                    "",
                ]
            )
        )
    ]

    assert events == [
        {"type": "response.output_text.delta", "delta": "hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
    ]


def test_extract_assistant_text_from_stream_item() -> None:
    assert (
        extract_assistant_text(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            }
        )
        == "done"
    )


@respx.mock
async def test_client_create_and_submit_request_shapes() -> None:
    create = respx.post("http://omnigent.test/v1/sessions").mock(
        return_value=httpx.Response(201, json={"id": "conv_1"})
    )
    submit = respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        session_id = await client.create_session("ag_1", "Slack C/1")
        await client.submit_message(session_id, "hello")
    finally:
        await client.aclose()

    assert session_id == "conv_1"
    assert create.calls.last.request.read() == b'{"agent_id":"ag_1","title":"Slack C/1"}'
    assert submit.calls.last.request.read() == (
        b'{"type":"message","data":{"role":"user","content":[{"type":"input_text",'
        b'"text":"hello"}]}}'
    )


@respx.mock
async def test_check_health_probes_health_endpoint() -> None:
    health = respx.get("http://omnigent.test/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        await client.check_health()
    finally:
        await client.aclose()

    assert health.calls.call_count == 1
    assert health.calls.last.request.url.path == "/health"


@respx.mock
async def test_validate_returns_agents_and_online_hosts() -> None:
    respx.get("http://omnigent.test/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get("http://omnigent.test/v1/agents").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "ag_1", "name": "Helper"}]})
    )
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "hosts": [
                    {"host_id": "h_on", "name": "Online", "status": "online"},
                    {"host_id": "h_off", "name": "Offline", "status": "offline"},
                ]
            },
        )
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        validated = await client.validate()
    finally:
        await client.aclose()

    assert [a["id"] for a in validated.agents] == ["ag_1"]
    assert [h["host_id"] for h in validated.online_hosts] == ["h_on"]


@respx.mock
async def test_validate_raises_auth_required_on_401() -> None:
    respx.get("http://omnigent.test/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get("http://omnigent.test/v1/agents").mock(return_value=httpx.Response(401))
    client = OmnigentClient("http://omnigent.test")

    try:
        raised = False
        try:
            await client.validate()
        except AuthRequiredError:
            raised = True
    finally:
        await client.aclose()

    assert raised


@respx.mock
async def test_get_host_home_derives_home_from_filesystem_listing() -> None:
    respx.get("http://omnigent.test/v1/hosts/host_1/filesystem").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"name": ".bashrc", "path": "/home/alice/.bashrc", "type": "file"},
                    {"name": "projects", "path": "/home/alice/projects", "type": "directory"},
                ],
            },
        )
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        home = await client.get_host_home("host_1")
    finally:
        await client.aclose()

    assert home == "/home/alice"


@respx.mock
async def test_get_host_home_returns_none_when_listing_empty() -> None:
    respx.get("http://omnigent.test/v1/hosts/host_1/filesystem").mock(
        return_value=httpx.Response(200, json={"object": "list", "data": []})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        home = await client.get_host_home("host_1")
    finally:
        await client.aclose()

    assert home is None


async def test_client_pool_reuses_client_per_server() -> None:
    pool = OmnigentClientPool()
    try:
        first = await pool.get("http://omnigent.test/")
        again = await pool.get("http://omnigent.test")
        other = await pool.get("http://other.test")
    finally:
        await pool.aclose_all()

    assert first is again
    assert first is not other


@respx.mock
async def test_launch_runner_on_explicit_host() -> None:
    launch = respx.post("http://omnigent.test/v1/hosts/host_1/runners").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_launched"})
    )
    respx.get("http://omnigent.test/v1/runners/runner_launched/status").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_launched", "online": True})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        runner_id = await client.launch_runner(
            "conv_1", workspace="/tmp/workspace", host_id="host_1"
        )
    finally:
        await client.aclose()

    assert runner_id == "runner_launched"
    assert launch.calls.last.request.read() == (
        b'{"session_id":"conv_1","workspace":"/tmp/workspace"}'
    )


@respx.mock
async def test_launch_runner_picks_random_online_host_when_unspecified() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "hosts": [
                    {"id": "host_offline", "status": "offline"},
                    {"id": "host_online", "status": "online"},
                ]
            },
        )
    )
    launch = respx.post("http://omnigent.test/v1/hosts/host_online/runners").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_launched"})
    )
    respx.get("http://omnigent.test/v1/runners/runner_launched/status").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_launched", "online": True})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        runner_id = await client.launch_runner("conv_1", workspace="/tmp/workspace")
    finally:
        await client.aclose()

    assert runner_id == "runner_launched"
    assert launch.called


async def test_launch_runner_requires_workspace() -> None:
    client = OmnigentClient("http://omnigent.test")

    try:
        message = ""
        try:
            await client.launch_runner("conv_1", workspace="")
        except OmnigentError as exc:
            message = str(exc)
    finally:
        await client.aclose()

    assert "workspace" in message.lower()


@respx.mock
async def test_launch_runner_errors_when_no_online_host() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(200, json={"hosts": [{"id": "h", "status": "offline"}]})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        raised: HostUnavailableError | None = None
        try:
            await client.launch_runner("conv_1", workspace="/tmp/workspace")
        except HostUnavailableError as exc:
            raised = exc
    finally:
        await client.aclose()

    assert raised is not None
    assert "No online Omnigent hosts" in str(raised)


@respx.mock
async def test_launch_runner_raises_host_unavailable_when_host_offline() -> None:
    respx.post("http://omnigent.test/v1/hosts/host_1/runners").mock(
        return_value=httpx.Response(409, json={"error": {"code": "host_offline"}})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        raised = False
        try:
            await client.launch_runner("conv_1", workspace="/ws", host_id="host_1")
        except HostUnavailableError:
            raised = True
    finally:
        await client.aclose()

    assert raised


@respx.mock
async def test_launch_runner_raises_host_unavailable_when_runner_never_online() -> None:
    respx.post("http://omnigent.test/v1/hosts/host_1/runners").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_x"})
    )
    respx.get("http://omnigent.test/v1/runners/runner_x/status").mock(
        return_value=httpx.Response(200, json={"online": False})
    )
    client = OmnigentClient("http://omnigent.test", runner_launch_timeout_seconds=0.01)

    try:
        raised = False
        try:
            await client.launch_runner("conv_1", workspace="/ws", host_id="host_1")
        except HostUnavailableError:
            raised = True
    finally:
        await client.aclose()

    assert raised


async def test_request_wraps_transport_failure_as_server_unreachable() -> None:
    # Point at a port nothing is listening on so the connection is refused.
    client = OmnigentClient("http://127.0.0.1:1")

    try:
        raised = False
        try:
            await client.check_health()
        except ServerUnreachableError:
            raised = True
    finally:
        await client.aclose()

    assert raised


@respx.mock
async def test_run_turn_streams_across_multiple_responses_until_session_idle() -> None:
    # An orchestrator ends its first response to wait on a sub-agent, then
    # resumes with the real answer in a second response. The turn is only over
    # once the session settles to idle — `response.completed` alone must not
    # cut the stream off after the "dispatched, waiting" message.
    sse_body = (
        'data: {"type":"response.output_text.delta","delta":"Explorer dispatched."}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        'data: {"type":"response.output_text.delta","delta":"Here is the report."}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        'data: {"type":"session.status","conversation_id":"conv_1","status":"idle"}\n\n'
        "data: [DONE]\n\n"
    )
    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, text=sse_body)
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        deltas = [
            event.get("delta")
            async for event in client.run_turn("conv_1", "hello")
            if event.get("type") == "response.output_text.delta"
        ]
    finally:
        await client.aclose()

    # Both responses stream; the second (the real answer) is not dropped.
    assert deltas == ["Explorer dispatched.", "Here is the report."]


@respx.mock
async def test_run_turn_resumes_after_idle_when_stream_continues() -> None:
    # A fan-out orchestrator ends its turn to wait on sub-agents, settling to
    # `idle` between wake cycles, then resumes with more output when a sub-agent
    # completes. The bot must NOT stop at the first idle: within the grace
    # window the stream delivers more, so the turn keeps going to the real end.
    sse_body = (
        'data: {"type":"response.output_text.delta","delta":"Fanning out."}\n\n'
        'data: {"type":"session.status","conversation_id":"conv_1","status":"idle"}\n\n'
        'data: {"type":"response.output_text.delta","delta":"Collecting results."}\n\n'
        'data: {"type":"session.status","conversation_id":"conv_1","status":"idle"}\n\n'
        'data: {"type":"response.output_text.delta","delta":"All done."}\n\n'
        'data: {"type":"session.status","conversation_id":"conv_1","status":"idle"}\n\n'
        "data: [DONE]\n\n"
    )
    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, text=sse_body)
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        deltas = [
            event.get("delta")
            async for event in client.run_turn("conv_1", "go", idle_grace_seconds=5.0)
            if event.get("type") == "response.output_text.delta"
        ]
    finally:
        await client.aclose()

    # All three segments streamed across the intermediate idle edges — the turn
    # only ends at the final idle when the stream itself closes.
    assert deltas == ["Fanning out.", "Collecting results.", "All done."]


@respx.mock
async def test_run_turn_transient_idle_midstream_does_not_truncate() -> None:
    # claude-native oscillates running/idle WHILE still streaming its answer,
    # with a sub-second gap before the next burst — and the snapshot reads `idle`
    # during that gap. The settle wait must catch the resumption rather than
    # ending the turn on the transient idle (which truncated the reply).
    async def _bursty_stream() -> AsyncIterator[bytes]:
        yield b'data: {"type":"response.output_text.delta","delta":"Part one. "}\n\n'
        yield b'data: {"type":"session.status","status":"idle"}\n\n'
        # Real gap before the next burst — shorter than the settle window.
        await asyncio.sleep(0.2)
        yield b'data: {"type":"response.output_text.delta","delta":"Part two. "}\n\n'
        yield b'data: {"type":"session.status","status":"idle"}\n\n'
        await asyncio.sleep(0.2)
        yield b'data: {"type":"response.output_text.delta","delta":"Part three."}\n\n'
        yield b'data: {"type":"session.status","status":"idle"}\n\n'
        yield b"data: [DONE]\n\n"

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_bursty_stream())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    # Snapshot reads `idle` during the gaps — the WRONG signal to end on. The
    # settle wait must win over it while text is still coming.
    respx.get("http://omnigent.test/v1/sessions/conv_1").mock(
        return_value=httpx.Response(200, json={"status": "idle"})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        deltas = [
            event.get("delta")
            async for event in client.run_turn(
                "conv_1",
                "go",
                idle_grace_seconds=5.0,
                idle_poll_seconds=5.0,
                idle_settle_seconds=1.0,
            )
            if event.get("type") == "response.output_text.delta"
        ]
    finally:
        await client.aclose()

    # All three bursts delivered — no mid-answer truncation despite the idles.
    assert deltas == ["Part one. ", "Part two. ", "Part three."]


@respx.mock
async def test_run_turn_ends_when_stream_goes_silent_without_idle_event() -> None:
    # Incident 3cca0d8d: the stream produces output then goes SILENT with NO
    # terminal/idle event ever arriving (half-open connection, or the `idle` edge
    # was missed while the consumer was parked). A bare read would block forever,
    # holding the thread's reservation and deflecting every follow-up. Every read
    # after the first event is now grace-bounded, so the turn ends when the
    # snapshot shows the server is idle.
    async def _silent_after_output() -> AsyncIterator[bytes]:
        yield b'data: {"type":"response.output_text.delta","delta":"Some answer."}\n\n'
        await asyncio.sleep(30)  # then nothing: no idle, no [DONE] — a bare read hangs

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_silent_after_output())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    # The server is actually done (idle) — the stream just never told us.
    respx.get("http://omnigent.test/v1/sessions/conv_1").mock(
        return_value=httpx.Response(200, json={"status": "idle"})
    )
    client = OmnigentClient("http://omnigent.test")

    async def _drain() -> list[str | None]:
        return [
            event.get("delta")
            async for event in client.run_turn(
                "conv_1",
                "go",
                idle_grace_seconds=5.0,
                idle_poll_seconds=0.05,
                idle_settle_seconds=0.05,
            )
            if event.get("type") == "response.output_text.delta"
        ]

    try:
        # Must finish well within the 30s silent stall — bounded by the poll.
        deltas = await asyncio.wait_for(_drain(), timeout=5.0)
    finally:
        await client.aclose()

    assert deltas == ["Some answer."]  # delivered, then the turn ended cleanly


@respx.mock
async def test_run_turn_ends_when_idle_grace_elapses_and_snapshot_idle() -> None:
    # A truly-final idle: the stream stays open briefly (no `[DONE]`) but nothing
    # more arrives within the grace window, and the snapshot confirms the session
    # is idle — so the turn ends rather than hanging on the late delta.
    async def _slow_stream() -> AsyncIterator[bytes]:
        yield b'data: {"type":"response.output_text.delta","delta":"Answer."}\n\n'
        yield b'data: {"type":"session.status","status":"idle"}\n\n'
        await asyncio.sleep(0.4)
        yield b'data: {"type":"response.output_text.delta","delta":"too late"}\n\n'

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_slow_stream())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    # Snapshot says idle → nothing outstanding → end the turn.
    respx.get("http://omnigent.test/v1/sessions/conv_1").mock(
        return_value=httpx.Response(200, json={"status": "idle"})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        deltas = [
            event.get("delta")
            async for event in client.run_turn(
                "conv_1",
                "go",
                idle_grace_seconds=5.0,
                idle_poll_seconds=0.05,
                idle_settle_seconds=0.05,
            )
            if event.get("type") == "response.output_text.delta"
        ]
    finally:
        await client.aclose()

    # The settle window (0.05s) was quiet and the snapshot is idle, so the turn
    # ended at the idle before the late delta (0.4s); it was never delivered.
    assert deltas == ["Answer."]


@respx.mock
async def test_run_turn_does_not_hang_after_elicitation_when_stream_silent() -> None:
    # Incident 10f1d893: after an elicitation, the consumer parks to handle it,
    # leaving the SSE connection unread. When it resumes, the (now stale) stream
    # delivers nothing more and never closes — a bare read would hang forever,
    # wedging the thread. The loop must treat the elicitation like a soft idle:
    # settle-wait, then poll the snapshot, and END when the session is idle.
    async def _stalls_after_elicitation() -> AsyncIterator[bytes]:
        yield b'data: {"type":"response.output_text.delta","delta":"Before deleting."}\n\n'
        yield (
            b'data: {"type":"response.elicitation_request",'
            b'"elicitation_id":"e1","params":{"message":"Approve?"}}\n\n'
        )
        # Then nothing: no more events, no [DONE]. A bare read here hangs.
        await asyncio.sleep(30)

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_stalls_after_elicitation())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    # The server has gone idle (the turn actually finished server-side).
    respx.get("http://omnigent.test/v1/sessions/conv_1").mock(
        return_value=httpx.Response(200, json={"status": "idle"})
    )
    client = OmnigentClient("http://omnigent.test")

    async def _drain() -> list[str]:
        return [
            event.get("type")
            async for event in client.run_turn(
                "conv_1",
                "go",
                idle_grace_seconds=5.0,
                idle_poll_seconds=0.05,
                idle_settle_seconds=0.05,
            )
        ]

    try:
        # Must complete well within the stream's 30s stall — bounded by the poll,
        # not hanging on the read.
        types = await asyncio.wait_for(_drain(), timeout=5.0)
    finally:
        await client.aclose()

    # The elicitation event was surfaced, then the turn ended cleanly (no hang).
    assert "response.elicitation_request" in types


async def test_await_within_grace_waits_while_snapshot_running() -> None:
    # The idle-disambiguation helper: each quiet poll consults the snapshot.
    # While the rolled-up status is `running` (a sub-agent child is still
    # working), it keeps waiting past the poll interval rather than ending;
    # once the in-flight read completes it reports resumption.
    from omnigent_slack.omnigent import _NO_RESUMPTION

    async def _slow_read() -> dict[str, object]:
        # Longer than the poll interval, so several polls fire first.
        await asyncio.sleep(0.15)
        return {"type": "response.output_text.delta", "delta": "Collected."}

    client = OmnigentClient("http://omnigent.test")

    async def _running_status(session_id: str) -> str | None:
        return "running"  # child still working across every quiet poll

    client.get_session_status = _running_status  # type: ignore[method-assign]
    pending = asyncio.ensure_future(_slow_read())
    try:
        # Poll every 0.05s, generous 5s cap — resumes well before the cap.
        result = await client._await_within_grace(pending, "conv_1", 5.0, 0.05, 0.05)
    finally:
        await client.aclose()

    # Snapshot said running across the quiet polls, so it waited for the read
    # to complete (resumption) instead of returning the end sentinel.
    assert result is not _NO_RESUMPTION
    assert pending.done() and pending.result()["delta"] == "Collected."


async def test_await_within_grace_ends_when_snapshot_not_running() -> None:
    from omnigent_slack.omnigent import _NO_RESUMPTION

    async def _silent_read() -> dict[str, object]:
        await asyncio.sleep(5.0)  # never completes within the poll interval
        return {"type": "response.output_text.delta", "delta": "too late"}

    client = OmnigentClient("http://omnigent.test")

    async def _idle_status(session_id: str) -> str | None:
        return "idle"

    client.get_session_status = _idle_status  # type: ignore[method-assign]
    pending = asyncio.ensure_future(_silent_read())
    try:
        result = await client._await_within_grace(pending, "conv_1", 5.0, 0.02, 0.02)
    finally:
        pending.cancel()
        await client.aclose()

    # First quiet poll + snapshot idle → the turn is genuinely over.
    assert result is _NO_RESUMPTION


async def test_await_within_grace_keeps_waiting_on_transient_status_none() -> None:
    # A None status is a best-effort snapshot failure (transient blip), NOT a
    # confirmed end. Treating it as "done" would truncate a still-live fan-out on
    # a momentary hiccup. The loop must keep waiting until the grace cap instead.
    from omnigent_slack.omnigent import _NO_RESUMPTION

    async def _never_read() -> dict[str, object]:
        await asyncio.sleep(60.0)
        return {"type": "response.output_text.delta", "delta": "never"}

    client = OmnigentClient("http://omnigent.test")
    calls = 0

    async def _flaky_status(session_id: str) -> str | None:
        nonlocal calls
        calls += 1
        return None  # snapshot fetch keeps failing

    client.get_session_status = _flaky_status  # type: ignore[method-assign]
    pending = asyncio.ensure_future(_never_read())
    try:
        # Cap 0.08s, poll 0.02s → several polls; each returns None but must not
        # end early — only the cap ends it.
        result = await client._await_within_grace(pending, "conv_1", 0.08, 0.02, 0.02)
    finally:
        pending.cancel()
        await client.aclose()

    assert result is _NO_RESUMPTION  # ended by the cap, not the first None
    assert calls >= 2  # kept polling through the transient failures


async def test_await_within_grace_cap_ends_even_while_running() -> None:
    # The cap is a backstop: if the snapshot stays `running` forever (stuck
    # session), the turn still ends once the total grace cap elapses rather than
    # parking indefinitely.
    from omnigent_slack.omnigent import _NO_RESUMPTION

    async def _never_read() -> dict[str, object]:
        await asyncio.sleep(60.0)
        return {"type": "response.output_text.delta", "delta": "never"}

    client = OmnigentClient("http://omnigent.test")

    async def _stuck_running(session_id: str) -> str | None:
        return "running"  # never settles

    client.get_session_status = _stuck_running  # type: ignore[method-assign]
    pending = asyncio.ensure_future(_never_read())
    try:
        # Poll 0.02s, cap 0.05s → a couple of polls then the cap ends it.
        result = await client._await_within_grace(pending, "conv_1", 0.05, 0.02, 0.02)
    finally:
        pending.cancel()
        await client.aclose()

    assert result is _NO_RESUMPTION


@respx.mock
async def test_client_raises_runner_unavailable() -> None:
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(
            503,
            json={"error": {"code": "runner_unavailable", "message": "No runner bound"}},
        )
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        try:
            await client.submit_message("conv_1", "hello")
        except RunnerUnavailableError:
            raised = True
        else:
            raised = False
    finally:
        await client.aclose()

    assert raised is True


def test_extract_elicitation_request_parses_fields() -> None:
    req = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_abc",
            "params": {
                "message": "Approve running rm?",
                "policy_name": "approve_shell",
                "content_preview": '{"command": "rm -rf x"}',
            },
        },
        "conv_stream",
    )
    assert req is not None
    assert req.elicitation_id == "elicit_abc"
    assert req.message == "Approve running rm?"
    assert req.policy_name == "approve_shell"
    assert req.content_preview == '{"command": "rm -rf x"}'
    # No target_session_id → resolve against the streaming session.
    assert req.session_id == "conv_stream"


def test_extract_elicitation_request_uses_target_session_when_mirrored() -> None:
    req = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_child",
            "params": {"message": "child asks", "target_session_id": "conv_child"},
        },
        "conv_parent",
    )
    assert req is not None
    # A mirrored sub-agent prompt resolves against the child, not the parent.
    assert req.session_id == "conv_child"


def test_extract_elicitation_request_ignores_other_events() -> None:
    assert extract_elicitation_request({"type": "response.output_text.delta"}, "s") is None
    # Missing/blank id is not a usable request.
    assert (
        extract_elicitation_request({"type": "response.elicitation_request", "params": {}}, "s")
        is None
    )


@respx.mock
async def test_resolve_elicitation_posts_accept() -> None:
    route = respx.post(
        "http://omnigent.test/v1/sessions/conv_1/elicitations/elicit_1/resolve"
    ).mock(return_value=httpx.Response(202, json={"queued": False}))
    client = OmnigentClient("http://omnigent.test")
    try:
        await client.resolve_elicitation("conv_1", "elicit_1", accepted=True)
    finally:
        await client.aclose()
    assert route.calls.last.request.read() == b'{"action":"accept"}'


@respx.mock
async def test_resolve_elicitation_decline_and_benign_statuses() -> None:
    # 404/409 are benign (already resolved / cancel race) — no raise.
    respx.post("http://omnigent.test/v1/sessions/conv_1/elicitations/gone/resolve").mock(
        return_value=httpx.Response(404, json={})
    )
    client = OmnigentClient("http://omnigent.test")
    try:
        await client.resolve_elicitation("conv_1", "gone", accepted=False)
    finally:
        await client.aclose()


@respx.mock
async def test_get_session_activity_maps_server_state() -> None:
    # The server snapshot is the authoritative "is this session busy?" signal.
    def snap(status: str, pending: list[dict[str, object]]) -> httpx.Response:
        return httpx.Response(200, json={"status": status, "pending_elicitations": pending})

    client = OmnigentClient("http://omnigent.test")
    try:
        route = respx.get("http://omnigent.test/v1/sessions/conv_1")

        route.mock(return_value=snap("running", []))
        a = await client.get_session_activity("conv_1")
        assert a.is_busy and not a.needs_user_action

        route.mock(return_value=snap("waiting", [{"elicitation_id": "e1"}]))
        a = await client.get_session_activity("conv_1")
        assert a.is_busy and a.needs_user_action

        route.mock(return_value=snap("idle", []))
        a = await client.get_session_activity("conv_1")
        assert not a.is_busy and not a.needs_user_action

        # An idle session that still has a pending elicitation needs action.
        route.mock(return_value=snap("idle", [{"elicitation_id": "e2"}]))
        a = await client.get_session_activity("conv_1")
        assert not a.is_busy and a.needs_user_action
    finally:
        await client.aclose()


@respx.mock
async def test_get_session_activity_unreadable_snapshot_is_not_busy() -> None:
    # A best-effort read failure must not report busy — the server safely buffers
    # a message that races a turn, so "go ahead" is the safe conservative default.
    respx.get("http://omnigent.test/v1/sessions/conv_1").mock(return_value=httpx.Response(500))
    client = OmnigentClient("http://omnigent.test")
    try:
        a = await client.get_session_activity("conv_1")
    finally:
        await client.aclose()
    assert a.status is None
    assert not a.is_busy and not a.needs_user_action


def test_extract_policy_denied() -> None:
    assert (
        extract_policy_denied(
            {"type": "response.policy_denied", "conversation_id": "c1", "reason": "No shell."}
        )
        == "No shell."
    )
    # Missing reason falls back to a generic message.
    assert extract_policy_denied({"type": "response.policy_denied"}) == "Blocked by policy."
    # Non-matching events return None.
    assert extract_policy_denied({"type": "response.output_text.delta"}) is None


def test_extract_output_file() -> None:
    f = extract_output_file(
        {"type": "response.output_file.done", "file_id": "file_1", "filename": "report.pdf"}
    )
    assert f is not None and f.file_id == "file_1" and f.filename == "report.pdf"
    # No filename → None filename, still a valid artifact.
    f2 = extract_output_file({"type": "response.output_file.done", "file_id": "file_2"})
    assert f2 is not None and f2.filename is None
    # Missing id / wrong type → None.
    assert extract_output_file({"type": "response.output_file.done"}) is None
    assert extract_output_file({"type": "session.status"}) is None


def test_extract_todos() -> None:
    todos = extract_todos(
        {
            "type": "session.todos",
            "conversation_id": "c1",
            "todos": [
                {"content": "A", "status": "completed", "activeForm": "Doing A"},
                {"content": "B", "status": "in_progress", "activeForm": "Doing B"},
            ],
        }
    )
    assert todos is not None and len(todos) == 2
    # An empty list is a real "no todos" update, distinct from a non-todo event.
    assert extract_todos({"type": "session.todos", "todos": []}) == []
    assert extract_todos({"type": "session.status"}) is None


def test_elicitation_url_mode_binary_is_supported() -> None:
    # `url` mode only carries a suggested approve page; a binary approval (empty
    # requestedSchema) is still rendered natively as Approve/Deny, not fobbed
    # off to the web link. This is the default server mode.
    req = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "e1",
            "params": {
                "mode": "url",
                "message": "Agent wants to run a shell command. Approve?",
                "phase": "tool_call",
                "requestedSchema": {},
                "url": "/approve/conv_1/e1",
            },
        },
        "conv_1",
    )
    assert req is not None
    assert req.mode == "url"
    assert not req.is_form
    assert req.is_supported is True


def test_elicitation_typed_schema_is_unsupported() -> None:
    # A requestedSchema with fields (and no AskUserQuestion) needs typed input we
    # can't collect with buttons — unsupported regardless of mode.
    for mode in ("form", "url"):
        req = extract_elicitation_request(
            {
                "type": "response.elicitation_request",
                "elicitation_id": "e1",
                "params": {
                    "mode": mode,
                    "message": "Enter a value",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
            "conv_1",
        )
        assert req is not None
        assert req.needs_typed_input is True
        assert req.is_supported is False


def test_elicitation_binary_and_form_are_supported() -> None:
    binary = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "e1",
            "params": {"message": "Approve?"},
        },
        "conv_1",
    )
    assert binary is not None and binary.is_supported is True and not binary.is_form

    form = extract_elicitation_request(
        {
            "type": "response.elicitation_request",
            "elicitation_id": "e2",
            "params": {
                "message": "Pick",
                "requestedSchema": {"type": "object"},
                "ask_user_question": {
                    "questions": [{"question": "Q?", "options": [{"label": "A"}]}]
                },
            },
        },
        "conv_1",
    )
    # Even with a schema present, an AskUserQuestion is a supported form.
    assert form is not None and form.is_form and form.is_supported is True
