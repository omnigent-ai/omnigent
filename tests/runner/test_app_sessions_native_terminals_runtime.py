"""Tests for Codex, Claude, and Antigravity terminal runtime behavior."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from omnigent import (
    codex_native_bridge,
    cursor_native_bridge,
    kiro_native_bridge,
)
from omnigent.antigravity_native_bridge import (
    is_placeholder_conversation_id as bridge_mod_is_placeholder,
)
from omnigent.claude_native_bridge import (
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    ResolvedSpec,
    _auto_create_antigravity_terminal,
    _auto_create_codex_terminal,
)
from omnigent.runner.resource_registry import (
    ANTIGRAVITY_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_create_session_threads_cursor_bridge_dir_without_dead_guard_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor-native session pre-spawn emits only the bridge dir env.

    This locks the runner boundary, not just the bridge helper: the
    session-creation route must pass a cursor-native bridge dir into the
    harness process manager, while omitting the unread request-session-id
    guard env. Inject/stop/interrupt paths use the bridge dir and tmux target;
    none consume an active-session guard.
    """
    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")
    spec = AgentSpec(
        spec_version=1,
        name="cursor-native-agent",
        executor=ExecutorSpec(
            config={"harness": "cursor-native", "model": "cursor-default"},
        ),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "0229f28e408c700b084b2a2e265f9b3c",
                "agent_id": "209bc0df7e730c9f33fcda57504c2623",
            },
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == "0229f28e408c700b084b2a2e265f9b3c"
    assert harness == "cursor-native"
    assert env == {
        cursor_native_bridge.BRIDGE_DIR_ENV_VAR: str(
            cursor_native_bridge.bridge_dir_for_session_id("0229f28e408c700b084b2a2e265f9b3c")
        )
    }
    assert "HARNESS_CURSOR_NATIVE_REQUEST_SESSION_ID" not in env


