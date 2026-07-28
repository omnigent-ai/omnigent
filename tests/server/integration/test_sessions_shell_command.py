"""Integration tests for the ``shell_command`` session event.

Exercises ``POST /v1/sessions/{id}/events`` with
``type="shell_command"`` — the server-orchestrated web-composer bang
(``!``) command: spawn/send runner proxying, ``terminal_command``
receipt persistence (success and error), SSE publication, the
owner-only gate, and the guarantee that NO agent turn starts.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM). The runner is faked by monkeypatching
``_get_runner_client_for_resource_access`` — the same resolver the
terminal resource proxies use — with an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes.sessions import routes_events as events_module
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


# ── Helpers ───────────────────────────────────────────────


def _shell_command_data(**data: Any) -> dict[str, Any]:
    """Add one canonical attempt id to a shell-command event body."""
    return {"attempt_id": str(uuid.uuid4()), **data}


def _write_delayed_shell_wrapper(
    tmp_path: Path,
    *,
    shell_name: str,
    real_shell: str,
    delay_s: float,
) -> Path:
    """Create a shell-named shim that delays before preserving the real argv."""
    wrapper_dir = tmp_path / "delayed-shells"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / shell_name
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        f"time.sleep({delay_s!r})\n"
        f"real_shell = {real_shell!r}\n"
        "os.execv(real_shell, [real_shell, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _encoded_output_command(marker: str) -> str:
    """Return a short shell command whose typed form omits *marker*."""
    encoded = "".join(f"\\0{ord(char):03o}" for char in marker)
    return f"builtin printf '%b\\n' '{encoded}'"


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> dict[str, Any]:
    """Create a minimal session and return the JSON response.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind, e.g. ``"ag_abc123"``.
    :returns: The ``POST /v1/sessions`` response body.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _terminal_resource(
    session_id: str,
    terminal_name: str,
    session_key: str,
    *,
    running: bool = True,
) -> dict[str, Any]:
    """Build a runner-shaped terminal resource payload.

    :param session_id: Owning session id.
    :param terminal_name: Shell type, e.g. ``"zsh"``.
    :param session_key: Session key, e.g. ``"u-ab12cd"``.
    :param running: Whether the terminal reports as running.
    :returns: The resource dict the runner create/get routes return.
    """
    return {
        "id": f"terminal_{terminal_name}_{session_key}",
        "object": "session.resource",
        "type": "terminal",
        "session_id": session_id,
        "name": f"{terminal_name}:{session_key}",
        "environment": "default",
        "metadata": {
            "terminal_name": terminal_name,
            "session_key": session_key,
            "running": running,
        },
    }


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """Route resource-access runner calls to an in-memory fake.

    Patches ``_get_runner_client_for_resource_access`` — the resolver
    behind the terminal create/get/input proxies — so every call lands
    on ``handler``. Requests are recorded for call-order assertions.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param handler: Fake runner request handler.
    :returns: ``(fake_client, recorded_requests)``. Callers must close
        the client.
    """
    calls: list[httpx.Request] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        """Record the request, then delegate to the fake handler."""
        calls.append(request)
        return handler(request)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_recording_handler),
        base_url="http://runner",
    )

    async def _fake_resource_client(session_id: str) -> httpx.AsyncClient:
        """Resolve every session's resource access to the fake runner."""
        del session_id
        return fake_runner

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _fake_resource_client,
    )
    return fake_runner, calls


def _forbid_agent_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-fail the test if anything tries to start an agent turn.

    The resource-access fake only observes calls routed through it —
    an accidental agent turn would go through the GENERIC runner
    client / event dispatcher instead and slip past those assertions.
    Spy both so any turn start explodes immediately.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    def _no_generic_runner(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "shell_command must never resolve the generic runner client "
            "(that path forwards events and starts agent turns)"
        )

    def _no_turn_dispatch(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "shell_command must never dispatch a session event to the runner (agent turn)"
        )

    monkeypatch.setattr(sessions_module, "_get_runner_client", _no_generic_runner)
    monkeypatch.setattr(sessions_module, "_dispatch_session_event_to_runner", _no_turn_dispatch)


def _capture_stream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture every ``session_stream.publish`` call.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The live list publishes are appended to.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    real_publish = sessions_module.session_stream.publish

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        real_publish(session_id, event)
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        _capture,
    )
    return published


