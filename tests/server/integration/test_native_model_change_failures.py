"""Native model refusals propagate through the runner to the public session API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.inner.databricks_executor import DatabricksAuthError
from omnigent.runner import create_runner_app, native_controls
from omnigent.runner._entry import _RunnerDatabricksAuth
from omnigent.runner.transports.ws_tunnel.frames import (
    Frame,
    HelloFrame,
    RequestFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.runtime import session_stream
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _spec_resolver_returning,
)
from tests.runner.helpers import make_test_terminal_instance
from tests.runner.native_helpers import _harness_spec
from tests.server.helpers import create_test_agent
from tests.server.integration.test_codex_model_change_after_idle_reap import _LoopbackCodex

_QUESTION = """\
────────────────────────────────────────────────────────────────────────────────
Which implementation should we use?
❯ 1. Keep the current implementation
  2. Try the alternative
  3. Type something.
────────────────────────────────────────────────────────────────────────────────
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
_PERMISSION = """\
╭──────────────────────────────────────────────────────────╮
│ Bash command                                             │
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. Yes, and don't ask again for this command             │
│   3. No, and tell Claude what to do differently (esc)      │
╰──────────────────────────────────────────────────────────╯
"""


@dataclass
class _Credentials:
    available: bool = True
    failures: int = 0

    def token(self) -> str:
        if not self.available:
            self.failures += 1
            raise DatabricksAuthError(
                "Host credential service could not supply workspace credentials"
            )
        return "test-token"


@dataclass
class _Tunnel:
    registry: TunnelRegistry
    session: RunnerSession
    frames: list[Frame] = field(default_factory=list)
    dispatch_errors: list[Exception] = field(default_factory=list)


@asynccontextmanager
async def _tunneled_runner(app: FastAPI) -> AsyncIterator[tuple[httpx.AsyncClient, _Tunnel]]:
    """Use real tunnel framing/dispatch with an in-memory socket boundary."""
    registry = TunnelRegistry()
    session = registry.register(
        "runner_connected",
        AsyncMock(),
        HelloFrame(runner_version="0.1.0-test", frame_protocol_version=1),
    )
    tunnel = _Tunnel(registry, session)

    async def send_response(data: str) -> None:
        frame = decode_frame(data)
        tunnel.frames.append(frame)
        assert registry.route_response_frame(session.runner_id, frame, session=session)

    async def dispatch_requests() -> None:
        while True:
            outbound = await session.outbound_queue.get()
            if outbound is None:
                return
            frame = decode_frame(outbound.data)
            assert isinstance(frame, RequestFrame)
            tunnel.frames.append(frame)
            try:
                await dispatch_via_asgi(app, frame, send_response)
            except Exception as exc:
                # The serving loop records dispatch failures and keeps the tunnel alive.
                tunnel.dispatch_errors.append(exc)

    dispatch = asyncio.create_task(dispatch_requests())
    try:
        async with httpx.AsyncClient(
            transport=WSTunnelTransport(registry, session.runner_id), base_url="http://runner"
        ) as runner:
            yield runner, tunnel
    finally:
        registry.deregister(session.runner_id, session=session)
        dispatch.cancel()
        with suppress(asyncio.CancelledError):
            await dispatch


@dataclass
class _ClaudePane:
    bridge_dir: Path
    confirm: bool = True
    commands: list[str] = field(default_factory=list)

    def report_model(self, model: str) -> None:
        (self.bridge_dir / "context.json").write_text(json.dumps({"model": model}))

    def inject(self, bridge_dir: Path, *, command: str, **_kwargs: Any) -> None:
        assert bridge_dir == self.bridge_dir
        self.commands.append(command)
        if self.confirm:
            self.report_model(command.removeprefix("/model "))


@dataclass
class _NativeSession:
    session_id: str
    store: SqlAlchemyConversationStore
    previous_model: str
    requested_model: str
    runner: httpx.AsyncClient
    tunnel: _Tunnel
    credentials: _Credentials
    events: list[dict[str, Any]]
    runner_replies: list[httpx.Response]
    claude: _ClaudePane | None = None
    codex: _LoopbackCodex | None = None

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [event["error"] for event in self.events if event["type"] == "response.error"]

    def assert_model(self, model: str) -> None:
        saved = self.store.get_conversation(self.session_id)
        assert saved.model_override == model
        assert saved.reasoning_effort == "high"
        if self.claude is not None:
            assert claude_bridge.read_claude_status_model(self.claude.bridge_dir) == model
        if self.codex is not None:
            assert codex_bridge.read_codex_config_model(self.codex.bridge_dir) == model
            assert self.codex.loaded_threads[self.codex.thread_id] == {
                "model": model,
                "effort": "high",
            }

    def assert_refused(self, response: httpx.Response, *, runner_status: int = 503) -> None:
        assert response.status_code == 503, response.text
        assert len(self.runner_replies) == 1
        assert self.runner_replies[0].status_code == runner_status
        assert len(self.errors) == 1
        assert self.errors[0]["code"] == "model_change_not_applied"
        assert self.requested_model in self.errors[0]["message"]
        assert f"status {runner_status}" in self.errors[0]["message"]
        self.assert_model(self.previous_model)