@pytest.mark.asyncio
async def test_create_session_threads_kiro_bridge_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiro-native session pre-spawn emits the Kiro bridge dir env."""
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    spec = AgentSpec(
        spec_version=1,
        name="kiro-native-agent",
        executor=ExecutorSpec(
            config={"harness": "kiro-native", "model": "auto"},
        ),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=tmp_path)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "823dbd1aab969b5a813fac59bb977a77",
                "agent_id": "2c515637c67d0717ad0bebc2747b71bc",
            },
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == "823dbd1aab969b5a813fac59bb977a77"
    assert harness == "kiro-native"
    assert env == {
        kiro_native_bridge.KIRO_NATIVE_BRIDGE_DIR_ENV_VAR: str(
            kiro_native_bridge.bridge_dir_for_session_id("823dbd1aab969b5a813fac59bb977a77")
        )
    }


@pytest.mark.asyncio
async def test_create_session_threads_workspace_to_pi_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi pre-spawn receives the session workspace, not the bundle dir."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    session_id = "18f39ab73f49285e4dab0c80ff7b8455"
    runner_workspace = tmp_path / "runner-workspace"
    runner_workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "86c64e0f9cab937bd04c79b9957dd55a-v1"
    bundle_dir.mkdir(parents=True)
    worktree = tmp_path / "repo-worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    spec = AgentSpec(
        spec_version=1,
        name="pi-worktree-agent",
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "pi"}),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    class _SessionWorkspaceClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": session_id,
                        "agent_id": "86c64e0f9cab937bd04c79b9957dd55a",
                        "created_at": 1.0,
                        "workspace": str(worktree),
                    },
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionWorkspaceClient(),  # type: ignore[arg-type]
        runner_workspace=runner_workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "86c64e0f9cab937bd04c79b9957dd55a"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == session_id
    assert harness == "pi"
    assert env is not None
    assert env["HARNESS_PI_CWD"] == str(worktree.resolve())
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


@pytest.mark.parametrize("workspace_value", [None, "   "])
@pytest.mark.asyncio
async def test_create_session_threads_runner_workspace_to_pi_cwd_when_session_workspace_missing(
    workspace_value: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi pre-spawn falls back to runner workspace when session workspace is empty."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    session_id = "3f1d20a97a7d0ba93e02cf17aeb92367"
    runner_workspace = tmp_path / "runner-workspace"
    runner_workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "86c64e0f9cab937bd04c79b9957dd55a-v1"
    bundle_dir.mkdir(parents=True)
    spec = AgentSpec(
        spec_version=1,
        name="pi-runner-workspace-agent",
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "pi"}),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    class _SessionWorkspaceClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": session_id,
                        "agent_id": "86c64e0f9cab937bd04c79b9957dd55a",
                        "created_at": 1.0,
                        "workspace": workspace_value,
                    },
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionWorkspaceClient(),  # type: ignore[arg-type]
        runner_workspace=runner_workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "86c64e0f9cab937bd04c79b9957dd55a"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == session_id
    assert harness == "pi"
    assert env is not None
    assert env["HARNESS_PI_CWD"] == str(runner_workspace.resolve())
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


@pytest.mark.parametrize(
    ("session_json", "expected"),
    [
        # Host-spawned (web UI): bound to a host -> auto-create.
        (
            {
                "id": "8af356d908005a65f872c246158c6293",
                "host_id": "abb32306b80732bdfa6153b2f5f6eb92",
            },
            True,
        ),
        # Top-level CLI session: no host_id and no parent, but the runner
        # still owns the Codex app-server and terminal.
        ({"id": "8af356d908005a65f872c246158c6293", "host_id": None}, True),
        ({"id": "8af356d908005a65f872c246158c6293"}, True),
    ],
)
@pytest.mark.asyncio
async def test_codex_top_level_session_needs_runner_terminal_for_all_session_shapes(
    session_json: dict[str, object], expected: bool
) -> None:
    """
    Codex-native terminal auto-create is runner-owned for every session.

    A top-level CLI session has no ``host_id`` and no parent, but the
    runner must still create the app-server and TUI terminal. If the old
    host-id gate returns ``False`` here, ``omnigent codex`` falls back to
    a CLI-owned app-server.
    """
    from omnigent.runner.app import _codex_session_needs_runner_terminal

    class _Client:
        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            return httpx.Response(200, json=session_json, request=httpx.Request("GET", url))

    assert (
        await _codex_session_needs_runner_terminal(_Client(), "8af356d908005a65f872c246158c6293")
        is expected
    )


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_uses_persisted_resume_launch_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Runner-owned Codex launch consumes persisted args and thread id.

    The CLI now persists launch intent and asks the runner to ensure the
    terminal. This test exercises the runner helper directly: it must read
    ``terminal_launch_args`` and ``external_session_id`` from the Omnigent snapshot,
    start the app-server itself, launch the TUI as ``codex ... resume
    --remote <runner-ws> <thread>``, and run the known-thread forwarder. If
    this regresses, the CLI falls back into split ownership or loses user
    pass-through flags on resume.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    from omnigent.runner import app as runner_app_mod

    session_id = "76cbdcbbf84d4149b2a7d7441b6966c1"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="019e96aa-1111-7222-8333-444455556666",
            codex_home=str(tmp_path / "stale-codex-home"),
        ),
    )

    class _SnapshotServerClient:
        """Server client that returns the persisted Codex launch config."""

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            """
            Return the session snapshot consumed by the runner helper.

            :param url: Request path, e.g.
                ``"/v1/sessions/76cbdcbbf84d4149b2a7d7441b6966c1"``.
            :param kwargs: Request keyword arguments such as
                ``{"timeout": 10.0}`` or ``{"params": {"limit": 1000}}``.
            :returns: HTTP 200 response carrying launch config.
            """
            if url == f"/v1/sessions/{session_id}/items":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "b1649a5cbfec3f92bec12275c14f4b5f",
                                "response_id": "codex_turn_1",
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "remember this"}],
                            }
                        ],
                        "has_more": False,
                    },
                    request=httpx.Request("GET", url),
                )
            assert url == f"/v1/sessions/{session_id}", kwargs
            return httpx.Response(
                200,
                json={
                    "terminal_launch_args": [
                        "--config",
                        "approval_policy=on-request",
                    ],
                    "model_override": "gpt-5.4-mini",
                    "external_session_id": thread_id,
                },
                request=httpx.Request("GET", url),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test", "IGNORED": "nope"}
            self.codex_home = tmp_path / "unconfigured-codex-home"
            self.listen_url: str | None = None
            self.started = False
            # Provider/model -c overrides the runner forwards to the
            # --remote TUI; empty here (no profile in this test).
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            assert list(self.codex_home.glob(f"sessions/**/rollout-*-{thread_id}.jsonl")), (
                "Codex resume rollout must be synthesized in app-server CODEX_HOME "
                "before app-server start"
            )
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()
    build_calls: list[dict[str, Any]] = []

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Capture app-server construction.

        :param kwargs: Keyword arguments passed by the runner helper.
        :returns: Fake app-server.
        """
        build_calls.append(kwargs)
        app_server.codex_home = kwargs["codex_home"]
        return app_server

    class _UnexpectedDiscoveryClient:
        """
        App-server client that must not connect on a known-thread resume.

        Fresh sessions connect this listener to discover ``thread/started``.
        Resume sessions already have ``external_session_id`` and should go
        straight to the known-thread forwarder.
        """

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """
            Fail if the resume path tries to discover a fresh thread.

            :returns: None.
            """
            raise AssertionError("resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            assert session_id == "76cbdcbbf84d4149b2a7d7441b6966c1"
            assert terminal_name == "codex"
            assert session_key == "main"
            assert resource_role == CODEX_NATIVE_TERMINAL_ROLE
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    published_events: list[dict[str, Any]] = []
    forward_calls: list[dict[str, Any]] = []
    preload_calls: list[tuple[str, str]] = []

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """
        Record preloading of the known Codex thread.

        :param transport: App-server transport URL.
        :param loaded_thread_id: Thread id passed to ``thread/resume``.
        :returns: None.
        """
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None, (
            "stale bridge state must be cleared until the new app-server has "
            "loaded the resume thread"
        )
        preload_calls.append((transport, loaded_thread_id))

    async def _fake_forward_known_thread(**kwargs: Any) -> None:
        """
        Record the known-thread forwarder invocation.

        :param kwargs: Forwarder keyword arguments.
        :returns: None.
        """
        forward_calls.append(kwargs)

    monkeypatch.setattr(
        codex_app_mod,
        "build_codex_native_server",
        _fake_build_codex_native_server,
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", _fake_forward_known_thread)

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        terminal_view = await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: published_events.append(event),
            agent_spec=agent_spec,
            server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    assert terminal_view.id == "terminal_codex_main"
    assert app_server.started is True
    expected_codex_home = codex_native_bridge.codex_home_for_bridge_dir(
        codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    )
    assert app_server.codex_home == expected_codex_home
    assert build_calls[0]["model"] == "gpt-5.4-mini"
    assert build_calls[0]["cwd"] == tmp_path / "workspace"
    assert "developer_instructions" not in build_calls[0]
    assert len(launched_specs) == 1
    launched = launched_specs[0]
    assert launched.command == "/opt/codex/bin/codex"
    assert launched.args[:3] == [
        "--config",
        "approval_policy=on-request",
        "resume",
    ]
    assert launched.args[3] == "--remote"
    assert launched.args[4].startswith("ws://127.0.0.1:")
    assert launched.args[5] == thread_id
    assert launched.env["OPENAI_API_KEY"] == "sk-test"
    assert "IGNORED" not in launched.env
    assert launched.env["CODEX_HOME"] == str(app_server.codex_home)
    assert launched.tmux_start_on_attach is False
    assert launched.tmux_allow_passthrough is True
    assert preload_calls == [(app_server.listen_url, thread_id)]
    assert published_events[0]["type"] == "session.resource.created"
    assert forward_calls == [
        {
            "session_id": session_id,
            "bridge_dir": bridge_dir,
            "codex_ws_url": app_server.listen_url,
            "thread_id": thread_id,
        }
    ]
    bridge_state = codex_native_bridge.read_bridge_state(bridge_dir)
    assert bridge_state is not None
    assert bridge_state.thread_id == thread_id
    assert bridge_state.socket_path == app_server.listen_url


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_fork_clones_rollout_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A forked codex clone clones the source rollout and resumes its copy.

    When the clone has no ``external_session_id`` but carries the fork
    labels, the runner must clone the SOURCE's rollout into the clone's
    own ``CODEX_HOME`` under a freshly minted thread id, pre-set that id
    on the Omnigent session, and launch ``codex resume <minted_id>`` (not the
    source thread). A regression launches fresh (no ``resume`` subcommand)
    and the clone loses the source's Codex history.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    from omnigent import codex_native
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir
    from omnigent.runner import app as runner_app_mod
    from omnigent.stores.conversation_store import (
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    session_id = "8aedf63f5e4046ae21b35fec5b35da50"
    source_id = "f143372bb481f9e85ffe3415f56f744f"
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="019e96aa-1111-7222-8333-444455556666",
            codex_home=str(tmp_path / "stale-codex-home"),
        ),
    )

    # Seed the SOURCE rollout in the source session's CODEX_HOME so the
    # fork branch finds something to clone.
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(source_id))
    source_rollout_dir = source_home / "sessions" / "2026" / "06" / "05"
    source_rollout_dir.mkdir(parents=True)
    (source_rollout_dir / f"rollout-2026-06-05T15-23-07-{source_thread}.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": source_thread, "cwd": "/old/source/dir"},
            }
        )
        + "\n"
    )

    patched_external_ids: list[str] = []

    class _ForkSnapshotClient:
        """Server client returning a forked clone snapshot (no thread id)."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """
            Return the clone's snapshot carrying fork labels but no thread id.

            :param url: Request path, e.g. ``"/v1/sessions/8aedf63f5e4046ae21b35fec5b35da50"``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response with fork labels.
            """
            del timeout
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(
                200,
                json={
                    "external_session_id": None,
                    "labels": {
                        FORK_SOURCE_LABEL_KEY: source_id,
                        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY: source_thread,
                    },
                },
                request=httpx.Request("GET", url),
            )

        async def patch(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
            """
            Record the pre-set external_session_id PATCH.

            :param url: Request path.
            :param json: PATCH body, e.g. ``{"external_session_id": "..."}``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response.
            """
            del timeout
            patched_external_ids.append(json["external_session_id"])
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
            self.listen_url: str | None = None
            self.started = False
            # Provider/model -c overrides forwarded to the --remote TUI.
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Return the fake app-server.

        :param kwargs: Construction kwargs (ignored).
        :returns: Fake app-server.
        """
        del kwargs
        return app_server

    class _UnexpectedDiscoveryClient:
        """Discovery client that must not connect on the resume path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """:raises AssertionError: Always — the fork resumes a known thread."""
            raise AssertionError("fork resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            del terminal_name, session_key, resource_role
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    forward_calls: list[dict[str, Any]] = []
    preload_calls: list[tuple[str, str]] = []

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """
        Record preloading of the cloned Codex thread.

        :param transport: App-server transport URL.
        :param loaded_thread_id: Thread id passed to ``thread/resume``.
        :returns: None.
        """
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None, (
            "fork-resume must not expose stale bridge state before preload"
        )
        preload_calls.append((transport, loaded_thread_id))

    async def _fake_forward_known_thread(**kwargs: Any) -> None:
        """
        Record the known-thread forwarder invocation.

        :param kwargs: Forwarder keyword arguments.
        :returns: None.
        """
        forward_calls.append(kwargs)

    monkeypatch.setattr(
        codex_app_mod, "build_codex_native_server", _fake_build_codex_native_server
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", _fake_forward_known_thread)

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_ForkSnapshotClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # A thread id was minted (uuidv7), pre-set on AP, and used for resume —
    # never the source thread id.
    assert len(patched_external_ids) == 1
    minted = patched_external_ids[0]
    assert minted != source_thread
    assert codex_native._CODEX_THREAD_ID_RE.fullmatch(minted)
    assert preload_calls == [(app_server.listen_url, minted)]
    assert forward_calls and forward_calls[0]["thread_id"] == minted

    launched = launched_specs[0]
    assert "resume" in launched.args
    assert launched.args[-1] == minted, (
        f"resume must target the minted thread id, got {launched.args}"
    )

    # The cloned rollout exists in the CLONE's CODEX_HOME under the minted
    # id, with session_meta.id and cwd rewritten.
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
    cloned = list(clone_home.glob(f"sessions/**/rollout-*-{minted}.jsonl"))
    assert len(cloned) == 1, f"expected one cloned rollout under {clone_home}, found {cloned}"
    meta = json.loads(cloned[0].read_text().splitlines()[0])["payload"]
    assert meta["id"] == minted
    assert meta["cwd"] == str(workspace.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_thread",
    [None, "019e96aa-0be2-7343-8d3b-6f914d60936b"],
    ids=["sdk-source", "missing-codex-rollout"],
)
async def test_auto_create_codex_terminal_fork_builds_rollout_from_items_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_thread: str | None,
) -> None:
    """A forked codex clone builds from items when its source rollout is unavailable.

    This covers both a non-Codex source with no source thread id and an imported
    Codex source whose rollout lives outside Omnigent's private ``CODEX_HOME``.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param source_thread: Optional unavailable source Codex thread id.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    from omnigent import codex_native
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir
    from omnigent.runner import app as runner_app_mod
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    session_id = "70a19efac3ec27549a70acbb4d0c635a"
    source_id = "4bd4c10aa9a3237eb5213cbeda70b70f"
    codeword = "swordfish-7281"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    patched_external_ids: list[str] = []

    class _ItemsForkSnapshotClient:
        """Server client: clone snapshot (carry-history, no source thread)
        plus the copied Omnigent items the rollout is built from."""

        async def get(
            self,
            url: str,
            *,
            timeout: float | None = None,
            params: dict[str, Any] | None = None,
        ) -> httpx.Response:
            """
            Serve the clone snapshot and its copied items.

            :param url: Request path — the session snapshot or its items.
            :param timeout: Request timeout (snapshot fetch).
            :param params: Query params (items fetch pagination).
            :returns: HTTP 200 response.
            """
            del timeout
            if url == f"/v1/sessions/{session_id}":
                labels = {
                    FORK_SOURCE_LABEL_KEY: source_id,
                    FORK_CARRY_HISTORY_LABEL_KEY: "1",
                }
                if source_thread is not None:
                    labels[FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY] = source_thread
                return httpx.Response(
                    200,
                    json={
                        "external_session_id": None,
                        "labels": labels,
                    },
                    request=httpx.Request("GET", url),
                )
            assert url == f"/v1/sessions/{session_id}/items"
            assert params is not None and params.get("order") == "asc"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": f"codeword {codeword}"}],
                        }
                    ],
                    "has_more": False,
                },
                request=httpx.Request("GET", url),
            )

        async def patch(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
            """
            Record the pre-set external_session_id PATCH.

            :param url: Request path.
            :param json: PATCH body, e.g. ``{"external_session_id": "..."}``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response.
            """
            del timeout
            patched_external_ids.append(json["external_session_id"])
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
            self.listen_url: str | None = None
            self.started = False
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """:param kwargs: Construction kwargs (ignored). :returns: Fake server."""
        del kwargs
        return app_server

    class _UnexpectedDiscoveryClient:
        """Discovery client that must not connect on the resume path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """:param ws_url: App-server URL. :param client_name: RPC client name."""
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """:raises AssertionError: Always — the fork resumes a known thread."""
            raise AssertionError("fork resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            del terminal_name, session_key, resource_role
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    forward_calls: list[dict[str, Any]] = []
    preload_calls: list[tuple[str, str]] = []

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """:param transport: App-server URL. :param loaded_thread_id: Resumed thread."""
        preload_calls.append((transport, loaded_thread_id))

    async def _fake_forward_known_thread(**kwargs: Any) -> None:
        """:param kwargs: Forwarder keyword arguments. :returns: None."""
        forward_calls.append(kwargs)

    monkeypatch.setattr(
        codex_app_mod, "build_codex_native_server", _fake_build_codex_native_server
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", _fake_forward_known_thread)

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_ItemsForkSnapshotClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # A thread id was minted, pre-set on AP, and used for resume.
    assert len(patched_external_ids) == 1
    minted = patched_external_ids[0]
    assert codex_native._CODEX_THREAD_ID_RE.fullmatch(minted)
    assert preload_calls == [(app_server.listen_url, minted)]
    assert forward_calls and forward_calls[0]["thread_id"] == minted

    launched = launched_specs[0]
    assert "resume" in launched.args
    assert launched.args[-1] == minted, (
        f"resume must target the minted thread id, got {launched.args}"
    )

    # The rollout was BUILT (not cloned) in the clone's CODEX_HOME under the
    # minted id, carrying the source conversation's codeword — proving the
    # copied Omnigent items, not a source rollout, seeded the history.
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
    built = list(clone_home.glob(f"sessions/**/rollout-*-{minted}.jsonl"))
    assert len(built) == 1, f"expected one built rollout under {clone_home}, found {built}"
    body = built[0].read_text()
    meta = json.loads(body.splitlines()[0])["payload"]
    assert meta["id"] == minted
    assert meta["cwd"] == str(workspace.resolve())
    assert codeword in body, (
        "Built rollout must carry the source conversation's text from the "
        "copied Omnigent items; missing it means history was not seeded."
    )


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_uses_worktree_workspace_not_bundle_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex-native launches in the session worktree, not the bundle dir.

    Regression for the worktree bug: ``_codex_workspace_from_spec_or_env``
    preferred ``ResolvedSpec.workdir`` (the runner's spec-bundle
    extraction dir) over the session workspace, stranding Codex in a temp
    dir with no ``.git`` and ignoring the worktree entirely. The fix reads
    the workspace from the session snapshot (the worktree path), matching
    claude-native and the per-session filesystem registry.

    This test sets up the adversarial case the bug got wrong: the snapshot
    reports a worktree workspace that differs from BOTH the runner env var
    and the ResolvedSpec bundle dir. The launched Codex app-server's
    ``cwd`` must be the worktree. If reverted, ``cwd`` would be the bundle
    dir and the ``cwd == worktree`` assertion fails.

    :param tmp_path: Temporary directory for isolated bridge/workspace state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner import app as runner_app_mod

    session_id = "54e4d4410c43954c11e702f5a8646483"
    # Three distinct dirs so the assertion can only pass for the worktree:
    #   runner_env  — OMNIGENT_RUNNER_WORKSPACE (claude-native's source)
    #   bundle_dir  — ResolvedSpec.workdir (what the bug used)
    #   worktree    — the session's stored workspace (correct answer)
    runner_env = tmp_path / "runner_workspace"
    runner_env.mkdir()
    bundle_dir = tmp_path / "runner-specs" / f"{session_id}-v1"
    bundle_dir.mkdir(parents=True)
    worktree = tmp_path / "repo-worktrees" / "feature-x"
    worktree.mkdir(parents=True)

    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(runner_env))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="019e96aa-1111-7222-8333-444455556666",
            codex_home=str(tmp_path / "stale-codex-home"),
        ),
    )

    class _WorktreeSnapshotClient:
        """Server client whose session snapshot carries a worktree workspace."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """
            Return the session snapshot with a worktree ``workspace``.

            :param url: Request path, e.g.
                ``"/v1/sessions/54e4d4410c43954c11e702f5a8646483"``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response carrying the worktree workspace.
            """
            del timeout
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(
                200,
                json={
                    "workspace": str(worktree),
                    "terminal_launch_args": None,
                    "model_override": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", url),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = tmp_path / "codex-home"
            self.listen_url: str | None = None
            self.started = False
            # Provider/model -c overrides forwarded to the --remote TUI.
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()
    build_calls: list[dict[str, Any]] = []

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Capture app-server construction.

        :param kwargs: Keyword arguments passed by the runner helper.
        :returns: Fake app-server.
        """
        build_calls.append(kwargs)
        return app_server

    class _FakeDiscoveryClient:
        """App-server client for the fresh-thread discovery path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    async def _fake_discover_thread_and_forward(**kwargs: Any) -> None:
        """
        Stand in for the fresh-session discovery forwarder.

        :param kwargs: Forwarder keyword arguments.
        :returns: None.
        """
        assert kwargs["bridge_dir"] == bridge_dir
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None, (
            "fresh Codex launch must clear stale bridge state before the "
            "discovery forwarder publishes the new thread"
        )

    launch_captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :param parent_os_env: Agent os_env threaded as the inheritance
                parent so the agent's sandbox / egress / passthrough apply.
            :returns: Terminal resource view.
            """
            del session_key, resource_role
            launch_captured["spec"] = spec
            launch_captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    monkeypatch.setattr(
        codex_app_mod,
        "build_codex_native_server",
        _fake_build_codex_native_server,
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _FakeDiscoveryClient)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_discover_thread_and_forward",
        _fake_discover_thread_and_forward,
    )

    # agent_spec is a ResolvedSpec whose workdir is the bundle dir — the
    # exact value the old code wrongly used as the cwd. Its os_env declares
    # sandbox: none, so the launched terminal must inherit that (not the
    # platform default) — see the sandbox-override regression note below.
    codex_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = ResolvedSpec(
        spec=AgentSpec(
            spec_version=1,
            name="codex",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "codex-native", "model": "gpt-5-default"},
            ),
            os_env=codex_os_env,
        ),
        workdir=bundle_dir,
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_WorktreeSnapshotClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # The Codex app-server cwd must be the worktree (resolved — the launch
    # config normalizes with expanduser().resolve()). A failure here means
    # the workspace resolution regressed: the bundle dir means the old
    # ResolvedSpec.workdir bug is back; the runner env dir means the
    # snapshot workspace was ignored.
    assert build_calls[0]["cwd"] == worktree.resolve(), (
        f"Codex launched in {build_calls[0]['cwd']!r}; expected the worktree "
        f"{worktree.resolve()!r}. bundle_dir={bundle_dir!r} would mean the "
        f"ResolvedSpec.workdir bug regressed; runner_env={runner_env!r} would "
        "mean the session snapshot workspace was ignored."
    )
    assert build_calls[0]["cwd"] != bundle_dir.resolve()  # never the spec-bundle dir
    assert "developer_instructions" not in build_calls[0]

    # Sandbox-override regression: the launched Codex terminal must inherit
    # the agent's sandbox: none rather than falling back to the platform
    # default (bwrap). Without it, a codex-native agent declaring
    # ``os_env.sandbox.type: none`` is wrongly forced into bwrap.
    launched_sandbox = launch_captured["spec"].os_env.sandbox
    assert launched_sandbox is not None and launched_sandbox.type == "none"
    assert launch_captured["parent_os_env"] is codex_os_env


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_starts_relay_at_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool relay is started at session creation, non-blocking.

    Root-cause fix for the ~30s first-turn stall: the relay (which writes
    ``tool_relay.json`` codex reads via serve-mcp) must be started when the
    session is created, into the same bridge dir codex uses, and WITHOUT
    awaiting the tools/list_changed notification. Previously the relay was
    only started on the first turn with ``await_notify=True``, which blocked
    on codex's MCP bridge ``server.json`` — a file that only appears once
    codex runs the turn — until ``post_tools_changed``'s 30s timeout.

    Asserts ``_auto_create_codex_terminal`` invokes the injected
    ``ensure_comment_relay`` exactly once, for this session's bridge dir,
    with ``await_notify=False``. If the call regressed to the first-turn
    path the spy would never fire here; if it regressed to
    ``await_notify=True`` the assertion on that kwarg would fail.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    from omnigent.runner import app as runner_app_mod

    session_id = "de154ca6405fb8912623984a14a2b044"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _SnapshotClient:
        """Fresh-session snapshot (no external thread → discovery path)."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """:returns: HTTP 200 fresh-session snapshot."""
            del timeout, url
            return httpx.Response(
                200,
                json={
                    "workspace": str(tmp_path / "workspace"),
                    "terminal_launch_args": None,
                    "model_override": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", f"/v1/sessions/{session_id}"),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = tmp_path / "codex-home"
            self.listen_url: str | None = None
            # Provider/model -c overrides forwarded to the --remote TUI.
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    class _FakeDiscoveryClient:
        """No-op app-server client for the discovery path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """:param ws_url: ws url. :param client_name: client name."""
            del ws_url, client_name

        async def connect(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    class _FakeResourceRegistry:
        """Resource registry returning a fixed terminal view."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """:returns: A fixed terminal resource view."""
            del terminal_name, session_key, spec, resource_role
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    async def _fake_discover(**kwargs: Any) -> None:
        """:returns: None — stands in for the discovery forwarder."""
        del kwargs

    monkeypatch.setattr(
        codex_app_mod, "build_codex_native_server", lambda **k: _FakeCodexAppServer()
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _FakeDiscoveryClient)
    monkeypatch.setattr(runner_app_mod, "_codex_discover_thread_and_forward", _fake_discover)

    relay_calls: list[dict[str, Any]] = []

    async def _spy_ensure_relay(sid: str, **kwargs: Any) -> None:
        """Record the relay-start invocation."""
        relay_calls.append({"session_id": sid, **kwargs})

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_SnapshotClient(),  # type: ignore[arg-type]
            ensure_comment_relay=_spy_ensure_relay,
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # Exactly one relay start, at session creation, for this session's bridge
    # dir, and non-blocking (await_notify=False) — the crux of the fix.
    assert len(relay_calls) == 1, relay_calls
    assert relay_calls[0]["session_id"] == session_id
    assert relay_calls[0]["explicit_bridge_dir"] == codex_native_bridge.bridge_dir_for_bridge_id(
        session_id
    )
    assert relay_calls[0]["await_notify"] is False


@pytest.mark.asyncio
async def test_claude_native_first_turn_not_blocked_by_cold_bridge_notify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First claude-native turn dispatches without waiting on a cold bridge.

    A UI-launched (never pre-warmed) claude-native session starts the comment
    relay lazily on the first turn. The ``tools/list_changed`` delivery
    (``post_tools_changed``) blocks until the bridge publishes ``server.json``
    — up to ``_TOOLS_CHANGED_READY_TIMEOUT_S`` (30s) on a still-cold bridge.
    The turn must NOT be gated on that: the claude-native first-turn caller
    passes ``await_notify=False``, so the relay starts and the notification is
    fired in a background task while the turn dispatches immediately.

    This holds ``post_tools_changed`` open on a never-released event (a cold
    bridge that never publishes ``server.json``) and asserts:

    (a) the notification was actually attempted — the relay genuinely started
        and reached the delivery step. Without this, (b) passes vacuously: a
        relay that bailed early (failed socket bind, unresolved spec) never
        blocks, so the turn was never at risk.
    (b) the harness still received the turn while the notification is blocked.

    A regression to ``await_notify=True`` would await ``post_tools_changed``
    inline, parking ``_run_turn_bg`` at the relay-start step until the event
    is released, so the harness would never see the turn within the poll
    budget and (b) fails.

    :param tmp_path: Temp dir backing the runner workspace (the bridge tree
        itself must live under the real ``/tmp`` trusted parent —
        ``_ensure_secure_dir`` rejects a bridge dir anywhere else, so the
        bridge root is NOT redirected into ``tmp_path``).
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    session_id = uuid.uuid4().hex
    # claude-native pins the bridge tree under /tmp (see _ensure_secure_dir);
    # use the real per-user bridge dir like tests/runner/test_comment_relay.py
    # and rmtree it on teardown rather than redirecting _BRIDGE_ROOT.
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    # start_tool_relay writes tool_relay.json INTO the bridge dir but does not
    # create it — mirror the client's prepare_bridge_dir before launch.
    prepare_bridge_dir(session_id, workspace=tmp_path)

    notify_started = threading.Event()
    notify_release = threading.Event()

    def _blocking_post_tools_changed(*args: Any, **kwargs: Any) -> None:
        """Stand in for a cold bridge: signal entry, then block until released.

        Runs in the default thread-pool executor (``post_tools_changed`` is
        synchronous), so a threading.Event is the right primitive.

        :param args: Positional args from the call site (``bridge_dir``).
        :param kwargs: Keyword args (none expected).
        :returns: None.
        """
        del args, kwargs
        notify_started.set()
        notify_release.wait()

    # The runner imports post_tools_changed from this module at call time, so
    # patching the module attribute is picked up by _ensure_comment_relay_started.
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.post_tools_changed",
        _blocking_post_tools_changed,
    )

    spec = AgentSpec(
        spec_version=1,
        name="claude",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Resolve every request to the claude-native spec under test."""
        del agent_id, session_id
        return spec

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "claude-agent",
                    "agent_id": "3a9725fd4de1720e83e53a632da41da8",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert resp.status_code == 202, f"{resp.status_code} {resp.text}"

            # (a) The relay started and reached the notification: post_tools_changed
            # is now parked on notify_release. If start_tool_relay or the spec
            # resolve had bailed, this never fires — making (b) vacuous.
            for _ in range(300):
                if notify_started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert notify_started.is_set(), (
                "post_tools_changed was never invoked: the relay did not start or "
                "did not reach the notify step, so the no-block assertion below "
                "would be vacuous."
            )

            # (b) The harness received the turn even though post_tools_changed is
            # still blocked (notify_release is NOT set). An unbounded await on the
            # notification would park _run_turn_bg at relay-start, leaving
            # posted_bodies empty until release — that is the ~15-30s first-turn
            # stall this change removes.
            for _ in range(300):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
            assert hc.posted_bodies, (
                "claude-native first turn never reached the harness while the "
                "tools/list_changed delivery was blocked — the turn is gated on a "
                "cold-bridge notification."
            )
            # Sanity: we never unblocked delivery, so (b) proves a bounded wait,
            # not that the bridge came up.
            assert not notify_release.is_set()
    finally:
        # Unblock the parked executor thread BEFORE teardown so the loop's
        # shutdown_default_executor(wait=True) does not hang joining it, then
        # close the relay socket/thread by deleting the session.
        notify_release.set()
        with contextlib.suppress(httpx.HTTPError):
            async with _runner_client(app) as cleanup_client:
                await cleanup_client.delete(f"/v1/sessions/{session_id}")
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def _run_antigravity_auto_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str,
    snapshot: dict[str, Any],
    candidate_ports: list[int],
    pane: tuple[Path, str] | None = None,
    pane_scoped_port: int | None = None,
    pane_agy_found: bool = True,
) -> tuple[Any, list[tuple[int, str]], list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """
    Drive ``_auto_create_antigravity_terminal`` with every live collaborator faked.

    No real agy is launched: ``build_agy_launch`` is stubbed to a no-op argv, the
    onboarding seed is a no-op, the resource registry records the launched spec,
    the forwarder is a counting stub, and the connect-RPC layer
    (``_candidate_agy_rpc_ports`` / ``resolve_pane_agy_rpc_port_state`` /
    ``start_cascade``) is mocked so the cold-start bootstrap runs without a socket.

    The cold-start exercises the REAL ``resolve_cold_start_agy_rpc_port`` dispatch:
    with no ``pane`` the pane is absent (``_terminal_tmux_pane`` → ``(None, None)``)
    and it falls back to the candidate scan; with a ``pane`` the pane-scoped
    resolver's 3-state result (driven by ``pane_scoped_port`` + ``pane_agy_found``)
    is consulted first.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param session_id: Session/conversation id under test.
    :param snapshot: The Omnigent session snapshot the helper should read.
    :param candidate_ports: Ports ``_candidate_agy_rpc_ports`` yields (``[]`` →
        the bootstrap never finds a candidate port).
    :param pane: ``(tmux_socket, tmux_target)`` ``_terminal_tmux_pane`` returns,
        or ``None`` (the default) → ``(None, None)`` (no local pane).
    :param pane_scoped_port: The port the pane-scoped resolver reports (``None`` →
        no port; combined with ``pane_agy_found`` to pick state 1/2/3).
    :param pane_agy_found: Whether the pane-scoped resolver found our agy in the
        pane subtree. ``True`` + a port → scoped (state 1); ``True`` + no port →
        candidate fallback (state 2); ``False`` → keep polling (state 3).
    :returns: ``(bridge_state_after, start_cascade_calls, reader_calls,
        external_session_id_patch_calls)``.
    """
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    from omnigent import antigravity_native_bridge as bridge_mod
    from omnigent.runner import app as runner_app_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    # No-op the launch builder + onboarding seed so nothing tries to find agy.
    monkeypatch.setattr(
        launch_mod, "build_agy_launch", lambda **_kwargs: (("agy",), {"AGY_ENV": "1"})
    )
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    # Auto-create now spawns the RPC reader (NOT the transcript forwarder); stub
    # ``supervise_reader`` at its definition module (the helper imports it lazily)
    # so the test does not start a real one. The reader is wrapped in
    # ``_run_antigravity_reader``, which still opens (and, on teardown, closes) a
    # real Omnigent client around this stub — fine, since nothing posts here.
    reader_calls: list[dict[str, Any]] = []

    def _counting_reader(*args: Any, **kwargs: Any) -> Any:
        reader_calls.append(kwargs)

        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _counting_reader)
    # The pane the runner resolves for cold-start scoping + the tmux advertise.
    # Default ``None`` → no local pane (``(None, None)``), so the cold-start uses
    # the candidate-scan fallback. A provided pane lets the test assert the
    # pane-scoped port path.
    resolved_pane = (None, None) if pane is None else pane
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: resolved_pane)
    # The pane-scoped resolver the REAL ``resolve_cold_start_agy_rpc_port``
    # consults first when a pane is present — returns the 3-state result.
    pane_resolution = rpc_mod.PaneAgyResolution(agy_found=pane_agy_found, port=pane_scoped_port)
    monkeypatch.setattr(
        rpc_mod, "resolve_pane_agy_rpc_port_state", lambda _sock, _tgt: pane_resolution
    )

    # Mock the connect-RPC cold-start surface. Collapse the port-poll budget +
    # backoff so the no-port case bails immediately instead of waiting the real
    # 20s (and the success case still finds its port on the first probe).
    monkeypatch.setattr(runner_app_mod, "_AGY_COLD_START_PORT_TIMEOUT_S", 0.0)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner_app_mod, "_agy_cold_start_poll_sleep", _no_sleep)
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", lambda: list(candidate_ports))
    start_cascade_calls: list[tuple[int, str]] = []

    def _fake_start_cascade(port: int, cascade_id: str, **_kwargs: Any) -> None:
        start_cascade_calls.append((port, cascade_id))

    monkeypatch.setattr(rpc_mod, "start_cascade", _fake_start_cascade)

    patch_calls: list[tuple[str, dict[str, Any]]] = []

    class _SnapshotServerClient:
        """Server client returning the snapshot + recording external_session_id PATCHes."""

        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(200, json=snapshot, request=httpx.Request("GET", url))

        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            patch_calls.append((url, json))
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeResourceRegistry:
        """Resource registry that records the required-terminal launch."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            assert terminal_name == "antigravity"
            assert session_key == "main"
            assert resource_role == ANTIGRAVITY_NATIVE_TERMINAL_ROLE
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
        )
        await asyncio.sleep(0)
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)

    bridge_dir = bridge_mod.bridge_dir_for_bridge_id(session_id)
    return (
        bridge_mod.read_bridge_state(bridge_dir),
        start_cascade_calls,
        reader_calls,
        patch_calls,
    )


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_starts_real_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A fresh runner launch cold-starts the agy conversation over RPC.

    The runner mints the cascade over ``StartCascade`` (no send-keys / no waiting
    for the TUI to lazily create it) so the executor's turn-1 has a real cascade
    id. This asserts the load-bearing integration: after the agy terminal launches
    and the connect-RPC port answers, the runner calls ``start_cascade`` with a
    runner-generated id, writes THAT real id into bridge state — NOT the
    ``agy_conv_*`` placeholder ``read_bridge_state`` would otherwise return — and
    PATCHes it onto the session as ``external_session_id`` so a later ``--resume``
    continues it.
    """
    session_id = "72a4f9222c7ac0f45ba2736b57b51f62"
    state, start_cascade_calls, reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},  # fresh: no external_session_id
        candidate_ports=[52548],
    )
    # start_cascade was called once, on the discovered port, with a real id.
    assert len(start_cascade_calls) == 1
    called_port, called_id = start_cascade_calls[0]
    assert called_port == 52548
    assert not bridge_mod_is_placeholder(called_id)
    # The real cold-started id is what reaches bridge state (no placeholder).
    assert state is not None
    assert state.conversation_id == called_id
    assert not bridge_mod_is_placeholder(state.conversation_id)
    # The same real id is PATCHed onto the session as external_session_id so a
    # later --resume continues agy's actual conversation (the read-path
    # replacement for the retired forwarder's _patch_external_session_id).
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)
    # The RPC reader spawns (it replaced the transcript forwarder).
    assert len(reader_calls) == 1


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_scopes_to_pane_agy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With several agy candidates, cold-start binds THIS session's pane agy.

    The cross-bind fix: on a host running several agy instances (sub-agent
    fan-out / shared runner), ``StartCascade`` must target the agy actually
    running under this session's tmux pane — NOT the lowest Heartbeat-answering
    candidate, which could be a FOREIGN agy and permanently cross-bind the
    session. With a resolvable pane the cold-start uses the pane-scoped port
    (61000) even though a lower foreign candidate (52548) exists.
    """
    session_id = "4130b87cea0f50aab661215a1c220d2a"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        # Several agy ports on the host; 52548 is the lowest (a FOREIGN agy).
        candidate_ports=[52548, 61000],
        # This session's pane resolves to a DIFFERENT (higher) agy's port.
        pane=(tmp_path / "agy.sock", "main"),
        pane_scoped_port=61000,
    )
    # StartCascade fired on the PANE-SCOPED port, NOT candidates[0] (52548).
    assert len(start_cascade_calls) == 1
    called_port, called_id = start_cascade_calls[0]
    assert called_port == 61000
    assert not bridge_mod_is_placeholder(called_id)
    assert state is not None
    assert state.conversation_id == called_id
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_falls_back_when_no_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No local pane (remote runner) → cold-start uses the lowest candidate port.

    Preserves the current behavior on single-agy hosts and remote runners: when
    ``_terminal_tmux_pane`` yields no socket/target the pane cannot be scoped, so
    the cold-start falls back to ``_candidate_agy_rpc_ports()[0]``.
    """
    session_id = "dd3b597d06f41a0914a65689398c6047"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[52548, 61000],
        pane=None,  # remote runner / no local pane
        pane_scoped_port=99999,  # must be ignored — there is no pane to scope to
    )
    assert len(start_cascade_calls) == 1
    called_port, _called_id = start_cascade_calls[0]
    assert called_port == 52548  # the lowest candidate, NOT the (ignored) pane port
    assert state is not None
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_waits_when_pane_agy_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pane present, our agy NOT up yet, FOREIGN candidate present → no cold-start.

    The cross-bind guard: with a local pane whose agy has not appeared yet
    (``agy_found=False``) and a foreign agy as the only candidate, the cold-start
    must NOT bind the foreign candidate — it keeps polling until its (collapsed)
    deadline, leaving the placeholder for the reader to bind later. No
    ``StartCascade``, no ``external_session_id`` PATCH.
    """
    session_id = "b4741a5530a1e4f6de788ecc6bf2ed16"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[52548],  # a FOREIGN agy is the only candidate
        pane=(tmp_path / "agy.sock", "main"),
        pane_agy_found=False,  # our agy not exec'd into the pane yet
        pane_scoped_port=None,
    )
    # Never cold-started onto the foreign candidate; placeholder stands.
    assert start_cascade_calls == []
    assert patch_calls == []
    assert state is not None
    assert bridge_mod_is_placeholder(state.conversation_id)


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_falls_back_when_port_unattributable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pane present, our agy found, port not lsof-attributable → candidate fallback.

    The restricted-/proc one-agy-per-pod case: our agy IS up in the pane
    (``agy_found=True``) but lsof cannot attribute its listener, so the scoped
    port is ``None``. Since agy exists here, the lone candidate is ours and the
    candidate fallback is safe.
    """
    session_id = "09ac5119b8cc54502810b565d8206821"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[52548],  # one-agy-per-pod → the lone candidate is ours
        pane=(tmp_path / "agy.sock", "main"),
        pane_agy_found=True,  # our agy IS up...
        pane_scoped_port=None,  # ...but its port is not lsof-attributable
    )
    assert len(start_cascade_calls) == 1
    assert start_cascade_calls[0][0] == 52548  # safe candidate fallback
    assert state is not None
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)


