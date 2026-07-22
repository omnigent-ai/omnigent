import asyncio
from collections.abc import AsyncIterator

import httpx
import respx
from omnigent_slack.omnigent import (
    AuthRequiredError,
    HarnessNotConfiguredError,
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
    is_hard_terminal_event,
    iter_sse_events,
    session_status,
)


def test_session_status_parses_status_and_response_id() -> None:
    # The turn-end signal is a session.status carrying a response_id (Stop hook);
    # a bare idle (no response_id) is a PTY-watcher flap and must be
    # distinguishable — response_id parses to None there.
    assert session_status(
        {"type": "session.status", "status": "running", "response_id": "resp_1"}
    ) == ("running", "resp_1")
    assert session_status({"type": "session.status", "status": "idle"}) == ("idle", None)
    assert session_status(
        {"type": "session.status", "status": "idle", "response_id": "resp_1"}
    ) == ("idle", "resp_1")
    # Empty/blank response_id normalizes to None.
    assert session_status({"type": "session.status", "status": "idle", "response_id": ""}) == (
        "idle",
        None,
    )
    # Non-status events → None.
    assert session_status({"type": "response.output_text.delta", "delta": "x"}) is None
    assert session_status({"type": "response.completed"}) is None


def test_is_hard_terminal_event() -> None:
    # Explicit failure/cancel end the turn regardless of response_id tracking.
    assert is_hard_terminal_event({"type": "response.failed"})
    assert is_hard_terminal_event({"type": "response.cancelled"})
    assert is_hard_terminal_event({"type": "turn.failed"})
    assert is_hard_terminal_event({"type": "turn.cancelled"})
    # A normal completion / delta / status is NOT hard-terminal.
    assert not is_hard_terminal_event({"type": "response.completed"})
    assert not is_hard_terminal_event({"type": "session.status", "status": "idle"})


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
async def test_run_turn_streams_across_multiple_responses_until_id_terminal() -> None:
    # An orchestrator ends its first response to wait on a sub-agent, then
    # resumes with the real answer in a second response. `response.completed`
    # alone must NOT end the turn; only the id-bearing terminal session.status
    # (the Stop-hook edge) does.
    sse_body = (
        'data: {"type":"response.output_text.delta","delta":"Explorer dispatched."}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        'data: {"type":"response.output_text.delta","delta":"Here is the report."}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        'data: {"type":"session.status","status":"idle","response_id":"resp_1"}\n\n'
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
async def test_run_turn_ignores_bare_idle_flaps_until_id_terminal() -> None:
    # claude-native's PTY watcher emits `session.status: idle` WITH NO
    # response_id mid-answer, between output bursts, while still generating. Those
    # flaps must be IGNORED — ending on one truncates the reply. The turn ends
    # only on the id-bearing idle (the Stop hook), after all bursts.
    sse_body = (
        'data: {"type":"session.status","status":"running","response_id":"resp_1"}\n\n'
        'data: {"type":"response.output_text.delta","delta":"Part one. "}\n\n'
        'data: {"type":"session.status","status":"idle"}\n\n'  # bare flap — ignore
        'data: {"type":"response.output_text.delta","delta":"Part two. "}\n\n'
        'data: {"type":"session.status","status":"idle"}\n\n'  # bare flap — ignore
        'data: {"type":"response.output_text.delta","delta":"Part three."}\n\n'
        'data: {"type":"session.status","status":"idle","response_id":"resp_1"}\n\n'  # real end
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
            async for event in client.run_turn("conv_1", "go")
            if event.get("type") == "response.output_text.delta"
        ]
    finally:
        await client.aclose()

    # All three bursts delivered — the bare-idle flaps did not truncate.
    assert deltas == ["Part one. ", "Part two. ", "Part three."]


@respx.mock
async def test_run_turn_ends_on_idless_idle_for_in_process_harness() -> None:
    # Incident dc05b28 (debby / claude-sdk in-process harness): ALL session.status
    # events are id-LESS for this harness (verified live + in schema). The turn
    # brackets are: id-less running -> deltas -> id-less WAITING (mid-fan-out,
    # sub-agents dispatched) -> id-less running -> final summary -> id-less IDLE.
    # The turn must: NOT end on the mid-fan-out `waiting`, stream the summary that
    # follows it, and END on the final id-less `idle`. (No response_id is ever
    # stamped, so the claude-native id-match strategy can't apply here.)
    async def _in_process_stream() -> AsyncIterator[bytes]:
        yield b'data: {"type":"session.status","status":"running"}\n\n'
        yield b'data: {"type":"response.output_text.delta","delta":"Dispatching partners."}\n\n'
        yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        yield b'data: {"type":"session.status","status":"waiting"}\n\n'  # mid-fan-out
        yield b'data: {"type":"session.status","status":"running"}\n\n'
        yield b'data: {"type":"response.output_text.delta","delta":"Both partners are back."}\n\n'
        yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        yield b'data: {"type":"session.status","status":"idle"}\n\n'  # id-less REAL end
        # The real server does NOT close after idle — it stays open with 15s
        # heartbeats. So ending REQUIRES recognizing the id-less idle; otherwise
        # the loop hangs to the liveness timeout. Model that with a long silence.
        await asyncio.sleep(30)

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_in_process_stream())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    async def _drain() -> list[str | None]:
        return [
            event.get("delta")
            async for event in client.run_turn("conv_1", "fan out", idle_grace_seconds=5.0)
            if event.get("type") == "response.output_text.delta"
        ]

    try:
        # Must end on the id-less idle, well within the 30s silence.
        deltas = await asyncio.wait_for(_drain(), timeout=5.0)
    finally:
        await client.aclose()

    # The post-`waiting` summary streamed (waiting didn't end the turn), and the
    # id-less idle ended it cleanly — no truncation, no hang.
    assert deltas == ["Dispatching partners.", "Both partners are back."]


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
        await asyncio.sleep(30)  # then nothing: no terminal, no heartbeat, no [DONE]

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_silent_after_output())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    async def _drain() -> list[str | None]:
        # A live connection heartbeats every ~15s; no event for idle_grace_seconds
        # means the socket is dead → end (the liveness backstop).
        return [
            event.get("delta")
            async for event in client.run_turn("conv_1", "go", idle_grace_seconds=0.3)
            if event.get("type") == "response.output_text.delta"
        ]

    try:
        deltas = await asyncio.wait_for(_drain(), timeout=5.0)
    finally:
        await client.aclose()

    assert deltas == ["Some answer."]  # delivered, then the dead socket ended it