async def _terminal_command_items(
    client: httpx.AsyncClient,
    session_id: str,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return persisted ``terminal_command`` items for a session.

    :param client: The test HTTP client.
    :param session_id: Session/conversation identifier.
    :param headers: Optional auth headers.
    :returns: The flat API dicts of every persisted receipt.
    """
    resp = await client.get(f"/v1/sessions/{session_id}/items", headers=headers or {})
    assert resp.status_code == 200, resp.text
    return [item for item in resp.json()["data"] if item["type"] == "terminal_command"]


@pytest.fixture
def zsh_terminal_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the session's agent spec to one declaring a ``zsh`` terminal.

    The ``shell_command`` spawn path gates on the spec's
    ``terminals:`` block exactly like the terminal create route. These
    tests run without a real bundle load, so the module's spec loader
    is patched to a minimal spec declaring ``zsh``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.spec.types import AgentSpec

    spec = AgentSpec(spec_version=1, terminals={"zsh": TerminalEnvSpec(command="zsh")})
    monkeypatch.setattr(
        sessions_module,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: spec,
    )


# ── spawn: happy path + wire-shape pin ────────────────────


async def test_shell_command_spawn_executes_and_persists_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """Spawn creates a terminal, sends the command, persists one receipt.

    Pins the whole §5.2 spawn contract: declared-name gate passes,
    server-generated ``u-`` session key, create → input runner call
    order, ``session.resource.created`` + ``response.output_item.done``
    on the stream, receipt fields, and — critically — NO agent turn
    (no runner ``/events`` POST, no ``session.input.consumed``).
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve terminal create and input; fail anything else."""
        if request.method == "POST" and request.url.path.endswith("/resources/terminals"):
            body = json.loads(request.content)
            return httpx.Response(
                200, json=_terminal_resource(sid, body["terminal"], body["session_key"])
            )
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(200, json={"status": "sent", "outcome": "sent"})
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    published = _capture_stream(monkeypatch)
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    # Explicit 200, not the events route's 202 default: the command
    # already executed — and no misleading "queued" field.
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "queued" not in payload
    item = payload["item"]
    session_key = item["session_key"]
    attempt_id = item["attempt_id"]
    assert re.fullmatch(r"u-[0-9a-f]{32}", session_key)
    assert session_key == sessions_module._shell_spawn_session_key(attempt_id)
    assert payload["terminal"]["id"] == f"terminal_zsh_{session_key}"
    assert payload["sequence"] == payload["terminal"]["sequence"]
    assert payload["item_id"] == item["id"]

    # Runner saw exactly create → input, in order — and NO /events
    # forward: a bang command must never start an agent turn.
    assert [(c.method, c.url.path) for c in calls] == [
        ("POST", f"/v1/sessions/{sid}/resources/terminals"),
        ("POST", f"/v1/sessions/{sid}/resources/terminals/terminal_zsh_{session_key}/input"),
    ]
    assert json.loads(calls[0].content) == {
        "terminal": "zsh",
        "session_key": session_key,
        "attempt_id": attempt_id,
    }
    assert json.loads(calls[1].content) == {
        "attempt_id": attempt_id,
        "text": "echo hi",
        "keys": "Enter",
        "wait_for_ready": True,
    }
    input_timeout = calls[1].extensions["timeout"]
    assert set(input_timeout.values()) == {15.0}

    # Stream saw the new terminal, then the receipt — and nothing else
    # (no session.input.consumed, no status edge: no turn started).
    assert [event["type"] for _, event in published] == [
        "session.resource.created",
        "response.output_item.done",
    ]
    assert published[0][1]["resource"]["id"] == f"terminal_zsh_{session_key}"
    assert published[0][1]["sequence"] == payload["sequence"]

    # Wire-shape pin: ``to_api_dict`` flattens the data payload OVER
    # the item envelope, so the receipt's delivery ``status`` ("ok")
    # occupies the top-level status slot where lifecycle values
    # ("completed") normally live. The web client narrows on exactly
    # this shape — assert it byte-for-byte on the SSE frame AND the
    # GET /items reload.
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    expected = {
        "id": receipt["id"],
        "response_id": receipt["response_id"],
        "type": "terminal_command",
        "status": "ok",
        "kind": "input",
        "input": "echo hi",
        "action": "spawn",
        "terminal_id": f"terminal_zsh_{session_key}",
        "terminal_name": "zsh",
        "session_key": session_key,
        "attempt_id": attempt_id,
        "sequence": payload["sequence"],
        "attempt_fingerprint": item["attempt_fingerprint"],
        "terminal": payload["terminal"],
        "http_status": 200,
    }
    assert receipt == expected
    assert published[1][1]["item"] == expected
    # Synthetic turn id — no real task/response exists for a receipt.
    assert receipt["response_id"].startswith("turn_")

    # The spawn also persisted the resource_event snapshot item the
    # create route writes, so reconnecting clients discover the shell.
    items_resp = await client.get(f"/v1/sessions/{sid}/items")
    resource_events = [i for i in items_resp.json()["data"] if i["type"] == "resource_event"]
    resource_event = next(
        item
        for item in resource_events
        if item["event_type"] == "session.resource.created"
        and item["resource_id"] == f"terminal_zsh_{session_key}"
    )
    assert resource_event["sequence"] == payload["sequence"]
    assert resource_event["resource"]["sequence"] == payload["sequence"]


async def test_shell_command_spawn_real_runner_waits_for_delayed_shell_ack(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The server receipts one command only after a real runner's late ACK."""
    real_bash = shutil.which("bash")
    if shutil.which("tmux") is None or real_bash is None:
        pytest.skip("tmux and bash are required for real shell-command coverage")

    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
    from omnigent.runner import create_runner_app
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals import TerminalRegistry

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    wrapper = _write_delayed_shell_wrapper(
        tmp_path,
        shell_name="bash",
        real_shell=real_bash,
        delay_s=1.35,
    )
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    shell_spec = TerminalEnvSpec(
        command=str(wrapper),
        args=["--noprofile", "--norc"],
        os_env=os_env,
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="real-shell-command",
        os_env=os_env,
        terminals={"bash": shell_spec},
    )
    monkeypatch.setattr(
        sessions_module,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: agent_spec,
    )

    async def _session_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{sid}"
        return httpx.Response(
            200,
            json={"id": sid, "agent_id": agent["id"], "workspace": str(tmp_path)},
        )

    async def _resolve_spec(agent_id: str, session_id: str) -> AgentSpec:
        assert agent_id == agent["id"]
        assert session_id == sid
        return agent_spec

    runner_server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_session_handler),
        base_url="http://server",
    )
    registry = TerminalRegistry()
    runner_app = create_runner_app(
        terminal_registry=registry,
        runner_workspace=tmp_path,
        per_session_workspace=False,
        server_client=runner_server_client,
        spec_resolver=_resolve_spec,
    )
    runner_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runner_app),
        base_url="http://runner",
    )

    async def _real_resource_client(session_id: str) -> httpx.AsyncClient:
        assert session_id == sid
        return runner_client

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _real_resource_client,
    )
    _forbid_agent_turn(monkeypatch)
    marker = f"O{uuid.uuid4().hex[:6]}"
    command = _encoded_output_command(marker)

    try:
        started = time.monotonic()
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="spawn",
                    terminal="bash",
                    command=command,
                ),
            },
        )
        elapsed = time.monotonic() - started

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["item"]["status"] == "ok"
        assert payload["item"]["input"] == command
        assert elapsed > 1.0, "the receipt must wait past the retired 1s readiness gate"

        session_key = payload["item"]["session_key"]
        instance = registry.get(sid, "bash", session_key)
        assert instance is not None
        assert instance.ready_process is None
        assert instance.shell_ready_nonce is not None
        screens: list[str] = []
        for _ in range(40):
            result = await instance.read(scrollback=100)
            screen = str(result.get("screen", ""))
            screens.append(screen)
            if any(line.strip() == marker for line in screen.splitlines()):
                break
            await asyncio.sleep(0.05)
        screen = screens[-1] if screens else ""
        assert screen.count(command) == 1, screen
        assert sum(line.strip() == marker for line in screen.splitlines()) == 1, screen
    finally:
        await registry.shutdown()
        await runner_client.aclose()
        await runner_server_client.aclose()


# ── send: happy path ──────────────────────────────────────