@pytest.mark.asyncio
async def test_auto_create_antigravity_resume_skips_cold_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A resume launch does NOT cold-start — the conversation already exists.

    On resume the snapshot carries agy's real ``external_session_id`` (persisted
    by a prior run), so the conversation already exists and ``StartCascade`` must
    not be issued (it would create a second, empty one). Bridge state keeps the
    resume id verbatim, and — since no cold-start runs — no ``external_session_id``
    PATCH is issued (it already holds the resume id).
    """
    session_id = "ac8a43ec8a770428cdb9eb718114efc5"
    resume_id = "68caaeac-2eaf-4e2c-9b95-721b022f4903"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={"external_session_id": resume_id},
        candidate_ports=[52548],
    )
    assert start_cascade_calls == []
    assert state is not None
    assert state.conversation_id == resume_id
    assert patch_calls == []


@pytest.mark.asyncio
async def test_cold_start_agy_conversation_returns_early_on_real_id_in_bridge_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The runner cold-start refuses to run when bridge state already holds a real id.

    Defense-in-depth mirroring the CLI cold-start (``antigravity_native.py``): the
    caller only invokes this on a fresh launch (``if not resume:``), but if bridge
    state already names a NON-placeholder conversation id, cold-starting would
    create a second empty conversation and clobber the real id. The guard must
    early-return BEFORE probing for a port or calling ``StartCascade`` — so even a
    future caller that forgets the resume gate cannot cold-start over a real id.
    """
    import omnigent.antigravity_native_rpc as rpc_mod
    from omnigent import antigravity_native_bridge as bridge_mod
    from omnigent.runner import app as runner_app_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    session_id = "0f44894f77886259ee71e892a9e2afd7"
    real_id = "68caaeac-2eaf-4e2c-9b95-721b022f4903"  # NOT an agy_conv_* placeholder
    assert not bridge_mod.is_placeholder_conversation_id(real_id)

    bridge_dir = bridge_mod.prepare_bridge_dir(session_id)
    bridge_mod.write_bridge_state(
        bridge_dir,
        bridge_mod.AntigravityNativeBridgeState(
            session_id=session_id,
            conversation_id=real_id,
        ),
    )

    # The cold-start must touch NEITHER the port-scan NOR StartCascade.
    def _no_ports() -> list[int]:
        raise AssertionError("cold-start must not probe for a port when the id is real")

    start_cascade_calls: list[tuple[int, str]] = []

    def _fake_start_cascade(port: int, cascade_id: str, **_kwargs: Any) -> None:
        start_cascade_calls.append((port, cascade_id))

    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", _no_ports)
    monkeypatch.setattr(rpc_mod, "start_cascade", _fake_start_cascade)

    patch_calls: list[tuple[str, dict[str, Any]]] = []

    class _RecordingServerClient:
        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            patch_calls.append((url, json))
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    result = await runner_app_mod._cold_start_agy_conversation(
        bridge_dir,
        session_id,
        server_client=cast(httpx.AsyncClient, _RecordingServerClient()),
    )

    # Returns the existing real id, and never cold-started or re-PATCHed.
    assert result == real_id
    assert start_cascade_calls == []
    assert patch_calls == []
    # Bridge state is untouched (still the real id, not a fresh cold-start id).
    state = bridge_mod.read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == real_id


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_port_timeout_keeps_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When no connect-RPC port answers, the cold-start is best-effort: the launch
    still completes and leaves the placeholder id for the reader to bind.

    The cold-start must NOT abort the launch (which would leave a registered
    terminal with no reader, never self-healing). With no port, ``start_cascade``
    is never called, bridge state retains the ``agy_conv_*`` placeholder, and no
    ``external_session_id`` PATCH is issued (there is no real id to record).
    """
    session_id = "3d781eaf3cbda148c8f3212c7c21ea04"
    state, start_cascade_calls, reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[],  # port never comes up within the bounded poll
    )
    assert start_cascade_calls == []
    assert state is not None
    assert bridge_mod_is_placeholder(state.conversation_id)
    assert patch_calls == []
    # The RPC reader still spawns regardless of the cold-start outcome.
    assert len(reader_calls) == 1


@pytest.mark.asyncio
async def test_auto_create_antigravity_wires_reader_task_and_interaction_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-create spawns the RPC reader task and wires its interaction bridge.

    Asserts the Task 11b integration linchpin end-to-end against fakes:

    * the background task is the ``antigravity-reader-{session_id}`` reader (NOT
      the transcript forwarder), registered in the single-instance task slot;
    * the reader is wired with an ``on_pending_interaction`` that, when a WAITING
      interaction is handed to it, POSTs the Task 9 antigravity-elicitation hook
      with ``{elicitation_id, params}``, then — on the human verdict — delivers
      the answer to agy via ``handle_user_interaction`` (the bridge default).
    """
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    from omnigent import antigravity_native_bridge as bridge_mod
    from omnigent import antigravity_native_interactions as interactions_mod
    from omnigent.antigravity_native_interactions import agy_elicitation_id
    from omnigent.antigravity_native_steps import pending_interaction
    from omnigent.runner import app as runner_app_mod

    session_id = "b68c3f1da613f48fb4126e965ab594a3"
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(
        launch_mod, "build_agy_launch", lambda **_kwargs: (("agy",), {"AGY_ENV": "1"})
    )
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: (None, None))
    # Skip the 11a cold-start network work (resume launch → no StartCascade).
    resume_id = "efb134b2-d69f-43de-bb54-c9ece346d8a3"
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", list)

    # Capture the reader's wiring (client + on_pending_interaction) and park so the
    # owning ``_run_antigravity_reader`` keeps its client open while we drive the
    # callback. ``supervise_reader`` is patched at its definition module (the
    # helper imports it lazily).
    captured: dict[str, Any] = {}
    wired = asyncio.Event()

    def _capturing_reader(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        wired.set()

        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _capturing_reader)

    # Control the reader's Omnigent client transport: record the elicitation hook
    # POST and return the human's ACCEPT verdict as an ElicitationResult body.
    hook_posts: list[tuple[str, dict[str, Any]]] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        hook_posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"action": "accept", "content": {}})

    real_async_client = httpx.AsyncClient

    def _mock_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_handle), **kwargs)

    # ``_run_antigravity_reader`` builds its client via ``httpx.AsyncClient``; patch
    # the httpx module itself (the reader is the only AsyncClient built on this
    # auto-create path — the snapshot client is a hand-rolled fake) so its POSTs hit
    # the MockTransport above instead of the network.
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client)

    # The WAITING (permission) step the bridge re-reads at delivery time, and the
    # ``handle_user_interaction`` delivery sink (the bridge's default ``deliver``).
    waiting_step = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "antigravity"
            / "steps"
            / "run_command_waiting.json"
        ).read_text()
    )
    # The shared ``run_reader_with_bridge`` helper's ``_get_steps`` closure binds
    # ``get_trajectory_steps`` from the reader module's top-level import, so patch
    # it there (matching how the reader's own poll-loop tests patch it).
    monkeypatch.setattr(reader_mod, "get_trajectory_steps", lambda _port, _cid: [waiting_step])
    delivered: list[dict[str, Any]] = []

    def _fake_deliver(
        port: int, cascade_id: str, *, trajectory_id: str, step_index: int, payload: Any
    ) -> None:
        delivered.append(
            {
                "port": port,
                "cascade_id": cascade_id,
                "trajectory_id": trajectory_id,
                "step_index": step_index,
                "payload": payload,
            }
        )

    monkeypatch.setattr(interactions_mod, "handle_user_interaction", _fake_deliver)

    class _SnapshotServerClient:
        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(
                200,
                json={"external_session_id": resume_id},
                request=httpx.Request("GET", url),
            )

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
        )
        await asyncio.wait_for(wired.wait(), timeout=5.0)

        # The single-instance task slot holds the reader task, named for the reader.
        task = runner_app_mod._AUTO_FORWARDER_TASKS[session_id]
        assert task.get_name() == f"antigravity-reader-{session_id}"

        # Drive the captured wiring with a WAITING (permission) interaction, as the
        # reader would when it observes one. Use the SAME cascade id + port the
        # callback contract threads through.
        port = 52548
        pending = pending_interaction(waiting_step)
        assert pending is not None
        await captured["on_pending_interaction"](resume_id, port, pending)

        # 1) It POSTed the antigravity-elicitation hook with {elicitation_id, params}.
        assert len(hook_posts) == 1
        path, body = hook_posts[0]
        assert path == f"/v1/sessions/{session_id}/hooks/antigravity-elicitation-request"
        assert body["elicitation_id"] == agy_elicitation_id(
            resume_id, pending["trajectory_id"], pending["step_index"]
        )
        assert isinstance(body["params"], dict)

        # 2) On the ACCEPT verdict it delivered the answer to agy via the bridge.
        assert len(delivered) == 1
        assert delivered[0]["cascade_id"] == resume_id
        assert delivered[0]["port"] == port
        assert delivered[0]["trajectory_id"] == pending["trajectory_id"]
        assert delivered[0]["step_index"] == pending["step_index"]
        assert delivered[0]["payload"] == {"permission": {"allow": True}}
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)