@pytest.fixture
async def native_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[_NativeSession]:
    harness = getattr(request, "param", "claude-native")
    is_claude = harness == "claude-native"
    previous_model = "claude-opus-4-6" if is_claude else "gpt-5.4"
    requested_model = "claude-opus-4-7" if is_claude else "gpt-6-sol"
    wrapper = "claude-code-native-ui" if is_claude else "codex-native-ui"
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "labels": {"omnigent.wrapper": wrapper},
            "model_override": previous_model,
            "reasoning_effort": "high",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    host = HostStore(db_uri).upsert_on_connect(
        "6b9c07bfb42f687d53af44f018adebec", "laptop", "owner@example.com"
    )
    store.set_host_id(session_id, host_id=host.host_id, workspace=str(tmp_path))
    store.replace_runner_id(session_id, "runner_connected")

    terminals = TerminalRegistry()
    terminal_name = "claude" if is_claude else "codex"
    terminal = make_test_terminal_instance(terminal_name, "main", tmp_path)
    monkeypatch.setattr(terminal, "is_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(terminal, "_tmux", AsyncMock())
    terminals._by_conversation[session_id] = {(terminal_name, "main"): terminal}
    claude = None
    codex = None
    if is_claude:
        monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "claude")
        bridge_dir = claude_bridge.bridge_dir_for_bridge_id(session_id)
        bridge_dir.mkdir(parents=True)
        (bridge_dir / "bridge.json").write_text(
            json.dumps(
                {
                    "active_session_id": session_id,
                    "model_picker_values": [previous_model, requested_model],
                }
            )
        )
        (bridge_dir / "tmux.json").write_text(
            json.dumps({"socket_path": str(tmp_path / "tmux.sock"), "tmux_target": "main"})
        )
        claude = _ClaudePane(bridge_dir)
        claude.report_model(previous_model)
        monkeypatch.setattr(claude_native, "resolve_native_claude_config", lambda **_kwargs: None)
        monkeypatch.setattr(claude_native, "stored_claude_catalog_rows", lambda _config: None)
        monkeypatch.setattr(claude_native, "claude_launch_catalog", AsyncMock(return_value=[]))
        monkeypatch.setattr(claude_bridge, "confirm_dialog_if_open", lambda *_a, **_k: False)
        monkeypatch.setattr(native_controls, "_CLAUDE_MODEL_CONFIRM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(native_controls, "_CLAUDE_MODEL_CONFIRM_POLL_S", 0.005)
    else:
        monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", tmp_path / "codex")
        monkeypatch.setattr(app_server, "_effort_catalog_cache", {})
        bridge_dir = codex_bridge.bridge_dir_for_bridge_id(session_id)
        codex_home = codex_bridge.codex_home_for_bridge_dir(bridge_dir)
        codex_home.mkdir(parents=True)
        (codex_home / "config.toml").write_text(
            f'model = "{previous_model}"\nmodel_reasoning_effort = "high"\n'
        )
        codex = _LoopbackCodex(bridge_dir, session_id)
        await codex.start()

    events: list[dict[str, Any]] = []
    publish = session_stream.publish

    def capture_event(sid: str, event: dict[str, Any]) -> None:
        if sid == session_id:
            events.append(event)
        publish(sid, event)

    monkeypatch.setattr(session_stream, "publish", capture_event)
    replies: list[httpx.Response] = []

    async def capture_reply(response: httpx.Response) -> None:
        if response.request.url.path.endswith("/events"):
            await response.aread()
            replies.append(response)

    credentials = _Credentials()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            auth=_RunnerDatabricksAuth(credentials.token, server_url="http://test"),
        ) as callbacks:
            runner_app = create_runner_app(
                process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
                spec_resolver=await _spec_resolver_returning(
                    _harness_spec(harness, model=previous_model)
                ),
                server_client=callbacks,
                terminal_registry=terminals,
            )
            runner_app.state.native_pane_status[session_id] = "idle"
            async with _tunneled_runner(runner_app) as (runner, tunnel):
                runner.event_hooks["response"].append(capture_reply)
                initialized = await asyncio.wait_for(
                    runner.post(
                        "/v1/sessions", json={"session_id": session_id, "agent_id": agent["id"]}
                    ),
                    timeout=5,
                )
                assert initialized.status_code == 201, initialized.text
                monkeypatch.setattr(
                    "omnigent.server.routes.sessions._get_runner_client",
                    AsyncMock(return_value=runner),
                )
                monkeypatch.setattr(
                    routes_core,
                    "ensure_runner_connected",
                    AsyncMock(return_value=(runner, store.get_conversation(session_id))),
                )
                monkeypatch.setattr(routes_core, "_ensure_runner_relay_ready", AsyncMock())
                yield _NativeSession(
                    session_id,
                    store,
                    previous_model,
                    requested_model,
                    runner,
                    tunnel,
                    credentials,
                    events,
                    replies,
                    claude,
                    codex,
                )
    finally:
        await terminals.cleanup_conversation(session_id)
        if codex is not None:
            await codex.close()