async def test_shell_command_send_routes_into_existing_shell(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send resolves the target terminal and proxies input directly.

    No create call, no resource event — the receipt carries the
    display name/key resolved from the runner's terminal metadata.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve terminal get and input; fail anything else."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(200, json={"status": "sent", "outcome": "sent"})
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    published = _capture_stream(monkeypatch)
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send", terminal_id=terminal_id, command="make test"
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 200, resp.text
    assert "queued" not in resp.json()
    item = resp.json()["item"]
    assert item["action"] == "send"
    assert item["status"] == "ok"
    assert item["input"] == "make test"
    assert item["terminal_id"] == terminal_id
    assert item["terminal_name"] == "zsh"
    assert item["session_key"] == "u-ab12cd"
    assert resp.json()["terminal"]["id"] == terminal_id

    assert [(c.method, c.url.path) for c in calls] == [
        ("GET", f"/v1/sessions/{sid}/resources/terminals/{terminal_id}"),
        ("POST", f"/v1/sessions/{sid}/resources/terminals/{terminal_id}/input"),
    ]
    assert json.loads(calls[1].content) == {
        "attempt_id": item["attempt_id"],
        "text": "make test",
        "keys": "Enter",
        "wait_for_ready": False,
    }
    # Only the receipt was published — no resource event (nothing was
    # created), no input-consumed, no status edge (no agent turn).
    assert [event["type"] for _, event in published] == ["response.output_item.done"]

    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0] == published[0][1]["item"]


# ── malformed payloads: 400, no receipt ───────────────────


@pytest.mark.parametrize(
    "data",
    [
        {"command": "echo hi"},
        {"action": "focus", "command": "echo hi"},
        {"action": "spawn", "command": "echo hi"},
        {"action": "spawn", "terminal": "zsh"},
        {"action": "spawn", "terminal": "zsh", "command": "   "},
        {"action": "spawn", "terminal": "", "command": "echo hi"},
        {"action": "send", "command": "echo hi"},
        {"action": "send", "terminal_id": "", "command": "echo hi"},
    ],
)
async def test_shell_command_rejects_malformed_payload(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
    data: dict[str, Any],
) -> None:
    """Bad shapes 400 at the boundary: no runner call, no receipt.

    Client-side validation would normally stop these; a request that
    still arrives malformed gets a plain 400 — nothing was attempted
    against a shell, so the transcript records nothing.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    fake_runner, calls = _install_fake_runner(
        monkeypatch, lambda request: httpx.Response(200, json={})
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "shell_command", "data": data},
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"
    assert calls == []
    assert await _terminal_command_items(client, sid) == []


@pytest.mark.parametrize(
    "attempt_id",
    # A frozen uuid1 literal (version 1, not the required v4) — must stay a
    # fixed value so xdist workers collect identical parametrize ids.
    [None, "", "not-a-uuid", "1d5a6f7c-4b8e-11ef-9a3d-0242ac130002", "A" * 129, 42],
)
async def test_shell_command_rejects_malformed_attempt_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    attempt_id: object,
) -> None:
    """New shell-command events require one canonical UUIDv4 attempt id."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(500),
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": {
                    "attempt_id": attempt_id,
                    "action": "send",
                    "terminal_id": "terminal_zsh_u-ab12cd",
                    "command": "pwd",
                },
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
    assert calls == []
    assert await _terminal_command_items(client, sid) == []


async def test_shell_command_unknown_event_type_still_rejected(
    client: httpx.AsyncClient,
) -> None:
    """A typo'd event type fails the allow-list, not the dispatcher."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "shell_commands", "data": {}},
    )
    assert resp.status_code == 400
    assert "Unknown event type" in resp.json()["error"]["message"]


# ── spawn gate: undeclared terminal → 400 + error receipt ─


async def test_shell_command_spawn_undeclared_terminal_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """The declared-name gate 400s AND records the attempt.

    Unlike a malformed payload, a well-formed spawn of an undeclared
    type is an execution attempt — the transcript gets an error
    receipt naming the declared set, and the runner is never reached.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    fake_runner, calls = _install_fake_runner(
        monkeypatch, lambda request: httpx.Response(200, json={})
    )
    published = _capture_stream(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="python", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    assert "zsh" in body["error"]["message"]
    # The gate fired BEFORE the proxy — a recorded call here means an
    # unauthorized launch reached the runner despite the 400.
    assert calls == []

    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert receipt["terminal_name"] == "python"
    assert "not declared" in receipt["error"]
    assert receipt["terminal_id"] == f"terminal_python_{receipt['session_key']}"
    # The error receipt is also published live for connected viewers.
    assert [event["type"] for _, event in published] == ["response.output_item.done"]
    assert published[0][1]["item"]["status"] == "error"


async def test_shell_command_spawn_unsupported_readiness_fails_before_create(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared command with no readiness contract never reaches create."""
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.spec.types import AgentSpec

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    spec = AgentSpec(
        spec_version=1,
        terminals={
            "batch": TerminalEnvSpec(
                command=sys.executable,
                args=["-c", "print('not interactive')"],
            )
        },
    )
    monkeypatch.setattr(
        sessions_module,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: spec,
    )
    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(
            500,
            json={"error": {"message": f"unexpected {request.url.path}"}},
        ),
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="spawn",
                    terminal="batch",
                    command="echo never-created",
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"
    assert "readiness contract" in resp.json()["error"]["message"]
    assert calls == []
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "error"
    assert "readiness contract" in receipts[0]["error"]


# ── send failures: dead shell 404 / 409 + error receipt ───


async def test_shell_command_send_unknown_terminal_404_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead/unknown terminal id 404s with an error receipt recorded."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-dead00"

    def _handler(request: httpx.Request) -> httpx.Response:
        """404 the terminal lookup; nothing else should be called."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            not_found = {"code": "not_found", "message": f"Terminal {terminal_id!r} not found"}
            return httpx.Response(404, json={"error": not_found})
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 404, resp.text
    assert [(c.method, c.url.path) for c in calls] == [
        ("GET", f"/v1/sessions/{sid}/resources/terminals/{terminal_id}"),
    ]
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "send"
    assert receipt["terminal_id"] == terminal_id
    assert "not found" in receipt["error"]


async def test_shell_command_send_not_running_409_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved-but-dead shell 409s from input with an error receipt."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Resolve the terminal, then 409 the input call."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            return httpx.Response(
                200, json=_terminal_resource(sid, "zsh", "u-ab12cd", running=False)
            )
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "terminal_not_running",
                        "message": f"Terminal {terminal_id!r} is not running",
                    }
                },
            )
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "terminal_not_running"
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "send"
    # Resolution succeeded before the failure, so the receipt still
    # carries the display fields.
    assert receipt["terminal_name"] == "zsh"
    assert receipt["session_key"] == "u-ab12cd"
    assert receipt["error_code"] == "terminal_not_running"
    assert "not running" in receipt["error"]
    assert len(calls) == 2


