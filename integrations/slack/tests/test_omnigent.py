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
