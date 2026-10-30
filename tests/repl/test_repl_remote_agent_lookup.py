"""REPL agent lookup on a remote server: an unknown agent name is reported safely."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from omnigent_client import OmnigentError, RegisteredAgent, SessionsNamespace
from omnigent_client._sessions import Session
from omnigent_ui_sdk import TerminalHost

import omnigent.host.identity as identity
from omnigent import session_host
from omnigent.repl._repl import _SessionsChatReplAdapter
from omnigent.session_host import route_session_request


@pytest.mark.asyncio
async def test_remote_repl_unknown_agent_error_is_escaped() -> None:
    """A missing agent name keeps its LookupError, with control characters escaped."""

    async def _resolve_agent(name: str) -> object:
        raise LookupError(f"No agent named {name!r}. Available: evil\x1b[2J")

    adapter = _SessionsChatReplAdapter(
        SimpleNamespace(sessions=SimpleNamespace(resolve_agent=_resolve_agent)),  # type: ignore[arg-type]
        "evil\x1b[2J",
    )

    # The REPL renders exceptions at its UI boundary; keep the LookupError contract.
    with pytest.raises(LookupError) as excinfo:
        await adapter._create_session_from_registered_agent(pending_debug=False)

    assert "\x1b" not in str(excinfo.value) and "\\x1b" in str(excinfo.value)


@pytest.mark.asyncio
async def test_remote_repl_preserves_selected_catalog_identity() -> None:
    """Session creation and runner selection do not resolve a selected name again."""
    created: list[str] = []
    harnesses: list[str | None] = []

    async def _unexpected_lookup(_name: str) -> RegisteredAgent:
        raise AssertionError("the selected catalog entry already has an identity")

    async def _resolve_runner(*, harness: str | None, **_kwargs: object) -> str:
        harnesses.append(harness)
        return "runner_existing"

    async def _create(agent_id: str, **_kwargs: object) -> Session:
        created.append(agent_id)
        return Session(id="conv_new", agent_id=agent_id, status="idle", created_at=0)

    client = SimpleNamespace(
        sessions=SimpleNamespace(
            resolve_agent=_unexpected_lookup,
            resolve_online_runner=_resolve_runner,
            create_from_agent_id=_create,
        )
    )
    adapter = _SessionsChatReplAdapter(
        client,  # type: ignore[arg-type]
        "reviewer",
        registered_agent=RegisteredAgent(id="ag_selected", harness="openai-agents"),
    )

    await adapter._create_session_from_registered_agent(pending_debug=False)

    assert created == ["ag_selected"]
    assert harnesses == ["openai-agents"]


@asynccontextmanager
async def _remote_adapter(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    session_id: str | None = None,
    agent_name: str = "reviewer",
) -> AsyncIterator[_SessionsChatReplAdapter]:
    """Use real SDK requests and snapshots; suppress only the background stream."""
    monkeypatch.setattr(session_host, "_session_hosts", {})

    async def _stream() -> None:
        return

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [route_session_request]},
    ) as http:
        adapter = _SessionsChatReplAdapter(
            SimpleNamespace(sessions=SessionsNamespace(http, "http://srv")),  # type: ignore[arg-type]
            agent_name,
            session_id=session_id,
            registered_agent=RegisteredAgent(id="ag_selected", harness="claude"),
        )
        monkeypatch.setattr(adapter, "_stream_pump", _stream)
        try:
            yield adapter
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["online-runner", "mismatched-runner", "resume", "no-host", "launch-error"]
)
async def test_remote_repl_runner_startup_uses_sdk_host_policy(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Adopt a matching runner, launch for a mismatch, or resume without discovery."""
    monkeypatch.setattr(
        identity, "load_host_identity_if_present", lambda: SimpleNamespace(host_id="host_local")
    )
    requests: list[str] = []
    host = TerminalHost(model_name="test", history_file=tmp_path / "history")
    snapshot = {
        "id": "conv_remote",
        "agent_id": "ag_selected",
        "agent_name": "uploaded-only",
        "runner_id": None if scenario == "online-runner" else "runner_existing",
        "harness": "claude-sdk",
        "status": "idle",
        "created_at": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(f"{request.method} {path}")
        if path == "/v1/runners":
            runners = (
                []
                if scenario == "no-host"
                else [
                    {
                        "runner_id": "runner_existing",
                        "online": True,
                        "harnesses": [
                            "codex"
                            if scenario in {"mismatched-runner", "launch-error"}
                            else "claude-sdk"
                        ],
                    }
                ]
            )
            return httpx.Response(200, json={"data": runners})
        if path == "/v1/hosts":
            assert scenario in {"no-host", "mismatched-runner", "launch-error"}
            hosts = (
                [] if scenario == "no-host" else [{"host_id": "host_local", "status": "online"}]
            )
            return httpx.Response(200, json={"hosts": hosts})
        if path == "/v1/sessions" and request.method == "POST":
            expected = {"agent_id": "ag_selected", "workspace": os.getcwd()}
            if scenario in {"mismatched-runner", "launch-error"}:
                expected["host_id"] = "host_local"
                assert host._live_line_count > 0
                assert "Waiting for this machine to start your agent..." in capsys.readouterr().out
            assert json.loads(request.content) == expected
            if scenario == "launch-error":
                return httpx.Response(502, json={"error": {"message": "Host launch refused"}})
            return httpx.Response(201, json=snapshot)
        if path == "/v1/sessions/conv_remote":
            if request.method == "PATCH":
                assert scenario == "online-runner"
                assert json.loads(request.content) == {"runner_id": "runner_existing"}
                snapshot["runner_id"] = "runner_existing"
            else:
                assert scenario == "resume" and request.method == "GET"
            return httpx.Response(200, json=snapshot)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with _remote_adapter(
        monkeypatch, handler, session_id="conv_remote" if scenario == "resume" else None
    ) as adapter:
        monkeypatch.setattr(adapter, "_host", host)
        if scenario == "no-host":
            with pytest.raises(RuntimeError, match="no online runner"):
                await adapter._ensure_session()
            assert requests == ["GET /v1/runners", "GET /v1/hosts"]
            assert adapter.session_id is None
        elif scenario == "launch-error":
            with pytest.raises(OmnigentError, match="Host launch refused"):
                await adapter._ensure_session()
            assert adapter.session_id is None
        else:
            assert await adapter._ensure_session() == "conv_remote"
            assert adapter._runner_id == "runner_existing"
            if scenario == "resume":
                assert requests == ["GET /v1/sessions/conv_remote"]
                assert adapter._agent_name == "uploaded-only"
        assert host._live_line_count == 0
        assert "Waiting for this machine" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_name", "harness"), [(None, "claude-sdk"), ("shadowed", "claude-sdk"), (None, None)]
)
async def test_new_session_uses_snapshot_identity_without_a_runner(
    monkeypatch: pytest.MonkeyPatch, agent_name: str | None, harness: str | None
) -> None:
    """An unnamed or shadowed session agent survives /new without a catalog lookup."""
    snapshot = {
        "id": "conv_source",
        "agent_id": "ag_uploaded",
        "agent_name": agent_name,
        "harness": harness,
        "runner_id": None,
        "status": "idle",
        "created_at": 0,
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(f"{request.method} {path}")
        if path == "/v1/sessions/conv_source":
            return httpx.Response(200, json=snapshot)
        if path == "/v1/runners":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"runner_id": "runner_wrong", "online": True, "harnesses": ["codex"]},
                        {"runner_id": "runner_right", "online": True, "harnesses": ["claude-sdk"]},
                    ]
                },
            )
        if path == "/v1/sessions":
            assert json.loads(request.content)["agent_id"] == "ag_uploaded"
            return httpx.Response(201, json={**snapshot, "id": "conv_new"})
        if path == "/v1/sessions/conv_new":
            assert json.loads(request.content) == {"runner_id": "runner_right"}
            return httpx.Response(
                200,
                json={
                    **snapshot,
                    "id": "conv_new",
                    "runner_id": "runner_right",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with _remote_adapter(monkeypatch, handler, agent_name="agent") as adapter:
        await adapter.view_session("conv_source", read_only=True)
        await adapter.start_new_conversation()
        if harness is None:
            with pytest.raises(RuntimeError, match="does not report its harness"):
                await adapter._ensure_session()
            assert requests == ["GET /v1/sessions/conv_source", "PATCH /v1/sessions/conv_source"]
        else:
            assert await adapter._ensure_session() == "conv_new"
            assert adapter._runner_id == "runner_right"


@pytest.mark.asyncio
async def test_new_session_does_not_inherit_previous_launch_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launch, /new, decode the new host, and route a subsequent request to it."""
    monkeypatch.setattr(
        identity, "load_host_identity_if_present", lambda: SimpleNamespace(host_id="host_local")
    )
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "host_unrelated")
    first = {
        "id": "conv_first",
        "agent_id": "ag_selected",
        "runner_id": "runner_launched",
        "host_id": "host_local",
        "harness": "claude-sdk",
        "status": "idle",
        "created_at": 0,
    }
    second = {**first, "id": "conv_next", "host_id": "host_next"}
    responses = iter(
        [
            ("GET", "/v1/runners", None, {"data": []}),
            ("GET", "/v1/hosts", None, {"hosts": [{"host_id": "host_local", "status": "online"}]}),
            ("POST", "/v1/sessions", "host_local", first),
            ("PATCH", "/v1/sessions/conv_first", "host_local", first),
            ("POST", "/v1/sessions", "host_unrelated", second),
            ("GET", "/v1/sessions/conv_next", "host_next", second),
        ]
    )
    created: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method, path, host, body = next(responses)
        assert (request.method, request.url.path) == (method, path)
        assert request.headers.get("X-Databricks-Omnigent-Slice-Key") == host
        if method == "POST":
            created.append(json.loads(request.content))
        if method == "PATCH":
            assert json.loads(request.content) == {"runner_id": ""}
        return httpx.Response(200, json=body)

    async with _remote_adapter(monkeypatch, handler) as adapter:
        assert await adapter._ensure_session() == "conv_first"
        await adapter.start_new_conversation()
        assert await adapter._ensure_session() == "conv_next"
        await adapter._client.sessions.get("conv_next")
        assert created == [
            {"agent_id": "ag_selected", "workspace": os.getcwd(), "host_id": "host_local"},
            {"agent_id": "ag_selected", "workspace": os.getcwd()},
        ]
        assert next(responses, None) is None
