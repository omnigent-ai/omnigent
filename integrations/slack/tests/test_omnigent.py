from collections.abc import AsyncIterator

import httpx
import respx

from omnigent_slack.omnigent import (
    OmnigentAuth,
    OmnigentClient,
    OmnigentError,
    RunnerUnavailableError,
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
    client = OmnigentClient(
        "http://omnigent.test",
        auth=OmnigentAuth(email="bot@example.com", session_cookie="cookie-value"),
    )

    try:
        session_id = await client.create_session("ag_1", "Slack C/1")
        await client.submit_message(session_id, "hello")
    finally:
        await client.aclose()

    assert session_id == "conv_1"
    assert create.calls.last.request.headers["X-Forwarded-Email"] == "bot@example.com"
    assert create.calls.last.request.headers["Cookie"] == "ap_session=cookie-value"
    assert create.calls.last.request.read() == b'{"agent_id":"ag_1","title":"Slack C/1"}'
    assert submit.calls.last.request.read() == (
        b'{"type":"message","data":{"role":"user","content":[{"type":"input_text",'
        b'"text":"hello"}]}}'
    )


@respx.mock
async def test_client_binds_random_runner() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(
            200,
            json={"hosts": [{"id": "host_1", "online": True, "runners": [{"id": "runner_a"}]}]},
        )
    )
    bind = respx.patch("http://omnigent.test/v1/sessions/conv_1").mock(
        return_value=httpx.Response(200, json={"id": "conv_1", "runner_id": "runner_a"})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        runner_id = await client.bind_random_runner("conv_1")
    finally:
        await client.aclose()

    assert runner_id == "runner_a"
    assert bind.calls.last.request.read() == b'{"runner_id":"runner_a"}'


@respx.mock
async def test_client_launches_runner_when_no_online_runner_exists() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(200, json={"hosts": []})
    )
    launch = respx.post("http://omnigent.test/v1/hosts/host_1/runners").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_launched"})
    )
    respx.get("http://omnigent.test/v1/runners/runner_launched/status").mock(
        return_value=httpx.Response(200, json={"runner_id": "runner_launched", "online": True})
    )
    client = OmnigentClient(
        "http://omnigent.test",
        runner_workspace="/tmp/workspace",
        runner_host_id="host_1",
    )

    try:
        runner_id = await client.bind_random_runner("conv_1")
    finally:
        await client.aclose()

    assert runner_id == "runner_launched"
    assert launch.calls.last.request.read() == (
        b'{"session_id":"conv_1","workspace":"/tmp/workspace"}'
    )


@respx.mock
async def test_client_launches_runner_on_random_online_host() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "hosts": [
                    {"id": "host_offline", "status": "offline"},
                    {"id": "host_online", "online": True},
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
    client = OmnigentClient("http://omnigent.test", runner_workspace="/tmp/workspace")

    try:
        runner_id = await client.bind_random_runner("conv_1")
    finally:
        await client.aclose()

    assert runner_id == "runner_launched"
    assert launch.called


@respx.mock
async def test_client_binds_runner_loaded_from_hosts() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "hosts": [
                    {"id": "host_offline", "online": False, "runners": [{"id": "runner_no"}]},
                    {
                        "id": "host_online",
                        "online": True,
                        "runners": [{"runner_id": "runner_from_host"}],
                    },
                ]
            },
        )
    )
    bind = respx.patch("http://omnigent.test/v1/sessions/conv_1").mock(
        return_value=httpx.Response(200, json={"id": "conv_1"})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        runner_id = await client.bind_random_runner("conv_1")
    finally:
        await client.aclose()

    assert runner_id == "runner_from_host"
    assert bind.calls.last.request.read() == b'{"runner_id":"runner_from_host"}'


@respx.mock
async def test_client_errors_when_no_runner_and_no_launch_workspace() -> None:
    respx.get("http://omnigent.test/v1/hosts").mock(
        return_value=httpx.Response(200, json={"hosts": []})
    )
    client = OmnigentClient("http://omnigent.test")

    try:
        try:
            await client.bind_random_runner("conv_1")
        except OmnigentError as exc:
            message = str(exc)
        else:
            message = ""
    finally:
        await client.aclose()

    assert "OMNIGENT_RUNNER_WORKSPACE" in message


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
