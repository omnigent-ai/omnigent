"""Fresh and resumed native launches retain the same startup event stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.codex_native import app_server as codex_app
from omnigent.harnesses.codex_native import bridge
from omnigent.harnesses.codex_native import main as codex_main
from omnigent.runner.native import orchestration as native
from omnigent.spec.types import AgentSpec, ExecutorSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["runner", "local-cli"])
@pytest.mark.parametrize("resumed", [False, True], ids=["fresh", "resume"])
async def test_startup_status_is_retained_before_working(
    entrypoint: str,
    resumed: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The initial idle edge clears MCP using the unchanged forwarder policy."""
    session_id = "00000000000040008000000000000001"
    thread_id = "00000000-0000-4000-8000-000000000002"
    loaded = False
    clients: list[StartupClient] = []
    subscribed = asyncio.Event()
    working = asyncio.Event()
    posts: list[dict[str, Any]] = []

    def broadcast(method: str, params: dict[str, Any]) -> None:
        for client in clients:
            if client.connected:
                client.events.put_nowait({"method": method, "params": params})

    def load_thread(*, fresh: bool) -> None:
        nonlocal loaded
        if loaded:
            return
        loaded = True
        if fresh:
            broadcast("thread/started", {"thread": {"id": thread_id}})
        # A status emitted during loading is not replayed to later listeners.
        broadcast("thread/status/changed", {"threadId": thread_id, "status": {"type": "idle"}})

    class StartupClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            self.connected = False
            self.client_name = client_name
            self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            clients.append(self)

        async def connect(self) -> None:
            assert not self.connected
            self.connected = True

        async def close(self) -> None:
            self.connected = False

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert self.connected
            assert method == "thread/resume"
            assert params["threadId"] == thread_id
            load_thread(fresh=False)
            if self.client_name != "omnigent-codex-native-preload":
                subscribed.set()
            return {"result": {"thread": {"id": thread_id, "turns": []}}}

        async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
            while True:
                yield await self.events.get()

    app_server = SimpleNamespace(
        codex_path="/test-bin/codex",
        codex_cli_version=(0, 152, 1),
        env={},
        config_overrides=[],
        start=AsyncMock(),
        close=AsyncMock(),
    )

    def build_server(**kwargs: Any) -> SimpleNamespace:
        app_server.codex_home = kwargs["codex_home"]
        app_server.codex_home.mkdir(parents=True, exist_ok=True)
        (app_server.codex_home / "config.toml").write_text(
            '[mcp_servers.slow]\ncommand = "slow-mcp"\nstartup_timeout_sec = 120\n'
        )
        return app_server

    async def launch_terminal(*_args: Any, **_kwargs: Any) -> Any:
        load_thread(fresh=not resumed)
        if entrypoint == "local-cli":
            return codex_main.LaunchedCodexTerminal(
                terminal_id="terminal_codex_main", tmux_socket=None, tmux_target=None
            )
        return SessionResourceView(
            id="terminal_codex_main", type="terminal", session_id=session_id, name="Codex"
        )

    snapshot: dict[str, Any] = {
        "labels": {codex_main._WRAPPER_LABEL_KEY: codex_main._WRAPPER_LABEL_VALUE},
        "external_session_id": thread_id if resumed else None,
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            event = json.loads(request.content)
            posts.append(event)
            if event == {
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "codex_turn_1"},
            }:
                working.set()
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json=snapshot)

    @contextlib.asynccontextmanager
    async def server_client(*_args: Any, **_kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url="http://startup.test", transport=httpx.MockTransport(handle_request)
        ) as client:
            yield client

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://startup.test")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    monkeypatch.setattr("omnigent.config.load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli_auth.open_server_client", server_client)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: "/test-bin/codex")
    monkeypatch.setattr(
        "omnigent.inner.codex_executor.populate_codex_skills_from_bundle", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reap_codex_native_processes_for_state_dir",
        lambda _path: None,
    )
    monkeypatch.setattr(codex_app, "CodexAppServerClient", StartupClient)
    for module in (codex_app, codex_main):
        monkeypatch.setattr(module, "build_codex_native_server", build_server)
        monkeypatch.setattr(
            module,
            "resolve_native_codex_launch",
            lambda **_kwargs: codex_app.NativeCodexLaunch([], "test-model", None),
        )
    monkeypatch.setattr(codex_main, "_ensure_local_codex_resume_rollout", AsyncMock())
    monkeypatch.setattr(codex_main, "_create_codex_session", AsyncMock(return_value=session_id))
    monkeypatch.setattr(codex_main, "_find_running_codex_terminal", AsyncMock(return_value=None))
    monkeypatch.setattr(codex_main, "_launch_codex_terminal", launch_terminal)
    local_forwarder: asyncio.Task[None] | None = None
    try:
        if entrypoint == "runner":
            async with server_client() as client:
                await native._auto_create_codex_terminal(
                    session_id,
                    SimpleNamespace(launch_auxiliary_terminal=launch_terminal),  # type: ignore[arg-type]
                    lambda _sid, _event: None,
                    agent_spec=AgentSpec(
                        spec_version=1,
                        name="codex",
                        executor=ExecutorSpec(config={"harness": "codex-native"}),
                    ),
                    server_client=client,
                )
        else:
            prepared = await codex_main._prepare_codex_terminal(
                base_url="http://startup.test",
                headers={},
                session_id=session_id if resumed else None,
                runner_id=None,
                session_bundle=b"test bundle",
                codex_args=(),
                command="/test-bin/codex",
                model="test-model",
            )
            if not resumed:
                prepared.thread_id = await codex_main._initialize_fresh_terminal_thread(
                    base_url="http://startup.test", headers={}, prepared=prepared
                )
            local_forwarder = codex_main._start_codex_forwarder(
                base_url="http://startup.test", headers={}, prepared=prepared, auth=None
            )
        await asyncio.wait_for(subscribed.wait(), timeout=2)
        broadcast("turn/started", {"threadId": thread_id, "turn": {"id": "turn_1"}})
        await asyncio.wait_for(working.wait(), timeout=2)

        mcp_updates = [
            event["data"]["servers"] for event in posts if event["type"] == "external_mcp_startup"
        ]
        assert mcp_updates == [
            {"slow": {"status": "starting", "error": None}},
            {},
        ], (
            "startup status must survive the listener handoff, "
            "without waiting for MCP or model output"
        )
        assert posts[-1]["type"] == "external_session_status"
    finally:
        await native._cancel_auto_forwarder_task(session_id)
        if local_forwarder is not None:
            local_forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await local_forwarder
        for client in clients:
            await client.close()