async def _pick_model(client: httpx.AsyncClient, session: _NativeSession) -> httpx.Response:
    return await asyncio.wait_for(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": session.requested_model}
        ),
        timeout=5,
    )


@pytest.mark.parametrize("confirmed", [False, True], ids=["timeout", "confirmed"])
async def test_claude_model_confirmation_controls_public_patch_and_rollback(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    confirmed: bool,
) -> None:
    session = native_session
    assert session.claude is not None
    session.claude.confirm = confirmed
    monkeypatch.setattr(claude_bridge, "inject_slash_command", session.claude.inject)

    response = await _pick_model(client, session)

    assert session.claude.commands == [f"/model {session.requested_model}"]
    assert session.tunnel.dispatch_errors == []
    if confirmed:
        assert response.status_code == 200, response.text
        assert session.runner_replies[0].status_code == 204
        session.assert_model(session.requested_model)
        assert session.errors == []
    else:
        session.assert_refused(response)
        refused = session.runner_replies[0].json()
        assert refused["error"] == "claude_native_model_unconfirmed"
        assert "did not confirm" in refused["detail"]
        assert "did not confirm" in session.errors[0]["message"]


@pytest.mark.parametrize("screen", [_QUESTION, _PERMISSION], ids=["question", "permission"])
async def test_claude_pending_prompt_blocks_model_pick_without_answering_it(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    screen: str,
) -> None:
    session = native_session
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(claude_bridge, "_capture_pane", lambda *_args: screen)
    monkeypatch.setattr(claude_bridge, "_run_tmux", lambda *args: sent.append(args))

    response = await _pick_model(client, session)

    session.assert_refused(response)
    assert session.runner_replies[0].json()["error"] == "claude_native_model_failed"
    assert any(
        record.exc_info and isinstance(record.exc_info[1], claude_bridge.ClaudeUserPromptPending)
        for record in caplog.records
    )
    assert sent == []
    assert session.tunnel.dispatch_errors == []


@pytest.mark.parametrize("native_session", ["codex-native"], indirect=True)
async def test_credential_failure_mirroring_applied_codex_effort_keeps_model_change(
    client: httpx.AsyncClient, native_session: _NativeSession, caplog: pytest.LogCaptureFixture
) -> None:
    session = native_session
    session.credentials.available = False
    session.tunnel.frames.clear()

    response = await _pick_model(client, session)

    assert response.status_code == 200, response.text
    assert session.runner_replies[0].status_code == 204
    session.assert_model(session.requested_model)
    assert session.errors == []
    assert session.credentials.failures >= 2
    assert any("Failed to resolve session labels" in record.message for record in caplog.records)
    assert any(
        "Could not mirror applied Codex effort" in record.message for record in caplog.records
    )
    requests = [frame for frame in session.tunnel.frames if isinstance(frame, RequestFrame)]
    heads = [frame for frame in session.tunnel.frames if isinstance(frame, ResponseHeadFrame)]
    ends = [frame for frame in session.tunnel.frames if isinstance(frame, ResponseEndFrame)]
    assert len(requests) == 1
    assert requests[0].path == f"/v1/sessions/{session.session_id}/events"
    assert requests[0].method == "POST"
    assert len(heads) == 1 and heads[0].status == 204
    assert len(ends) == 1 and ends[0].error is None
    assert ends[0].id == heads[0].id == requests[0].id
    assert session.tunnel.session.in_flight == {}
    assert session.tunnel.dispatch_errors == []
    health = await asyncio.wait_for(session.runner.get("/health"), timeout=5)
    assert health.status_code == 200