@respx.mock
async def test_run_turn_ends_on_id_terminal_ignoring_later_deltas() -> None:
    # The id-bearing terminal is authoritative: once it arrives, the turn is over.
    # A stray later delta on the same (now-stale) stream is not delivered.
    async def _stream() -> AsyncIterator[bytes]:
        yield b'data: {"type":"session.status","status":"running","response_id":"resp_1"}\n\n'
        yield b'data: {"type":"response.output_text.delta","delta":"Answer."}\n\n'
        yield b'data: {"type":"session.status","status":"idle","response_id":"resp_1"}\n\n'
        await asyncio.sleep(0.4)
        yield b'data: {"type":"response.output_text.delta","delta":"too late"}\n\n'

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_stream())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        deltas = [
            event.get("delta")
            async for event in client.run_turn("conv_1", "go")
            if event.get("type") == "response.output_text.delta"
        ]
    finally:
        await client.aclose()

    # Ended at the id-terminal; the late delta after it was never delivered.
    assert deltas == ["Answer."]


@respx.mock
async def test_run_turn_does_not_hang_after_elicitation_when_stream_silent() -> None:
    # Incident 10f1d893: after an elicitation, the consumer parks to handle it,
    # leaving the SSE connection unread. If the stream then delivers nothing and
    # never closes, a bare read would hang forever, wedging the thread. The
    # liveness backstop (no event for idle_grace_seconds) ends the turn.
    async def _stalls_after_elicitation() -> AsyncIterator[bytes]:
        yield b'data: {"type":"response.output_text.delta","delta":"Before deleting."}\n\n'
        yield (
            b'data: {"type":"response.elicitation_request",'
            b'"elicitation_id":"e1","params":{"message":"Approve?"}}\n\n'
        )
        # Then nothing: no more events, no [DONE], no heartbeat.
        await asyncio.sleep(30)

    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(200, stream=_stalls_after_elicitation())
    )
    respx.post("http://omnigent.test/v1/sessions/conv_1/events").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OmnigentClient("http://omnigent.test")

    async def _drain() -> list[str]:
        return [
            event.get("type")
            async for event in client.run_turn("conv_1", "go", idle_grace_seconds=0.3)
        ]

    try:
        # Must complete well within the 30s stall — bounded by the liveness window.
        types = await asyncio.wait_for(_drain(), timeout=5.0)
    finally:
        await client.aclose()

    # The elicitation event was surfaced, then the turn ended cleanly (no hang).
    assert "response.elicitation_request" in types


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


@respx.mock
async def test_launch_runner_412_propagates_harness_not_configured_message() -> None:
    # A 412 harness_not_configured is an actionable precondition failure — the
    # server's curated error.message must reach the user, not collapse to the
    # generic "failed with status 412".
    respx.post("http://omnigent.test/v1/hosts/host_1/runners").mock(
        return_value=httpx.Response(
            412,
            json={
                "error": {
                    "code": "harness_not_configured",
                    "message": "launch failed: claude CLI missing; run omnigent setup",
                }
            },
        )
    )
    client = OmnigentClient("http://omnigent.test")
    try:
        raised: HarnessNotConfiguredError | None = None
        try:
            await client.launch_runner("conv_1", workspace="/home/u", host_id="host_1")
        except HarnessNotConfiguredError as exc:
            raised = exc
    finally:
        await client.aclose()

    assert raised is not None
    # The server's message is preserved verbatim (it's the actionable guidance).
    assert "omnigent setup" in str(raised)
    assert "status 412" not in str(raised)  # not the generic fallback


@respx.mock
async def test_stream_401_raises_auth_required_not_response_not_read() -> None:
    # A 401 on the SSE stream must classify as AuthRequiredError so the bot can
    # prompt "/omnigent to log in again". The stream response body is unread, so
    # the error classifier must read it before inspecting — otherwise httpx
    # raises ResponseNotRead and the real 401 is masked as a generic failure.
    respx.get("http://omnigent.test/v1/sessions/conv_1/stream").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "Authentication required"}}
        )
    )
    client = OmnigentClient("http://omnigent.test")
    try:
        raised: Exception | None = None
        try:
            async with client.stream_session_events("conv_1") as events:
                async for _event in events:
                    pass
        except Exception as exc:
            raised = exc
    finally:
        await client.aclose()

    assert isinstance(raised, AuthRequiredError)


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