async def test_shell_command_capacity_rejection_stays_definite_non_delivery(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1's bounded-ledger 503 is a definite rejection, not unknown."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "idempotency_capacity_exhausted",
                    "message": "terminal input idempotency capacity exhausted",
                }
            },
        )

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id=terminal_id,
                    command="pwd",
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "idempotency_capacity_exhausted"
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "error"
    assert receipts[0]["error_code"] == "idempotency_capacity_exhausted"


# ── runner unreachable: 502 + error receipt ───────────────


async def test_shell_command_spawn_runner_unreachable_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """No reachable runner: the attempt is recorded, then 502 surfaces."""

    async def _no_runner(session_id: str) -> None:
        """Resolve no runner for any session."""
        del session_id

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _no_runner,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "shell_command",
            "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
        },
    )
    assert resp.status_code == 502, resp.text

    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert receipt["terminal_name"] == "zsh"
    # The HTTPException 502 detail is recorded as the cause.
    assert receipt["error"] == "no runner available for resource access"


async def test_shell_command_send_runner_unreachable_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send with no reachable runner records an error receipt too."""

    async def _no_runner(session_id: str) -> None:
        """Resolve no runner for any session."""
        del session_id

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _no_runner,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "shell_command",
            "data": _shell_command_data(
                action="send", terminal_id="terminal_zsh_u-ab12cd", command="pwd"
            ),
        },
    )
    assert resp.status_code == 502, resp.text

    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "send"
    assert receipt["terminal_id"] == "terminal_zsh_u-ab12cd"
    assert receipt["error"] == "no runner available for resource access"


# ── runner router raises OmnigentError (no HTTP client) ───


async def test_shell_command_spawn_router_error_before_create_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """An OmnigentError from the runner router (stage 1) is receipted.

    In production ``client_for_session_resources()`` raises
    ``OmnigentError`` BEFORE any HTTP client exists (session unbound,
    runner offline). The spawn-create stage must persist the error
    receipt and surface the router's status — not skip the receipt
    because no ``HTTPException`` was raised.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    async def _router_offline(session_id: str) -> httpx.AsyncClient:
        """Raise the router's no-runner error on every resolution."""
        del session_id
        raise OmnigentError(
            "No runner bound for session",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _router_offline,
    )
    _forbid_agent_turn(monkeypatch)

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "shell_command",
            "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
        },
    )
    assert resp.status_code == 503, resp.text

    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert receipt["terminal_name"] == "zsh"
    # The key was already generated, so the receipt names the terminal
    # that WOULD have been created.
    assert re.fullmatch(r"u-[0-9a-f]{32}", receipt["session_key"])
    assert receipt["terminal_id"] == f"terminal_zsh_{receipt['session_key']}"
    assert receipt["error"] == "No runner bound for session"


async def test_shell_command_spawn_router_error_after_create_persists_partial_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A runner drop between create and input still leaves a receipt.

    The terminal WAS created (``session.resource.created`` persisted,
    shell discoverable in the rail) but the command never landed — the
    receipt must say exactly that, or the transcript shows nothing
    while a fresh empty shell appears.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the terminal create; input is never reached."""
        body = json.loads(request.content)
        return httpx.Response(
            200, json=_terminal_resource(sid, body["terminal"], body["session_key"])
        )

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    resolutions = 0

    async def _drops_after_create(session_id: str) -> httpx.AsyncClient:
        """Serve the create resolution, then report the runner gone."""
        del session_id
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            return fake_runner
        raise OmnigentError(
            "Runner disconnected",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _drops_after_create,
    )
    published = _capture_stream(monkeypatch)
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 503, resp.text
    assert resolutions == 2

    # The terminal creation went through and was announced/persisted…
    assert [event["type"] for _, event in published] == [
        "session.resource.created",
        "response.output_item.done",
    ]
    items_resp = await client.get(f"/v1/sessions/{sid}/items")
    assert any(
        i["type"] == "resource_event" and i["event_type"] == "session.resource.created"
        for i in items_resp.json()["data"]
    )
    # …and the receipt records the partial outcome explicitly.
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert re.fullmatch(r"u-[0-9a-f]{32}", receipt["session_key"])
    assert receipt["terminal_id"] == f"terminal_zsh_{receipt['session_key']}"
    assert receipt["error"] == (
        "Terminal was created but the command was not delivered: Runner disconnected"
    )
    assert published[1][1]["item"]["id"] == receipt["id"]


async def test_shell_command_send_router_error_after_resolve_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner drop between send's resolve-GET and input is receipted.

    The resolve succeeded, so the receipt carries the shell's display
    fields; the input-stage router error must not bypass persistence.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the terminal resolve; input is never reached."""
        return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    resolutions = 0

    async def _drops_after_resolve(session_id: str) -> httpx.AsyncClient:
        """Serve the resolve resolution, then report the runner gone."""
        del session_id
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            return fake_runner
        raise OmnigentError(
            "Runner disconnected",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _drops_after_resolve,
    )
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 503, resp.text
    assert resolutions == 2

    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "send"
    assert receipt["terminal_id"] == terminal_id
    assert receipt["terminal_name"] == "zsh"
    assert receipt["session_key"] == "u-ab12cd"
    assert receipt["error"] == "Runner disconnected"


# ── malformed runner responses (decode/shape) ─────────────


async def test_shell_command_create_non_json_response_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A non-JSON create response is a receipted 502, not a bare crash.

    A proxy injecting an HTML error page into the terminal-create 200
    raises a decode error inside the proxy helper — the stage wrapper
    must still persist exactly one error receipt.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"<html>not json</html>"),
    )
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert "launching the terminal" in receipt["error"]
    # Create never succeeded, so no partial-outcome prefix.
    assert "was created" not in receipt["error"]