@pytest.mark.asyncio
async def test_auto_create_antigravity_wires_omnigent_mcp_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-create wires the Omnigent MCP relay so agy gets the sys_* tools (#1194).

    Asserts the three wiring points end-to-end against fakes:

    * the relay starter (``ensure_comment_relay``) is invoked for THIS session's
      bridge dir before launch, so its ``tool_relay.json`` is on disk when agy
      first scans the MCP server;
    * the relay ``mcp_config.json`` is written into the per-session isolated agy
      Gemini dir (``<bridge_dir>/agy-home/.gemini/config``), NOT the user's real
      ``~/.gemini`` — the config-scoping footgun the design avoids;
    * the launch args carry ``--gemini_dir=<isolated .gemini>`` while the launch
      env does not override ``HOME``, so agy keeps platform auth such as macOS
      Keychain but loads the bridge-scoped config.
    """
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    from omnigent import antigravity_native_bridge as bridge_mod
    from omnigent.runner import app as runner_app_mod

    session_id = "1fd85439049bbfc88cbf04221bad5079"
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", list)

    # Capture the env build_agy_launch starts from so we can assert it is preserved
    # without a HOME override (the launch env is the captured spec's ``env`` below).
    monkeypatch.setattr(
        launch_mod, "build_agy_launch", lambda **_kwargs: (("agy",), {"AGY_ENV": "1"})
    )

    def _noop_reader(*_args: Any, **_kwargs: Any) -> Any:
        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _noop_reader)

    relay_calls: list[dict[str, Any]] = []

    async def _recording_relay(session_id_arg: str, **kwargs: Any) -> None:
        relay_calls.append({"session_id": session_id_arg, **kwargs})

    captured_spec: dict[str, Any] = {}

    snapshot = {"workspace": str(tmp_path / "workspace")}

    class _SnapshotServerClient:
        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(200, json=snapshot, request=httpx.Request("GET", url))

        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            del json
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            captured_spec["args"] = list(spec.args)
            captured_spec["env"] = dict(spec.env)
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
            ensure_comment_relay=_recording_relay,
        )
        await asyncio.sleep(0)
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)

    bridge_dir = bridge_mod.bridge_dir_for_bridge_id(session_id)
    iso_home = bridge_mod.agy_home_dir(bridge_dir)
    iso_gemini = bridge_mod.agy_gemini_dir(bridge_dir)

    # 1) The relay starter was invoked for this session's bridge dir.
    assert len(relay_calls) == 1
    assert relay_calls[0]["session_id"] == session_id
    assert relay_calls[0]["explicit_bridge_dir"] == bridge_dir
    assert relay_calls[0]["await_notify"] is False

    # 2) The relay mcp_config.json landed in the ISOLATED agy HOME, not ~/.gemini.
    mcp_config = iso_home / ".gemini" / "config" / "mcp_config.json"
    assert mcp_config.is_file()
    payload = json.loads(mcp_config.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["omnigent"]
    assert server["args"][:4] == ["-I", "-m", "omnigent.claude_native_bridge", "serve-mcp"]
    assert str(bridge_dir) in server["args"]
    assert "sys_session_create" in server["enabledTools"]
    # The bridge token the shared relay needs was written into the bridge dir.
    assert (bridge_dir / "bridge.json").is_file()

    # 3) The launch args point agy at the isolated Gemini dir, while HOME stays
    #    real so platform auth such as macOS Keychain keeps working.
    assert captured_spec["args"] == [f"--gemini_dir={iso_gemini}"]
    assert "HOME" not in captured_spec["env"]
    assert captured_spec["env"]["AGY_ENV"] == "1"

    # 4) The session workspace is pre-trusted AND the feedback survey is disabled
    #    in the SAME isolated settings.json agy reads under --gemini_dir (#1598 +
    #    #1494), proving the trust seed and the survey-disable compose in the
    #    isolated dir without one clobbering the other (the rebase conflict point).
    settings = iso_gemini / "antigravity-cli" / "settings.json"
    assert settings.is_file()
    settings_data = json.loads(settings.read_text(encoding="utf-8"))
    workspace_key = str((tmp_path / "workspace").resolve())
    assert workspace_key in settings_data.get("trustedWorkspaces", [])
    assert settings_data.get("showFeedbackSurvey") is False


@pytest.mark.asyncio
async def test_auto_create_antigravity_prepends_gemini_dir_to_generated_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--gemini_dir`` is inserted right after the binary, ahead of every other flag.

    The sibling relay-wiring test mocks ``build_agy_launch`` to emit no flags, so it
    cannot prove the insertion does not corrupt the order of the REAL generated
    flags (``--conversation``, ``--model``, ``--dangerously-skip-permissions``, and
    pass-through ``extra_args``). agy global flags must precede any positional /
    subcommand token, so ``--gemini_dir`` is prepended at index 0 and the rest of
    argv is preserved verbatim. This guards that invariant against a future change
    to the argv-composition line in ``_auto_create_antigravity_terminal``.
    """
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    from omnigent import antigravity_native_bridge as bridge_mod
    from omnigent.runner import app as runner_app_mod

    session_id = "976793baf55bcdf96830aa376e394f80"
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", list)

    # A realistic build_agy_launch output: binary + a full set of generated flags.
    # The argv-composition line must preserve these verbatim after the prepend.
    generated_tail = [
        "--conversation",
        "abc-123",
        "--model",
        "gemini-2.5-pro",
        "--dangerously-skip-permissions",
        "--print-timeout",
        "30",
    ]
    monkeypatch.setattr(
        launch_mod,
        "build_agy_launch",
        lambda **_kwargs: (("agy", *generated_tail), {}),
    )

    def _noop_reader(*_args: Any, **_kwargs: Any) -> Any:
        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _noop_reader)

    async def _recording_relay(_session_id_arg: str, **_kwargs: Any) -> None:
        return None

    captured_spec: dict[str, Any] = {}
    snapshot = {"workspace": str(tmp_path / "workspace")}

    class _SnapshotServerClient:
        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(200, json=snapshot, request=httpx.Request("GET", url))

        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            del json
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            captured_spec["command"] = spec.command
            captured_spec["args"] = list(spec.args)
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
            ensure_comment_relay=_recording_relay,
        )
        await asyncio.sleep(0)
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)

    bridge_dir = bridge_mod.bridge_dir_for_bridge_id(session_id)
    iso_gemini = bridge_mod.agy_gemini_dir(bridge_dir)

    # The terminal command stays the agy binary; --gemini_dir leads the arg list and
    # every generated flag follows in its original order (none dropped or reordered).
    assert captured_spec["command"] == "agy"
    assert captured_spec["args"] == [f"--gemini_dir={iso_gemini}", *generated_tail]


