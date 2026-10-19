"""Runner registry and routes for chat code-block runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities.conversation import CODE_RUN_START_REFUSALS, code_run_source_id
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.code_runs import (
    CodeRunLimitError,
    CodeRunOutput,
    CodeRunRegistry,
    register_code_run_routes,
)
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests.runner.helpers import NullServerClient

_SESSION = "conv_run"


class _ScriptedEnvironment:
    """Stands in for a per-run sandbox helper.

    Emits *chunks*, then either returns *result* or, when *block* is set,
    waits for ``stop_shell_stream`` like a long-running process.
    """

    def __init__(
        self,
        chunks: list[tuple[str, str]] | None = None,
        result: dict[str, object] | None = None,
        *,
        block: bool = False,
    ) -> None:
        self.chunks = chunks or []
        self.result = result or {"exit_code": 0, "timed_out": False, "truncated": False}
        self.block = block
        self.calls: list[tuple[str, str, int]] = []
        self.stop_calls = 0
        self.closed = False
        self._stopped = asyncio.Event()

    async def shell_stream(
        self,
        command: str,
        *,
        language: str,
        timeout: int,
        on_output: Callable[[str, str], None],
    ) -> dict[str, object]:
        self.calls.append((command, language, timeout))
        for stream, text in self.chunks:
            on_output(stream, text)
        if self.block:
            await self._stopped.wait()
            return {"exit_code": None, "timed_out": False, "truncated": False}
        return self.result

    def stop_shell_stream(self) -> None:
        self.stop_calls += 1
        self._stopped.set()

    def kill_shell_stream(self) -> None:
        self._stopped.set()

    def close(self) -> None:
        self.closed = True


class _RecordingServer:
    """Server client that records event posts and replays scripted outcomes."""

    def __init__(self, outcomes: list[int | Exception] | None = None) -> None:
        self.posts: list[tuple[str, Any]] = []
        self.outcomes = list(outcomes or [])

    async def post(self, url: str, *, json: Any = None, timeout: float | None = None) -> Any:
        del timeout
        self.posts.append((url, json))
        outcome = self.outcomes.pop(0) if self.outcomes else 202
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={}, request=httpx.Request("POST", url))


def _registry(
    environment: _ScriptedEnvironment | Exception,
    events: list[Mapping[str, object]],
    server: _RecordingServer,
    sleeps: list[float] | None = None,
) -> CodeRunRegistry:
    async def environment_factory(session_id: str) -> Any:
        del session_id
        if isinstance(environment, Exception):
            raise environment
        return environment

    async def fake_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    return CodeRunRegistry(
        publish_event=lambda session_id, event: events.append(event),
        server_client=server,  # type: ignore[arg-type]
        environment_factory=environment_factory,
        sleep=fake_sleep,
    )


def _start(registry: CodeRunRegistry, run_id: str = "run_1", command: str = "echo hi") -> None:
    registry.start(_SESSION, run_id=run_id, language="shell", command=command, timeout=30)


def _finished_body(server: _RecordingServer) -> dict[str, Any]:
    url, body = server.posts[-1]
    assert url == f"/v1/sessions/{_SESSION}/events"
    return body


def test_output_keeps_the_start_and_end_past_the_cap() -> None:
    output = CodeRunOutput(limit=10)
    output.append("stdout", "abcdef")
    output.append("stderr", "ghijkl")

    assert output.total == 12
    assert output.truncated is True
    assert output.segments() == [
        {"stream": "stdout", "text": "abcde"},
        {"stream": "stderr", "text": "hijkl"},
    ]


def test_output_under_the_cap_merges_adjacent_chunks() -> None:
    output = CodeRunOutput(limit=10)
    output.append("stdout", "ab")
    output.append("stdout", "cd")
    output.append("stderr", "e")

    assert output.truncated is False
    assert output.segments() == [
        {"stream": "stdout", "text": "abcd"},
        {"stream": "stderr", "text": "e"},
    ]


@pytest.mark.asyncio
async def test_output_coalesces_into_events_with_cumulative_offsets() -> None:
    events: list[Mapping[str, object]] = []
    environment = _ScriptedEnvironment(
        [("stdout", "a"), ("stdout", "b"), ("stderr", "c"), ("stdout", "d")], block=True
    )
    registry = _registry(environment, events, _RecordingServer())
    _start(registry)
    await asyncio.sleep(0.05)
    assert events == []

    await asyncio.sleep(0.3)
    assert events == [
        {
            "type": "session.code_run.output",
            "run_id": "run_1",
            "offset": 0,
            "stream": "stdout",
            "text": "ab",
        },
        {
            "type": "session.code_run.output",
            "run_id": "run_1",
            "offset": 2,
            "stream": "stderr",
            "text": "c",
        },
        {
            "type": "session.code_run.output",
            "run_id": "run_1",
            "offset": 3,
            "stream": "stdout",
            "text": "d",
        },
    ]
    registry.stop(_SESSION, "run_1")
    await registry.wait(_SESSION, "run_1")


@pytest.mark.asyncio
async def test_a_16_kib_burst_flushes_without_waiting() -> None:
    events: list[Mapping[str, object]] = []
    environment = _ScriptedEnvironment([("stdout", "x" * (16 * 1024))], block=True)
    registry = _registry(environment, events, _RecordingServer())
    _start(registry)
    await asyncio.sleep(0.05)

    assert [len(str(event["text"])) for event in events] == [16 * 1024]
    registry.stop(_SESSION, "run_1")
    await registry.wait(_SESSION, "run_1")


@pytest.mark.asyncio
async def test_finished_item_is_posted_with_a_deterministic_source_id() -> None:
    server = _RecordingServer()
    environment = _ScriptedEnvironment([("stdout", "hi\n")])
    registry = _registry(environment, [], server)
    _start(registry)
    await registry.wait(_SESSION, "run_1")

    body = _finished_body(server)
    assert len(server.posts) == 1
    assert body["type"] == "external_conversation_item"
    data = body["data"]
    assert data["item_type"] == "code_run"
    assert data["source_id"] == code_run_source_id("run_1", "finished")
    assert data["source_id"] == "code_run:run_1:finished"
    item = data["item_data"]
    assert item["run_id"] == "run_1"
    assert item["phase"] == "finished"
    assert item["status"] == "exited"
    assert item["exit_code"] == 0
    assert item["output"] == [{"stream": "stdout", "text": "hi\n"}]
    assert item["truncated"] is False
    assert isinstance(item["duration_ms"], int)
    assert environment.calls == [("echo hi", "shell", 30)]
    assert environment.closed is True
    assert registry.running(_SESSION) == []


@pytest.mark.asyncio
async def test_finished_post_retries_transient_failures_with_the_same_body() -> None:
    sleeps: list[float] = []
    server = _RecordingServer([httpx.ConnectError("down"), 503, 202])
    registry = _registry(_ScriptedEnvironment(), [], server, sleeps)
    _start(registry)
    await registry.wait(_SESSION, "run_1")

    assert len(server.posts) == 3
    assert server.posts[0] == server.posts[1] == server.posts[2]
    assert sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_finished_post_gives_up_after_five_minutes() -> None:
    sleeps: list[float] = []
    server = _RecordingServer([httpx.ConnectError("down")] * 100)
    registry = _registry(_ScriptedEnvironment(), [], server, sleeps)
    _start(registry)
    await registry.wait(_SESSION, "run_1")

    assert sum(sleeps) <= 300
    assert sum(sleeps) > 240
    assert len(server.posts) == len(sleeps) + 1


@pytest.mark.asyncio
async def test_finished_post_does_not_retry_a_client_error() -> None:
    sleeps: list[float] = []
    server = _RecordingServer([404])
    registry = _registry(_ScriptedEnvironment(), [], server, sleeps)
    _start(registry)
    await registry.wait(_SESSION, "run_1")

    assert len(server.posts) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_a_fifth_running_run_is_refused() -> None:
    registry = _registry(_ScriptedEnvironment(block=True), [], _RecordingServer())
    for index in range(4):
        _start(registry, run_id=f"run_{index}")

    with pytest.raises(CodeRunLimitError):
        _start(registry, run_id="run_4")
    await registry.stop_session(_SESSION)


@pytest.mark.asyncio
async def test_stop_ends_a_running_run_as_stopped() -> None:
    server = _RecordingServer()
    environment = _ScriptedEnvironment([("stdout", "partial")], block=True)
    registry = _registry(environment, [], server)
    _start(registry)
    await asyncio.sleep(0.05)

    assert registry.stop(_SESSION, "run_1") is True
    await registry.wait(_SESSION, "run_1")

    item = _finished_body(server)["data"]["item_data"]
    assert item["status"] == "stopped"
    assert item["exit_code"] is None
    assert item["output"] == [{"stream": "stdout", "text": "partial"}]
    assert environment.closed is True


@pytest.mark.asyncio
async def test_stop_of_an_unknown_run_does_nothing() -> None:
    registry = _registry(_ScriptedEnvironment(), [], _RecordingServer())

    assert registry.stop(_SESSION, "run_missing") is False


@pytest.mark.asyncio
async def test_timeout_and_failure_statuses() -> None:
    server = _RecordingServer()
    timed_out = _ScriptedEnvironment(
        result={"exit_code": None, "timed_out": True, "truncated": True}
    )
    registry = _registry(timed_out, [], server)
    _start(registry)
    await registry.wait(_SESSION, "run_1")
    item = _finished_body(server)["data"]["item_data"]
    assert (item["status"], item["truncated"]) == ("timed_out", True)

    registry = _registry(RuntimeError("no sandbox"), [], server)
    _start(registry, run_id="run_2")
    await registry.wait(_SESSION, "run_2")
    item = _finished_body(server)["data"]["item_data"]
    assert item["status"] == "failed"
    assert item["output"] == [{"stream": "stderr", "text": "no sandbox\n"}]


@pytest.mark.asyncio
async def test_running_lists_output_so_far() -> None:
    environment = _ScriptedEnvironment([("stdout", "a"), ("stderr", "b")], block=True)
    registry = _registry(environment, [], _RecordingServer())
    _start(registry)
    await asyncio.sleep(0.05)

    assert registry.running(_SESSION) == [
        {
            "run_id": "run_1",
            "offset": 2,
            "output": [{"stream": "stdout", "text": "a"}, {"stream": "stderr", "text": "b"}],
        }
    ]
    assert registry.running("conv_other") == []
    registry.stop(_SESSION, "run_1")
    await registry.wait(_SESSION, "run_1")
    assert registry.running(_SESSION) == []


@pytest.mark.asyncio
async def test_cleanup_session_stops_the_sessions_runs() -> None:
    environment = _ScriptedEnvironment(block=True)
    registry = _registry(environment, [], _RecordingServer())
    resources = SessionResourceRegistry()
    resources.set_code_run_stopper(registry.stop_session)
    _start(registry)
    await asyncio.sleep(0.05)

    await resources.cleanup_session(_SESSION)

    assert environment.stop_calls >= 1
    assert registry.running(_SESSION) == []


@pytest.fixture
async def routed() -> AsyncIterator[
    tuple[httpx.AsyncClient, dict[str, bool], _ScriptedEnvironment]
]:
    environment = _ScriptedEnvironment([("stdout", "x")], block=True)
    registry = _registry(environment, [], _RecordingServer())
    copy_on_write = {"value": False}

    async def uses_copy_on_write(session_id: str) -> bool:
        del session_id
        return copy_on_write["value"]

    app = FastAPI()
    register_code_run_routes(app, registry=registry, uses_copy_on_write=uses_copy_on_write)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client, copy_on_write, environment
    await registry.stop_session(_SESSION)


def _run_body(run_id: str = "run_1") -> dict[str, object]:
    return {"run_id": run_id, "language": "shell", "command": "echo hi", "timeout": 30}


@pytest.mark.asyncio
async def test_routes_start_list_and_stop(
    routed: tuple[httpx.AsyncClient, dict[str, bool], _ScriptedEnvironment],
) -> None:
    client, _, environment = routed
    base = f"/v1/sessions/{_SESSION}/code-runs"

    started = await client.post(base, json=_run_body())
    assert (started.status_code, started.json()) == (202, {"run_id": "run_1"})
    await asyncio.sleep(0.05)

    listed = await client.get(base, params={"status": "running"})
    assert listed.status_code == 200
    assert listed.json()["copy_on_write"] is False
    assert [run["run_id"] for run in listed.json()["data"]] == ["run_1"]

    stopped = await client.post(f"{base}/run_1/stop")
    assert (stopped.status_code, stopped.json()) == (200, {"stopped": True})
    assert environment.stop_calls == 1
    unknown = await client.post(f"{base}/run_missing/stop")
    assert (unknown.status_code, unknown.json()) == (200, {"stopped": False})


@pytest.mark.asyncio
async def test_routes_refuse_copy_on_write_and_a_fifth_run(
    routed: tuple[httpx.AsyncClient, dict[str, bool], _ScriptedEnvironment],
) -> None:
    client, copy_on_write, _ = routed
    base = f"/v1/sessions/{_SESSION}/code-runs"

    copy_on_write["value"] = True
    refused = await client.post(base, json=_run_body())
    assert (refused.status_code, refused.json()["code"]) == (409, CODE_RUN_START_REFUSALS[409])
    copy_on_write["value"] = False

    for index in range(4):
        assert (await client.post(base, json=_run_body(f"run_{index}"))).status_code == 202
    limited = await client.post(base, json=_run_body("run_4"))
    assert (limited.status_code, limited.json()["code"]) == (429, CODE_RUN_START_REFUSALS[429])


@pytest.mark.asyncio
async def test_routes_reject_a_malformed_run(
    routed: tuple[httpx.AsyncClient, dict[str, bool], _ScriptedEnvironment],
) -> None:
    client, _, _ = routed
    body = {**_run_body(), "language": "ruby"}

    response = await client.post(f"/v1/sessions/{_SESSION}/code-runs", json=body)

    assert response.status_code == 400
    # The server treats only these structured bodies as a run that never started.
    assert response.json()["code"] == CODE_RUN_START_REFUSALS[400]


class _EventRecordingServer(NullServerClient):
    """Null server client that also records event posts."""

    def __init__(self) -> None:
        self.event_posts: list[Any] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url.endswith("/events"):
            self.event_posts.append(kwargs.get("json"))
        return await super().post(url, **kwargs)


async def _view_then_disconnect(app: FastAPI, path: str) -> None:
    """Open an SSE stream on *app*, read its first frame, then disconnect.

    Drives the ASGI app directly: httpx's ASGI transport buffers a whole
    response body, so it cannot read one frame of an endless stream.
    """
    first_frame = asyncio.Event()

    async def receive() -> dict[str, object]:
        await first_frame.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_frame.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("runner", 80),
        "client": ("viewer", 1),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    assert first_frame.is_set()


@pytest.mark.asyncio
async def test_a_run_survives_a_dropped_viewer_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real run keeps going and saves its result after the viewer stream drops."""
    # An unsandboxed helper: an active sandbox hides this checkout from a tmp cwd.
    monkeypatch.setattr(
        "omnigent.runner.tool_dispatch._effective_runner_os_env_spec",
        lambda agent_spec, session_id, workspace: OSEnvSpec(
            type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none")
        ),
    )
    server = _EventRecordingServer()
    app = create_runner_app(
        runner_workspace=tmp_path,
        server_client=server,  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        started = await client.post(
            f"/v1/sessions/{_SESSION}/code-runs",
            json={
                "run_id": "run_live",
                "language": "shell",
                "command": "echo before; sleep 0.5; echo after",
                "timeout": 30,
            },
        )
        assert started.status_code == 202, started.text

        await _view_then_disconnect(app, f"/v1/sessions/{_SESSION}/stream")

        registry: CodeRunRegistry = app.state.code_run_registry
        await asyncio.wait_for(registry.wait(_SESSION, "run_live"), timeout=30)

    finished = [post for post in server.event_posts if post["data"]["item_type"] == "code_run"]
    assert len(finished) == 1
    item = finished[0]["data"]["item_data"]
    assert item["status"] == "exited", item
    assert "".join(segment["text"] for segment in item["output"]) == "before\nafter\n"


@pytest.mark.asyncio
async def test_code_run_routes_refuse_unauthenticated_calls_like_the_shell_route() -> None:
    """A direct call without the runner token is refused exactly as the shell route refuses it."""
    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        auth_token="runner-secret",
    )
    base = f"/v1/sessions/{_SESSION}"
    calls = [
        ("POST", f"{base}/resources/environments/default/shell", {"command": "id"}),
        ("POST", f"{base}/code-runs", _run_body()),
        ("POST", f"{base}/code-runs/run_1/stop", {}),
        ("GET", f"{base}/code-runs?status=running", None),
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            refusals = [
                await client.request(method, url, json=body, headers=headers)
                for method, url, body in calls
            ]
            assert {(r.status_code, r.text) for r in refusals} == {
                (401, '{"detail":"Invalid or missing runner auth token"}')
            }
        listed = await client.get(
            f"{base}/code-runs", headers={"Authorization": "Bearer runner-secret"}
        )
    assert listed.status_code == 200


@pytest.mark.asyncio
async def test_a_spoofed_tunnel_client_is_still_refused() -> None:
    """``X-Forwarded-For: tunnel`` through a trusting proxy layer cannot skip the token.

    uvicorn's default proxy-header handling trusts loopback and rewrites the
    ASGI client from that header, so the client tuple alone cannot identify
    tunnel traffic; the in-tunnel scope's server marker can, and only the
    tunnel sets it.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        auth_token="runner-secret",
    )
    behind_proxy = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")  # type: ignore[arg-type]
    transport = httpx.ASGITransport(
        app=behind_proxy,  # type: ignore[arg-type]
        client=("127.0.0.1", 5000),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        spoofed = await client.get(
            f"/v1/sessions/{_SESSION}/code-runs", headers={"X-Forwarded-For": "tunnel"}
        )
    assert spoofed.status_code == 401


@pytest.mark.asyncio
async def test_an_in_tunnel_request_needs_no_bearer_token() -> None:
    """A request the tunnel dispatches passes the token check without a bearer."""
    from omnigent.runner.transports.ws_tunnel.frames import (
        RequestFrame,
        ResponseHeadFrame,
        decode_frame,
    )
    from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi

    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        auth_token="runner-secret",
    )
    frames: list[object] = []

    async def send_text(data: str) -> None:
        frames.append(decode_frame(data))

    await dispatch_via_asgi(
        app,  # type: ignore[arg-type]
        RequestFrame(id="req_1", method="GET", path=f"/v1/sessions/{_SESSION}/code-runs"),
        send_text,
    )

    [head] = [frame for frame in frames if isinstance(frame, ResponseHeadFrame)]
    assert head.status == 200