async def test_shell_command_create_non_object_response_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A decoded-but-non-object create body is a receipted 502."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(200, json=["not", "a", "resource"]),
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "error"
    assert "expected a resource object" in receipts[0]["error"]


async def test_shell_command_resolve_non_json_response_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON resolve response is a receipted 502."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"garbage"),
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "send"
    assert receipt["terminal_id"] == terminal_id
    assert "resolving the terminal" in receipt["error"]


async def test_shell_command_resolve_non_object_response_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decoded-but-non-object resolve body is a receipted 502."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(200, json="just a string"),
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "error"
    assert "expected a resource object" in receipts[0]["error"]


async def test_shell_command_input_2xx_malformed_body_is_delivery_unknown(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A 2xx input response without the discriminator is unknown."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    def _handler(request: httpx.Request) -> httpx.Response:
        """Create normally; return non-JSON garbage from input."""
        if request.method == "POST" and request.url.path.endswith("/resources/terminals"):
            body = json.loads(request.content)
            return httpx.Response(
                200, json=_terminal_resource(sid, body["terminal"], body["session_key"])
            )
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(200, content=b"\x00 not json at all")
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 200, resp.text
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "unknown"
    assert receipt["action"] == "spawn"
    assert "error" not in receipt


async def test_shell_command_reads_delivery_unknown_outcome_discriminator(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1's explicit ambiguous outcome becomes an unknown 200 receipt."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        return httpx.Response(200, json={"outcome": "delivery_unknown"})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id=terminal_id,
                    command="deploy",
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 200
    assert resp.json()["item"]["status"] == "unknown"
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "unknown"


async def test_shell_command_input_2xx_non_object_body_is_delivery_unknown(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx non-object body cannot confirm delivery."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Resolve normally; return a JSON array from input."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(200, json=["sent", "maybe"])
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 200, resp.text
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "unknown"
    assert receipts[0]["action"] == "send"


async def test_shell_command_input_error_with_non_json_body_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An input 4xx with an undecodable body still receipts cleanly.

    The tolerant error decode falls back to the generic message rather
    than crashing receipt-less on the malformed error page.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Resolve normally; 409 with an HTML body from input."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(409, content=b"<html>bad gateway page</html>")
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 409, resp.text
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["error"] == "Terminal input failed (runner returned HTTP 409)"


def _fail_resource_event_append(
    monkeypatch: pytest.MonkeyPatch,
    store_error: Exception,
) -> None:
    """Make the REAL store's append raise for resource_event items only.

    Drives ``_publish_and_persist_resource_event``'s actual code path
    (including its internal best-effort suppression), while the
    handler's own ``terminal_command`` receipt appends still succeed —
    replacing the helper with a stub would bypass exactly the
    suppression under test.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param store_error: Exception the append raises for resource
        events, e.g. ``ValueError`` (suppressed by the helper) or
        ``SQLAlchemyError`` (escapes the helper).
    :returns: None.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    real_append = SqlAlchemyConversationStore.append

    def _selective_append(self: Any, conversation_id: str, items: Any) -> Any:
        """Raise for resource_event items; pass everything else through."""
        if any(getattr(item, "type", None) == "resource_event" for item in items):
            raise store_error
        return real_append(self, conversation_id, items)

    monkeypatch.setattr(SqlAlchemyConversationStore, "append", _selective_append)


def _spawn_create_only_handler(sid: str) -> Callable[[httpx.Request], httpx.Response]:
    """Fake-runner handler serving ONLY the terminal create.

    :param sid: Session id for the returned resource.
    :returns: A handler for :func:`_install_fake_runner`.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the terminal create; input must never be reached."""
        if request.method == "POST" and request.url.path.endswith("/resources/terminals"):
            body = json.loads(request.content)
            return httpx.Response(
                200, json=_terminal_resource(sid, body["terminal"], body["session_key"])
            )
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    return _handler


async def test_shell_command_resource_event_suppressed_append_failure_is_receipted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A store failure the shared helper SUPPRESSES still fails the stage.

    ``_publish_and_persist_resource_event`` swallows ValueError from
    the append for its best-effort callers and returns ``False``. The
    record stage must treat that as failure — otherwise the handler
    returns 200 with ``session.resource.created`` never persisted and
    reconnecting clients cannot discover the shell.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    _fail_resource_event_append(monkeypatch, ValueError("append rejected"))
    fake_runner, calls = _install_fake_runner(monkeypatch, _spawn_create_only_handler(sid))
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    # Only the create call — the input stage was never reached.
    assert [(c.method, c.url.path) for c in calls] == [
        ("POST", f"/v1/sessions/{sid}/resources/terminals"),
    ]
    items_resp = await client.get(f"/v1/sessions/{sid}/items")
    all_items = items_resp.json()["data"]
    # The resource event really is missing — and the receipt says so.
    assert not any(item["type"] == "resource_event" for item in all_items)
    receipts = [item for item in all_items if item["type"] == "terminal_command"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert receipt["error"].startswith("Terminal was created but the command was not delivered:")
    assert "session.resource.created was not persisted" in receipt["error"]


async def test_shell_command_resource_event_db_error_is_receipted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A DB-level append error (outside the helper's net) is receipted.

    ``SQLAlchemyError`` is not in the shared helper's suppression
    tuple, so it escapes the helper — the stage wrapper's normalized
    net must catch it and persist the partial receipt instead of
    letting the attempt exit receipt-less.
    """
    from sqlalchemy.exc import SQLAlchemyError

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    _fail_resource_event_append(monkeypatch, SQLAlchemyError("db connection lost"))
    fake_runner, calls = _install_fake_runner(monkeypatch, _spawn_create_only_handler(sid))
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["error"].startswith("Terminal was created but the command was not delivered:")
    assert "recording the new terminal" in receipt["error"]
    assert "db connection lost" in receipt["error"]


# ── malformed create resource fields ──────────────────────


@pytest.mark.parametrize(
    "bad_id",
    [["not", "a", "string"], "", None],
    ids=["list", "empty", "missing"],
)
async def test_shell_command_create_malformed_resource_id_persists_error_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
    bad_id: Any,
) -> None:
    """A create resource with a bad/missing id is a receipted 502.

    The id is validated INSIDE the create stage, before it is
    committed to orchestration state — a malformed value must produce
    an error receipt there, never poison the receipt's own
    construction later (which would lose the receipt entirely).
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve a create whose resource id is malformed."""
        if request.method == "POST" and request.url.path.endswith("/resources/terminals"):
            body = json.loads(request.content)
            resource = _terminal_resource(sid, body["terminal"], body["session_key"])
            if bad_id is None:
                resource.pop("id")
            else:
                resource["id"] = bad_id
            return httpx.Response(200, json=resource)
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 502, resp.text
    # Validation failed inside the create stage: no resource event, no
    # input call.
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "spawn"
    assert "resource id must be a non-empty string" in receipt["error"]
    # The receipt keeps the pre-commit deterministic id — the bad value
    # never reached orchestration state.
    assert receipt["terminal_id"] == f"terminal_zsh_{receipt['session_key']}"


async def test_shell_command_receipt_construction_failure_falls_back_to_minimal_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the full receipt cannot be built, a minimal one still persists.

    ``_receipt_once`` must never lose the receipt to a validation
    error in its own construction — it falls back to a receipt without
    the terminal fields, keeping the action/command/outcome record.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-dead00"

    real_data_cls = events_module.TerminalCommandData

    class _ExplodingReceiptData(real_data_cls):  # type: ignore[valid-type,misc]
        """Receipt payload that fails whenever terminal fields are set."""

        def __init__(self, **kwargs: Any) -> None:
            if kwargs.get("terminal_id") is not None:
                raise ValueError("synthetic receipt construction failure")
            super().__init__(**kwargs)

    monkeypatch.setattr(events_module, "TerminalCommandData", _ExplodingReceiptData)

    def _handler(request: httpx.Request) -> httpx.Response:
        """404 the terminal resolve so the error receipt carries an id."""
        not_found = {"code": "not_found", "message": f"Terminal {terminal_id!r} not found"}
        return httpx.Response(404, json={"error": not_found})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    # The original failure status still surfaces…
    assert resp.status_code == 404, resp.text
    assert len(calls) == 1
    # …and exactly one minimal receipt survived the construction crash.
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["action"] == "send"
    assert "not found" in receipt["error"]
    assert "terminal_id" not in receipt
    assert "terminal_name" not in receipt
    assert "session_key" not in receipt


async def test_shell_command_sse_publish_failure_after_persist_stores_exactly_one_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A post-append SSE failure leaves the durable success unchanged."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Resolve the terminal, then accept the input."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(200, json={"status": "sent", "outcome": "sent"})
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    def _exploding_publish(session_id: str, event: dict[str, Any]) -> None:
        """Fail publication — after the receipt has already persisted."""
        raise RuntimeError("session stream publish exploded")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        _exploding_publish,
    )

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id=terminal_id,
                    command="pwd",
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 200
    assert resp.json()["item"]["status"] == "ok"
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "ok"
    assert receipts[0]["action"] == "send"
    assert receipts[0]["id"] == resp.json()["item_id"]
    assert "receipt SSE publish failed" in caplog.text


async def test_shell_command_explicit_error_survives_sse_publish_failure(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A notification failure cannot replace an explicit runner error."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "terminal_not_running",
                    "message": "terminal stopped before delivery",
                }
            },
        )

    def _exploding_publish(session_id: str, event: dict[str, Any]) -> None:
        raise RuntimeError("session stream publish exploded")

    monkeypatch.setattr(sessions_module.session_stream, "publish", _exploding_publish)
    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id=terminal_id,
                    command="pwd",
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 409
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "error"
    assert "terminal stopped" in receipts[0]["error"]
    assert "receipt SSE publish failed" in caplog.text