@pytest.mark.parametrize("parent_host_id", ["21506f91db53823dba9a99e9b0db742d", None])
@pytest.mark.asyncio
async def test_codex_subagent_always_needs_runner_terminal(
    parent_host_id: str | None,
) -> None:
    """
    Codex-native sub-agent children always need a runner-created terminal.

    A sub-agent child (created via ``sys_session_send``) carries a
    ``parent_session_id`` but no ``host_id`` of its own, and no CLI ever
    manages its terminal. The gate must therefore return ``True`` regardless
    of whether the PARENT was host-spawned (``host_id`` present) or CLI-driven
    (``host_id`` None).

    The ``parent_host_id=None`` case is the regression: gating the child
    on the parent's ``host_id`` made codex-native sub-agents under a CLI-driven
    parent (e.g. nessie run via ``omnigent run --server``) silently never get
    a terminal, so ``sys_session_send`` dispatch no-op'd. If that case returns
    ``False``, the regression has reappeared.

    :param parent_host_id: The parent session's ``host_id`` value to simulate;
        ``"21506f91db53823dba9a99e9b0db742d"`` (web-UI parent) or ``None`` (CLI-driven parent).
    """
    from omnigent.runner.app import _codex_session_needs_runner_terminal

    class _Client:
        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """
            Return child then parent session snapshots.

            :param url: Omnigent session snapshot URL.
            :param timeout: HTTP timeout in seconds.
            :returns: Fake Omnigent session response.
            """
            del timeout
            if url.endswith("/ff5cac23d0beb79fad914046049f32ff"):
                return httpx.Response(
                    200,
                    json={
                        "id": "ff5cac23d0beb79fad914046049f32ff",
                        "parent_session_id": "ead6d59a6b650d19dbdf61ec32426f4e",
                        "host_id": None,
                    },
                    request=httpx.Request("GET", url),
                )
            if url.endswith("/ead6d59a6b650d19dbdf61ec32426f4e"):
                return httpx.Response(
                    200,
                    json={"id": "ead6d59a6b650d19dbdf61ec32426f4e", "host_id": parent_host_id},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(404, request=httpx.Request("GET", url))

    # True for both parent host_ids; a False for the None parent = regressed.
    assert (
        await _codex_session_needs_runner_terminal(_Client(), "ff5cac23d0beb79fad914046049f32ff")
        is True
    )


@pytest.mark.asyncio
async def test_codex_session_needs_runner_terminal_false_without_client() -> None:
    """
    With no server client (embedded/test runner) the gate cannot confirm a
    host-spawned or sub-agent session, so it returns ``False`` — skipping
    auto-create rather than risking a competing setup.
    """
    from omnigent.runner.app import _codex_session_needs_runner_terminal

    assert (
        await _codex_session_needs_runner_terminal(None, "8af356d908005a65f872c246158c6293")
        is False
    )


@pytest.mark.asyncio
async def test_codex_discover_thread_and_forward_cleans_up_on_discovery_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    When the fresh TUI never starts a thread, the background task must close
    the listener AND the per-session app-server and drop it from the registry.
    Otherwise each failed host-spawned codex session orphans an app-server
    subprocess (and a dangling listener) for the runner's lifetime.
    """
    from omnigent import codex_native_forwarder
    from omnigent.runner.app import (
        _AUTO_CODEX_APP_SERVERS,
        _codex_discover_thread_and_forward,
    )

    closed = {"client": False, "app_server": False}

    class _Client:
        async def close(self) -> None:
            closed["client"] = True

    class _AppServer:
        async def close(self) -> None:
            closed["app_server"] = True

    async def _raise_no_thread(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError("no thread/started observed")

    # The helper lazily imports wait_for_thread_started from the forwarder
    # module on each call, so patching the module attribute takes effect.
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", _raise_no_thread)

    session_id = "2053b47e49239a8c24e3cd30cdb21c8e"
    _AUTO_CODEX_APP_SERVERS[session_id] = _AppServer()
    try:
        await _codex_discover_thread_and_forward(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            codex_home=tmp_path / "codex-home",
            event_client=_Client(),  # type: ignore[arg-type]
        )
    finally:
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # client closed = no dangling reader task/socket; app_server closed = no
    # orphaned subprocess; dropped from registry = no leaked dict reference.
    assert closed["client"] is True
    assert closed["app_server"] is True
    assert session_id not in _AUTO_CODEX_APP_SERVERS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_cause"),
    [
        (TimeoutError("no thread/started observed"), "startup timed out"),
        (
            RuntimeError("event stream ended"),
            "event stream ended before a thread was created",
        ),
    ],
)
async def test_codex_discover_thread_and_forward_records_accurate_startup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc: Exception,
    expected_cause: str,
) -> None:
    """
    The startup breadcrumb must describe the actual failure mode: a timeout
    reads as "startup timed out", while a RuntimeError (TUI exited / event
    stream ended) must NOT be mislabeled as a timeout.
    """
    from omnigent import codex_native_forwarder
    from omnigent.codex_native_bridge import read_bridge_startup_error
    from omnigent.runner.app import (
        _AUTO_CODEX_APP_SERVERS,
        _codex_discover_thread_and_forward,
    )

    class _Client:
        async def close(self) -> None:
            return None

    class _AppServer:
        async def close(self) -> None:
            return None

    async def _raise(*_args: object, **_kwargs: object) -> str:
        raise exc

    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", _raise)

    session_id = "5cb1fea582a3bd8aad3619ca820af75b"
    _AUTO_CODEX_APP_SERVERS[session_id] = _AppServer()
    try:
        await _codex_discover_thread_and_forward(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            codex_home=tmp_path / "codex-home",
            event_client=_Client(),  # type: ignore[arg-type]
        )
    finally:
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    recorded = read_bridge_startup_error(tmp_path)
    assert recorded is not None
    assert expected_cause in recorded
    assert type(exc).__name__ in recorded
    # A RuntimeError must never be described as a timeout.
    if not isinstance(exc, TimeoutError):
        assert "timed out" not in recorded