async def test_shell_command_response_loss_is_unknown_and_retry_is_idempotent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-side-effect read loss yields one unknown receipt and no resend."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"
    input_side_effects = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal input_side_effects
        if request.method == "GET":
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        input_side_effects += 1
        raise httpx.ReadError("runner response was lost", request=request)

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    attempt_id = str(uuid.uuid4())
    data = _shell_command_data(
        attempt_id=attempt_id,
        action="send",
        terminal_id=terminal_id,
        command="make deploy",
    )
    try:
        first = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "shell_command", "data": data},
        )
        replay = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "shell_command", "data": data},
        )
    finally:
        await fake_runner.aclose()

    assert first.status_code == replay.status_code == 200
    assert first.json()["item"]["status"] == "unknown"
    assert replay.json()["item_id"] == first.json()["item_id"]
    assert input_side_effects == 1
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["attempt_id"] == attempt_id


async def test_shell_command_cancellation_after_dispatch_is_unknown(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation raised by an in-flight runner call is ambiguous delivery."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    class CancellingRunner:
        """Resolve the terminal, then cancel after the input dispatch starts."""

        def __init__(self) -> None:
            self.input_calls = 0

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            return httpx.Response(
                200,
                json=_terminal_resource(sid, "zsh", "u-ab12cd"),
                request=httpx.Request("GET", "http://runner/terminal"),
            )

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            self.input_calls += 1
            raise asyncio.CancelledError

    runner = CancellingRunner()

    async def _resolve_runner(session_id: str) -> CancellingRunner:
        del session_id
        return runner

    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client_for_resource_access",
        _resolve_runner,
    )
    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "shell_command",
            "data": _shell_command_data(
                action="send",
                terminal_id=terminal_id,
                command="make deploy",
            ),
        },
    )

    assert resp.status_code == 200
    assert resp.json()["item"]["status"] == "unknown"
    assert runner.input_calls == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "unknown"


async def test_shell_command_concurrent_duplicate_produces_one_receipt(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent identical events share one execution and durable receipt."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        return httpx.Response(200, json={"status": "sent", "outcome": "sent"})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    data = _shell_command_data(
        action="send",
        terminal_id=terminal_id,
        command="make test",
    )
    try:
        first, second = await asyncio.gather(
            client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "shell_command", "data": data},
            ),
            client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "shell_command", "data": data},
            ),
        )
    finally:
        await fake_runner.aclose()

    assert first.status_code == second.status_code == 200
    assert first.json()["item_id"] == second.json()["item_id"]
    assert len(calls) == 2
    assert len(await _terminal_command_items(client, sid)) == 1


async def test_shell_command_attempt_fingerprint_conflict_is_409(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One attempt id cannot be rebound to a different command."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        return httpx.Response(200, json={"status": "sent", "outcome": "sent"})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    attempt_id = str(uuid.uuid4())
    try:
        first = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    attempt_id=attempt_id,
                    action="send",
                    terminal_id=terminal_id,
                    command="pwd",
                ),
            },
        )
        conflict = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    attempt_id=attempt_id,
                    action="send",
                    terminal_id=terminal_id,
                    command="whoami",
                ),
            },
        )
    finally:
        await fake_runner.aclose()

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "attempt_conflict"
    assert len(calls) == 2
    assert len(await _terminal_command_items(client, sid)) == 1


async def test_shell_command_create_response_loss_reuses_one_terminal(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A lost create response retries the same derived key and terminal."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    live_keys: set[str] = set()
    create_calls = 0
    input_calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls, input_calls
        body = json.loads(request.content)
        if request.url.path.endswith("/resources/terminals"):
            create_calls += 1
            live_keys.add(body["session_key"])
            if create_calls == 1:
                raise httpx.ReadError("create response lost", request=request)
            return httpx.Response(
                200,
                json=_terminal_resource(sid, body["terminal"], body["session_key"]),
            )
        input_calls += 1
        return httpx.Response(200, json={"status": "sent", "outcome": "sent"})

    fake_runner, _calls = _install_fake_runner(monkeypatch, _handler)
    attempt_id = str(uuid.uuid4())
    data = _shell_command_data(
        attempt_id=attempt_id,
        action="spawn",
        terminal="zsh",
        command="echo ready",
    )
    try:
        first = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "shell_command", "data": data},
        )
        replay = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "shell_command", "data": data},
        )
    finally:
        await fake_runner.aclose()

    assert first.status_code == replay.status_code == 200
    assert first.json()["terminal"]["id"] == replay.json()["terminal"]["id"]
    assert live_keys == {sessions_module._shell_spawn_session_key(attempt_id)}
    assert create_calls == 2
    assert input_calls == 1
    assert len(await _terminal_command_items(client, sid)) == 1


async def test_direct_and_bare_create_attempts_use_the_same_stable_key(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """The shared direct-create route is response-loss safe for bare bangs too."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    live_keys: set[str] = set()
    create_calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        create_calls += 1
        body = json.loads(request.content)
        live_keys.add(body["session_key"])
        if create_calls == 1:
            raise httpx.ReadError("create response lost", request=request)
        return httpx.Response(
            200,
            json=_terminal_resource(sid, body["terminal"], body["session_key"]),
        )

    fake_runner, _calls = _install_fake_runner(monkeypatch, _handler)
    attempt_id = str(uuid.uuid4())
    body = {"attempt_id": attempt_id, "terminal": "zsh", "session_key": "u-random"}
    try:
        first = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json=body,
        )
        retry = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json=body,
        )
    finally:
        await fake_runner.aclose()

    assert first.status_code == retry.status_code == 200
    assert first.json()["terminal"]["id"] == retry.json()["terminal"]["id"]
    assert isinstance(first.json()["sequence"], int)
    assert first.json()["sequence"] == first.json()["terminal"]["sequence"]
    assert live_keys == {sessions_module._shell_spawn_session_key(attempt_id)}
    assert create_calls == 3


# ── redirects are not deliveries ──────────────────────────


async def test_shell_command_input_redirect_is_receipted_error(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 307 from the input route is NOT a delivery.

    Only strict 2xx means the command reached the shell; a redirect
    (proxy misconfiguration) must produce an error receipt, not a
    phantom ``ok``.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    terminal_id = "terminal_zsh_u-ab12cd"

    def _handler(request: httpx.Request) -> httpx.Response:
        """Resolve normally; redirect the input call."""
        if request.method == "GET" and request.url.path.endswith(terminal_id):
            return httpx.Response(200, json=_terminal_resource(sid, "zsh", "u-ab12cd"))
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(307, headers={"location": "http://elsewhere/input"})
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="send", terminal_id=terminal_id, command="pwd"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 500, resp.text
    assert len(calls) == 2
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["error"] == "Terminal input failed (runner returned HTTP 307)"


async def test_shell_command_create_redirect_is_receipted_error(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """A 3xx from the terminal create is NOT a launched terminal."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    fake_runner, calls = _install_fake_runner(
        monkeypatch,
        lambda request: httpx.Response(
            302, json={}, headers={"location": "http://elsewhere/terminals"}
        ),
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 500, resp.text
    assert len(calls) == 1
    receipts = await _terminal_command_items(client, sid)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "error"
    assert receipt["error"] == "Terminal launch failed (runner returned HTTP 302)"
    # No partial prefix: the create never succeeded.
    assert "was created" not in receipt["error"]


# ── owner-only gate (multi-user) ──────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App fixture with the permission store enabled.

    Mirrors the shared ``app`` fixture but wires a
    ``SqlAlchemyPermissionStore`` + strict header auth so per-user
    permission levels are enforced on the events route.

    :param runtime_init: Fixture that initializes the runtime with a
        mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled FastAPI app.

    :param auth_app: The permission-enforcing app fixture.
    :param tmp_path: Pytest temporary directory fixture.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    set_harness_process_manager(None)
    await pm.shutdown()


async def test_shell_command_requires_owner_level(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared editor is refused: bang commands are owner-only.

    Keystroke injection matches the write-attach gate — LEVEL_EDIT
    passes the events route's base check but must fail the
    shell-command branch with 403, before any runner call or receipt.
    """
    owner = "bryan@example.com"
    editor = "carol@example.com"
    owner_headers = {"X-Forwarded-Email": owner}

    bundle = build_agent_bundle(name="test-agent")
    create_resp = await auth_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=owner_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    sid = create_resp.json()["session_id"]

    from omnigent.server.auth import LEVEL_EDIT

    grant_resp = await auth_client.put(
        f"/v1/sessions/{sid}/permissions",
        json={"user_id": editor, "level": LEVEL_EDIT},
        headers=owner_headers,
    )
    assert grant_resp.status_code in (200, 201), grant_resp.text

    fake_runner, calls = _install_fake_runner(
        monkeypatch, lambda request: httpx.Response(200, json={})
    )
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id="terminal_zsh_u-ab12cd",
                    command="pwd",
                ),
            },
            headers={"X-Forwarded-Email": editor},
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 403, resp.text
    # The refusal happened before orchestration: no runner traffic and
    # no receipt in the transcript.
    assert calls == []
    assert await _terminal_command_items(auth_client, sid, headers=owner_headers) == []


async def _create_owned_session(auth_client: httpx.AsyncClient, owner: str) -> str:
    """Create a bundled session owned by ``owner`` and return its id.

    :param auth_client: Client wired to the auth-enabled app.
    :param owner: Identity for ``X-Forwarded-Email``.
    :returns: The new session id.
    """
    bundle = build_agent_bundle(name="test-agent")
    resp = await auth_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": owner},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def test_shell_command_owner_success_records_created_by(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    zsh_terminal_spec: None,
) -> None:
    """The session owner may run bang commands; receipts credit them.

    In multi-user mode the receipt's ``created_by`` must carry the
    authenticated owner so shared viewers can see who injected the
    command — the audit-trail half of the owner-only gate.
    """
    owner = "bryan@example.com"
    owner_headers = {"X-Forwarded-Email": owner}
    sid = await _create_owned_session(auth_client, owner)

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve terminal create and input; fail anything else."""
        if request.method == "POST" and request.url.path.endswith("/resources/terminals"):
            body = json.loads(request.content)
            return httpx.Response(
                200, json=_terminal_resource(sid, body["terminal"], body["session_key"])
            )
        if request.method == "POST" and request.url.path.endswith("/input"):
            return httpx.Response(200, json={"status": "sent", "outcome": "sent"})
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url.path}"}})

    fake_runner, calls = _install_fake_runner(monkeypatch, _handler)
    _forbid_agent_turn(monkeypatch)
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(action="spawn", terminal="zsh", command="echo hi"),
            },
            headers=owner_headers,
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["created_by"] == owner
    assert len(calls) == 2

    receipts = await _terminal_command_items(auth_client, sid, headers=owner_headers)
    assert len(receipts) == 1
    assert receipts[0]["created_by"] == owner
    assert receipts[0]["status"] == "ok"


async def test_shell_command_viewer_denied(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only viewer is refused before the owner gate even runs.

    LEVEL_READ fails the events route's base LEVEL_EDIT check — no
    runner traffic, no receipt.
    """
    owner = "bryan@example.com"
    viewer = "victor@example.com"
    owner_headers = {"X-Forwarded-Email": owner}
    sid = await _create_owned_session(auth_client, owner)

    from omnigent.server.auth import LEVEL_READ

    grant_resp = await auth_client.put(
        f"/v1/sessions/{sid}/permissions",
        json={"user_id": viewer, "level": LEVEL_READ},
        headers=owner_headers,
    )
    assert grant_resp.status_code in (200, 201), grant_resp.text

    fake_runner, calls = _install_fake_runner(
        monkeypatch, lambda request: httpx.Response(200, json={})
    )
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id="terminal_zsh_u-ab12cd",
                    command="pwd",
                ),
            },
            headers={"X-Forwarded-Email": viewer},
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 403, resp.text
    assert calls == []
    assert await _terminal_command_items(auth_client, sid, headers=owner_headers) == []


async def test_shell_command_bearer_token_without_identity_denied(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine credential with no user identity cannot run commands.

    In strict header mode an ``Authorization: Bearer`` token carries
    no identity, so the request resolves to no user and is rejected
    before the shell-command branch — no runner traffic, no receipt.
    """
    owner = "bryan@example.com"
    owner_headers = {"X-Forwarded-Email": owner}
    sid = await _create_owned_session(auth_client, owner)

    fake_runner, calls = _install_fake_runner(
        monkeypatch, lambda request: httpx.Response(200, json={})
    )
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "shell_command",
                "data": _shell_command_data(
                    action="send",
                    terminal_id="terminal_zsh_u-ab12cd",
                    command="pwd",
                ),
            },
            headers={"Authorization": "Bearer tok_machine_credential"},
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 401, resp.text
    assert calls == []
    assert await _terminal_command_items(auth_client, sid, headers=owner_headers) == []


# ── ledger single-flight lifecycle (unit-level) ───────────


class _EmptyItemsStore:
    """Conversation store stub with no prior receipts for any attempt."""

    def list_items(self, *args: Any, **kwargs: Any) -> Any:
        """Return an empty terminal_command page so no receipt replays."""
        return SimpleNamespace(data=[], has_more=False, last_id=None)


async def test_shell_command_ledger_retains_cleanup_task_until_done() -> None:
    """An abandoned-owner cleanup task stays strong-referenced until done.

    asyncio keeps only a weak reference to fire-and-forget tasks, so a
    cleanup task that is not retained can be garbage-collected before it
    releases the in-flight slot — wedging every later duplicate on that
    attempt. The ledger must hold the task until it finishes, then drop
    it and leave the slot free.
    """
    ledger = events_module._ShellCommandAttemptLedger(_EmptyItemsStore())

    async def _owner() -> None:
        claimed = await ledger.wait_or_claim(session_id="s", attempt_id="a", fingerprint="fp")
        assert claimed is None  # this task now owns the attempt

    owner = asyncio.create_task(_owner())
    await owner
    # Let the owner's done-callback schedule the retained cleanup task.
    await asyncio.sleep(0)
    assert ledger._cleanup_tasks  # strong reference held while it runs

    await asyncio.gather(*ledger._cleanup_tasks)
    assert not ledger._cleanup_tasks  # reference dropped once finished
    assert not ledger._in_flight  # slot released


async def test_shell_command_ledger_cancelled_owner_gives_waiters_retryable() -> None:
    """A cancelled owner resolves duplicate waiters as retryable, not 500.

    Cancelling the shared future would surface as a ``CancelledError``
    (HTTP 500) on innocent concurrent duplicates. They must instead see
    a retryable ``attempt_abandoned`` so a resend re-claims and runs.
    """
    ledger = events_module._ShellCommandAttemptLedger(_EmptyItemsStore())
    started = asyncio.Event()

    async def _owner() -> None:
        claimed = await ledger.wait_or_claim(session_id="s", attempt_id="a", fingerprint="fp")
        assert claimed is None
        started.set()
        await asyncio.sleep(3600)  # never persists a receipt

    owner = asyncio.create_task(_owner())
    await started.wait()
    entry = ledger._in_flight[("s", "a")]  # future duplicate waiters await

    owner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await owner
    await asyncio.gather(*ledger._cleanup_tasks)

    assert not entry.result.cancelled()
    exc = entry.result.exception()
    assert isinstance(exc, OmnigentError)
    assert exc.code == ErrorCode.ATTEMPT_ABANDONED
    assert not ledger._in_flight  # slot released for a retry
