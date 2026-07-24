"""Tests for native session lifecycle, status, interrupt, and stop events."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from omnigent import (
    claude_native_bridge,
    codex_native_bridge,
    kiro_native_bridge,
)
from omnigent.claude_native_bridge import (
    bridge_dir_for_conversation_id,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import (
    KIRO_NATIVE_TERMINAL_ROLE,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


class _EventRecordingServerClient(NullServerClient):
    """Records Omnigent ``external_*`` event POSTs for assertion.

    Subclasses :class:`NullServerClient` so all other runner→AP calls still
    succeed silently; captures ``external_conversation_item`` bodies so a
    test can assert that NO interrupt marker was persisted, and
    ``external_mcp_startup`` bodies so Stop tests can assert the cancelled
    MCP map was published.
    """

    def __init__(self) -> None:
        self.posted_items: list[dict[str, Any]] = []
        self.posted_mcp_startup: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Record ``external_conversation_item`` / ``external_mcp_startup`` bodies."""
        del url
        body = kwargs.get("json")
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            self.posted_items.append(body.get("data") or {})
        if isinstance(body, dict) and body.get("type") == "external_mcp_startup":
            self.posted_mcp_startup.append(body.get("data") or {})
        return self._Response()


class _RecordingCodexAppServerClient:
    """
    Test double for Codex app-server JSON-RPC controls.

    :param transport: Transport passed to
        :func:`omnigent.codex_native_app_server.client_for_transport`, e.g.
        ``"ws://127.0.0.1:1234"``.
    :param client_name: App-server client name, e.g.
        ``"omnigent-codex-native-runner"``.
    """

    def __init__(self, transport: str, client_name: str) -> None:
        self.transport = transport
        self.client_name = client_name
        self.connected = False
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.model_list_responses: list[dict[str, Any]] = []

    async def connect(self) -> None:
        """
        Mark the fake client connected.

        :returns: None.
        """
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture a JSON-RPC request.

        :param method: JSON-RPC method, e.g. ``"turn/interrupt"``.
        :param params: JSON-RPC params, e.g.
            ``{"threadId": "thread_123", "turnId": "turn_123"}``.
        :returns: Empty successful JSON-RPC result.
        """
        self.requests.append((method, params))
        if method == "model/list" and self.model_list_responses:
            return self.model_list_responses.pop(0)
        return {"result": {}}

    async def close(self) -> None:
        """
        Mark the fake client closed.

        :returns: None.
        """
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_payload,expected_params",
    [
        (
            {"type": "model_change", "model": "gpt-5.4"},
            {"threadId": "thread_codex", "model": "gpt-5.4"},
        ),
        (
            {"type": "effort_change", "effort": "xhigh"},
            {"threadId": "thread_codex", "effort": "xhigh"},
        ),
        (
            {"type": "plan_mode_change", "enabled": True},
            {
                "threadId": "thread_codex",
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {
                        "model": "gpt-5.4",
                        "reasoning_effort": None,
                        "developer_instructions": None,
                    },
                },
            },
        ),
    ],
    ids=["model_change", "effort_change", "plan_mode_change"],
)
async def test_events_codex_native_settings_change_uses_thread_settings_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_payload: dict[str, Any],
    expected_params: dict[str, Any],
) -> None:
    """
    Codex-native model / effort updates call ``thread/settings/update``.

    The web UI persists model and effort through Omnigent's normal session
    PATCH path. The runner must translate the forwarded control event into
    Codex app-server's structured settings RPC, not type into the terminal or
    204 as a no-op. The update is a next-turn setting: it is valid even when
    no active turn id is recorded.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = "524fe55f9d5a7f66fec5c5401a930b84"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43210",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the recorded bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5.4"},
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json=event_payload,
        )

    assert resp.status_code == 204, (
        f"codex-native {event_payload['type']} must return 204; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert fake_client.connected
    assert fake_client.closed
    assert fake_client.requests == [
        ("thread/settings/update", expected_params),
    ], (
        f"codex-native {event_payload['type']} must call thread/settings/update "
        f"with next-turn settings; got {fake_client.requests!r}."
    )


@pytest.mark.asyncio
async def test_opencode_native_model_options_uses_cli_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent import opencode_native_app_server, opencode_native_bridge
    from omnigent.opencode_native_bridge import OpenCodeNativeBridgeState
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_opencode_native_model_options"
    monkeypatch.setattr(opencode_native_bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(
        opencode_native_bridge,
        "read_bridge_state",
        lambda _dir: OpenCodeNativeBridgeState(
            session_id=conv_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id="ses_1",
        ),
    )
    captured_envs: list[Mapping[str, str] | None] = []

    def _fake_list_options(*, env: Mapping[str, str] | None = None) -> list[dict[str, object]]:
        captured_envs.append(env)
        return [{"id": "opencode-go/glm-5.2", "displayName": "opencode-go/glm-5.2"}]

    monkeypatch.setattr(
        opencode_native_app_server,
        "list_opencode_cli_model_options",
        _fake_list_options,
    )
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        response = await client.get(f"/v1/sessions/{conv_id}/codex-model-options")

    assert response.status_code == 200
    assert response.json() == {
        "models": [{"id": "opencode-go/glm-5.2", "displayName": "opencode-go/glm-5.2"}]
    }
    assert len(captured_envs) == 1
    cli_env = captured_envs[0]
    assert cli_env is not None
    bridge_dir = opencode_native_bridge.bridge_dir_for_bridge_id(conv_id)
    assert cli_env["XDG_DATA_HOME"] == str(bridge_dir / "xdg-data")
    assert cli_env["XDG_CONFIG_HOME"] == str(bridge_dir / "xdg-config")


@pytest.mark.asyncio
async def test_codex_native_model_options_returns_503_until_bridge_state_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner model-options endpoint is retryable before Codex bridge startup.

    The AP server caches successful runner responses. A codex-native runner
    must therefore not return ``200 {"models": []}`` while the Codex terminal
    is still creating its app-server bridge; that would permanently hide the
    Web UI model picker for the session.
    """
    from omnigent import codex_native_app_server

    conv_id = "d2f0a2d856bc03c1674d3d634b4f250c"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    def _client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Fail the test if the endpoint reaches Codex without bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Never returns; raises if called.
        """
        raise AssertionError(
            f"client_for_transport must not be called before bridge state exists: "
            f"{transport=} {client_name=}"
        )

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.get(f"/v1/sessions/{conv_id}/codex-model-options")

    # A retryable 503 keeps the AP server from caching an empty model list;
    # returning 200 here would recreate the missing-picker regression.
    assert resp.status_code == 503, resp.text
    assert resp.json() == {
        "error": "codex_native_model_options_failed",
        "detail": "Codex-native model options are not ready yet.",
    }


@pytest.mark.asyncio
async def test_codex_native_model_options_query_model_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner model-options endpoint queries Codex ``model/list``.

    The Web UI must not carry its own Codex model / effort catalog. The
    runner is the process that can reach the session's Codex app-server, so
    this endpoint should ask Codex for models and return those model objects
    unchanged for the AP snapshot.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = "68ba0a62ebe928d26adf37c8974ce1eb"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43210",
        client_name="omnigent-codex-native-runner",
    )
    fake_client.model_list_responses = [
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.5",
                        "model": "databricks-gpt-5-5",
                        "displayName": "GPT-5.5",
                        "defaultReasoningEffort": "high",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "Low"},
                            {"reasoningEffort": "medium", "description": "Medium"},
                        ],
                        "isDefault": True,
                    }
                ],
                "nextCursor": "next-page",
            }
        },
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4-mini",
                        "model": "databricks-gpt-5-4-mini",
                        "displayName": "GPT-5.4 mini",
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "minimal", "description": "Minimal"}
                        ],
                        "isDefault": False,
                    }
                ],
                "nextCursor": None,
            }
        },
    ]

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the recorded bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client scripted with ``model/list`` pages.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.get(f"/v1/sessions/{conv_id}/codex-model-options")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "models": [
            {
                "id": "gpt-5.5",
                "model": "databricks-gpt-5-5",
                "displayName": "GPT-5.5",
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low"},
                    {"reasoningEffort": "medium", "description": "Medium"},
                ],
                "isDefault": True,
            },
            {
                "id": "gpt-5.4-mini",
                "model": "databricks-gpt-5-4-mini",
                "displayName": "GPT-5.4 mini",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "minimal", "description": "Minimal"}
                ],
                "isDefault": False,
            },
        ]
    }
    assert fake_client.requests == [
        ("model/list", {"includeHidden": False}),
        ("model/list", {"includeHidden": False, "cursor": "next-page"}),
    ]
    assert fake_client.connected
    assert fake_client.closed


@pytest.mark.asyncio
async def test_claude_native_model_options_use_session_launch_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner exposes friendly aliases from one cached Claude config."""
    from omnigent.claude_native import ClaudeNativeUcodeConfig

    conv_id = "6a416804870ed618cc8908f5cebab937"
    claude_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return claude_spec

    config = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-10",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "system.ai.claude-haiku-4-5",
        },
        api_key_helper="printf token",
        model="system.ai.claude-opus-4-10",
    )
    resolved_specs: list[AgentSpec | None] = []

    def _resolve(*, spec: AgentSpec | None) -> ClaudeNativeUcodeConfig:
        resolved_specs.append(spec)
        return config

    monkeypatch.setattr("omnigent.claude_native.resolve_native_claude_config", _resolve)

    async def _fake_auto_create(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        **kwargs: Any,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        resolver = kwargs.get("resolve_launch_config")
        recorder = kwargs.get("record_launch_config")
        assert callable(resolver)
        assert callable(recorder)
        recorder(session_id, await resolver())
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _fake_auto_create)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        first = await client.get(f"/v1/sessions/{conv_id}/claude-model-options")
        second = await client.get(f"/v1/sessions/{conv_id}/claude-model-options")

    expected = {
        "models": [
            {
                "id": "opus",
                "model": "system.ai.claude-opus-4-10",
                "displayName": "Opus 4.10",
                "isDefault": True,
            },
            {
                "id": "haiku",
                "model": "system.ai.claude-haiku-4-5",
                "displayName": "Haiku 4.5",
                "isDefault": False,
            },
        ]
    }
    assert first.status_code == 200
    assert first.json() == expected
    assert second.json() == expected
    # Auto-create and both UI reads shared one launch-time live query.
    assert resolved_specs == [claude_spec]


@pytest.mark.asyncio
async def test_claude_native_model_options_config_error_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authoritative-empty catalog answers 424, not the retryable 503.

    The AP server treats 503 as a "runner still booting" retry window; a
    configuration failure (workspace exposes no Claude models) can't be
    retried away, so it must use a distinct status.
    """
    import click

    from omnigent.claude_native import ClaudeNativeUcodeConfig

    conv_id = "7b527915981fe729dd9a19a6dfcbca48"
    claude_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return claude_spec

    def _resolve(*, spec: AgentSpec | None) -> ClaudeNativeUcodeConfig:
        del spec
        raise click.ClickException("Databricks profile 'p' exposes no Claude model services.")

    monkeypatch.setattr("omnigent.claude_native.resolve_native_claude_config", _resolve)

    async def _fake_auto_create(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        **kwargs: Any,
    ) -> SessionResourceView:
        del resource_registry, publish_event, kwargs
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _fake_auto_create)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.get(f"/v1/sessions/{conv_id}/claude-model-options")

    assert resp.status_code == 424
    body = resp.json()
    assert body["error"] == "claude_native_model_options_config"
    assert "exposes no Claude model services" in body["detail"]


@pytest.mark.asyncio
async def test_events_codex_native_plan_mode_requires_loaded_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Codex-native Plan-mode updates fail when no Codex bridge is loaded.

    The AP server treats a 2xx runner response as proof that the UI can show
    Plan mode. Returning 204 when no bridge state exists would therefore
    persist a false Plan indicator even though Codex app-server never received
    ``thread/settings/update``.
    """
    conv_id = "290b63ecec11a7ae5b93da19be2d9195"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5.4"},
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the codex-native spec for any agent id.

        :param agent_id: Agent identifier, e.g. ``"880b5afda28ad55ff74cbeb9b5fc67fb"``.
        :param session_id: Session identifier, e.g. ``"d1f9214d74c38b9f9a9db17ed8352dc4"``.
        :returns: Codex-native agent spec.
        """
        del agent_id, session_id
        return codex_native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "plan_mode_change", "enabled": True},
        )

    assert resp.status_code == 503, resp.text
    assert "loaded Codex bridge" in resp.text


@pytest.mark.asyncio
async def test_events_interrupt_on_codex_native_uses_turn_interrupt_without_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` interrupt on a codex-native session calls
    Codex app-server ``turn/interrupt``.

    Codex's TUI interrupt key is only a UI shortcut for the structured
    app-server call. The runner/web path must use the app-server protocol
    directly so Codex validates the active turn id and returns only after the
    abort is accepted. Codex records the interrupt as a turn-status edge, not
    as a message, so the runner still must not synthesize a
    ``[System: interrupted]`` bubble.

    Pins:
    1. ``turn/interrupt`` is sent with the recorded thread/turn ids.
    2. NO ``[System: interrupted]`` marker is persisted to AP.
    3. The session is NOT added to ``_interrupted_sessions``; no marker in
       ``_session_histories``.
    """
    from omnigent import codex_native_app_server
    from omnigent.runner.app import _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = "83d1472d16e3e635c84ca44f29624fca"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_codex",
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43210",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the recorded bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seeds _session_spec_cache so the dispatch detects "codex-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )

        captured_history = list(_session_histories_ref.get(conv_id, []))
        flagged = conv_id in app.state.interrupted_sessions

    assert int_resp.status_code == 204, (
        f"codex-native interrupt must return 204; got {int_resp.status_code}: {int_resp.text}"
    )

    # 1) The runner reached Codex app-server's structured interrupt path. If
    # this is empty, the handler regressed to a terminal-only or no-op cancel.
    assert fake_client.connected
    assert fake_client.closed
    assert fake_client.requests == [
        (
            "turn/interrupt",
            {
                "threadId": "thread_codex",
                "turnId": "turn_codex",
            },
        )
    ], (
        f"codex-native interrupt must call turn/interrupt with the active "
        f"thread/turn ids; got {fake_client.requests!r}."
    )

    # 2) NO marker persisted — a synthesized [System: interrupted] would diverge
    # the web UI from Codex's own session (the mismatch this revert removes).
    marker_texts = [
        b.get("text")
        for data in server_client.posted_items
        for b in (data.get("item_data") or {}).get("content", [])
        if isinstance(b, dict)
    ]
    assert not any("interrupted" in (t or "").lower() for t in marker_texts), (
        f"codex-native interrupt must NOT persist an interrupted marker; "
        f"posted item texts were {marker_texts!r}."
    )

    # 3) Not flagged, and nothing leaks into the runner's in-memory history.
    assert not flagged, f"codex-native session {conv_id!r} must not be flagged interrupted."
    assert all(
        not (
            h.get("role") == "user"
            and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
        )
        for h in captured_history
    ), f"no interrupt marker should enter _session_histories; got {captured_history!r}"


@pytest.mark.asyncio
async def test_events_stop_session_on_codex_native_uses_turn_interrupt_without_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` ``stop_session`` on codex-native interrupts the active turn.

    Regression guard for the cancel-floor work: ``stop_session`` only
    special-cased claude-native, so codex-native fell into
    ``_cancel_inprocess_turn``, which flags the session interrupted and (on the
    next turn or a live-task race) synthesizes the ``[System: interrupted]``
    marker Codex never emits. codex-native must reach the same app-server
    ``turn/interrupt`` path as the interrupt branch.

    Pins (sister to ``...interrupt_on_codex_native...``):
    1. ``turn/interrupt`` is sent with the recorded thread/turn ids.
    2. NO ``[System: interrupted]`` marker is persisted to AP.
    3. The session is NOT added to ``_interrupted_sessions``; no marker leaks
       into ``_session_histories``.
    """
    from omnigent import codex_native_app_server
    from omnigent.runner.app import _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = "fa87fda193a47e99e6a2599e44032807"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43211",
            thread_id="thread_codex_stop",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_codex_stop",
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43211",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the stop-session path.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43211"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )

        captured_history = list(_session_histories_ref.get(conv_id, []))
        flagged = conv_id in app.state.interrupted_sessions

    assert stop_resp.status_code == 204, (
        f"codex-native stop_session must return 204; got {stop_resp.status_code}: {stop_resp.text}"
    )

    # 1) The runner reached Codex app-server's structured interrupt path. If
    # this is empty, stop_session regressed to the in-process cancel floor or
    # the old terminal-key path.
    assert fake_client.connected
    assert fake_client.closed
    assert fake_client.requests == [
        (
            "turn/interrupt",
            {
                "threadId": "thread_codex_stop",
                "turnId": "turn_codex_stop",
            },
        )
    ], (
        f"codex-native stop_session must call turn/interrupt with the active "
        f"thread/turn ids; got {fake_client.requests!r}."
    )

    # 2) NO marker persisted — the in-process floor would have synthesized one.
    marker_texts = [
        b.get("text")
        for data in server_client.posted_items
        for b in (data.get("item_data") or {}).get("content", [])
        if isinstance(b, dict)
    ]
    assert not any("interrupted" in (t or "").lower() for t in marker_texts), (
        f"codex-native stop_session must NOT persist an interrupted marker; "
        f"posted item texts were {marker_texts!r}."
    )

    # 3) Not flagged (the in-process floor's _interrupted_sessions.add never ran),
    # and nothing leaks into the runner's in-memory history.
    assert not flagged, (
        f"codex-native session {conv_id!r} must not be flagged interrupted — a "
        f"stale flag would taint the next turn with a bogus marker."
    )
    assert all(
        not (
            h.get("role") == "user"
            and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
        )
        for h in captured_history
    ), f"no interrupt marker should enter _session_histories; got {captured_history!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop_session"])
async def test_events_stop_on_codex_native_cancels_mcp_startup_without_active_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_type: str,
) -> None:
    """
    Stop/interrupt with no active turn cancels in-flight MCP startup.

    During codex-native startup no turn id is recorded yet, so Stop used to
    204 no-op while Codex sat wedged on a slow or failing MCP server
    (issue #2058). The handler must flip the bridge's pending servers to
    ``cancelled`` (unblocking the executor's first-turn gate) and send the
    Codex TUI's startup interrupt — ``turn/interrupt`` with an empty turn
    id — instead of doing nothing.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = f"36ea25fd09df4a2d85136100fbecd3e9{event_type}"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    # Abort the session-create auto-terminal path before it reaches
    # ``clear_bridge_state`` — otherwise the seeded bridge state below is
    # wiped on hosts where the codex CLI/provider config exist (in CI the
    # auto-create aborts on its own before the clear).
    from omnigent.runner import app as runner_app_module

    async def _fail_launch_config(**kwargs: Any) -> None:
        """Abort codex auto-create before it clears bridge state."""
        del kwargs
        raise RuntimeError("launch config disabled in test")

    monkeypatch.setattr(runner_app_module, "_codex_native_launch_config", _fail_launch_config)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43212",
            thread_id="thread_codex_mcp",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    codex_native_bridge.update_mcp_server_startup(bridge_dir, "storage-console", "starting")

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43212",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the startup-cancel path.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43212"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": event_type},
        )

    assert stop_resp.status_code == 204, (
        f"codex-native {event_type} must return 204; got {stop_resp.status_code}: {stop_resp.text}"
    )
    # The Codex TUI's startup interrupt shape: turn/interrupt with an
    # EMPTY turn id (its ``startup_interrupt``); a recorded-turn shape
    # here would be rejected by the app-server mid-startup.
    assert fake_client.requests == [
        (
            "turn/interrupt",
            {"threadId": "thread_codex_mcp", "turnId": ""},
        )
    ], (
        f"codex-native {event_type} during MCP startup must send the startup "
        f"interrupt (empty turnId); got {fake_client.requests!r}."
    )
    # The local flip is authoritative even if Codex never acknowledges
    # the interrupt.
    assert codex_native_bridge.read_mcp_startup(bridge_dir) == {
        "storage-console": {"status": "cancelled", "error": None}
    }
    # And the flipped map is PUBLISHED: the forwarder only reposts when it
    # changes the map itself and codex's cancelled edges are owner-only,
    # so without this post the web band would stay stuck on "starting".
    assert server_client.posted_mcp_startup == [
        {"servers": {"storage-console": {"status": "cancelled", "error": None}}}
    ], (
        f"codex-native {event_type} must publish the cancelled MCP map; "
        f"got {server_client.posted_mcp_startup!r}."
    )


@pytest.mark.asyncio
async def test_events_interrupt_on_codex_native_with_turn_and_mcp_stops_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stop during a startup-deferred turn interrupts the turn AND the startup.

    Codex accepts ``turn/start`` mid-MCP-startup and defers its execution
    until the round settles, so a Stop pressed in that window finds an
    active turn id recorded. Interrupting only the turn would leave the
    user watching a startup they asked to stop — the handler must also
    send the startup interrupt (empty turn id, best-effort, first) and
    flip the bridge's pending servers to ``cancelled``.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = "14fb6a0dde97fc0f7a58a84e1be2c538"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    # Keep the seeded bridge state alive through session create (see the
    # sister startup-cancel test for why auto-create must abort early).
    from omnigent.runner import app as runner_app_module

    async def _fail_launch_config(**kwargs: Any) -> None:
        """Abort codex auto-create before it clears bridge state."""
        del kwargs
        raise RuntimeError("launch config disabled in test")

    monkeypatch.setattr(runner_app_module, "_codex_native_launch_config", _fail_launch_config)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43214",
            thread_id="thread_codex_dual",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_deferred",
        ),
    )
    codex_native_bridge.update_mcp_server_startup(bridge_dir, "storage-console", "starting")

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43214",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the dual-stop path.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43214"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )

    assert int_resp.status_code == 204, int_resp.text
    # Startup interrupt (empty turnId) first — best-effort — then the
    # recorded turn's interrupt.
    assert fake_client.requests == [
        ("turn/interrupt", {"threadId": "thread_codex_dual", "turnId": ""}),
        ("turn/interrupt", {"threadId": "thread_codex_dual", "turnId": "turn_deferred"}),
    ], f"dual stop must send startup interrupt then turn interrupt; got {fake_client.requests!r}."
    assert codex_native_bridge.read_mcp_startup(bridge_dir) == {
        "storage-console": {"status": "cancelled", "error": None}
    }
    # The cancelled map is published to the session (band + snapshot update).
    assert server_client.posted_mcp_startup == [
        {"servers": {"storage-console": {"status": "cancelled", "error": None}}}
    ]


@pytest.mark.asyncio
async def test_events_interrupt_on_codex_native_without_turn_or_mcp_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stop with no active turn and no pending MCP startup stays a 204 no-op.

    An idle codex-native session must not send spurious ``turn/interrupt``
    requests to the app-server on every Stop press.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = "5cb0fd92163581dee07e5462a93d5021"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    # Keep the seeded bridge state alive through session create (see the
    # sister startup-cancel test for why auto-create must abort early).
    from omnigent.runner import app as runner_app_module

    async def _fail_launch_config(**kwargs: Any) -> None:
        """Abort codex auto-create before it clears bridge state."""
        del kwargs
        raise RuntimeError("launch config disabled in test")

    monkeypatch.setattr(runner_app_module, "_codex_native_launch_config", _fail_launch_config)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43213",
            thread_id="thread_codex_idle",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )

    def _fail_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """Fail the test if the runner opens an app-server connection."""
        raise AssertionError(
            f"idle codex-native interrupt must not reach the app-server; "
            f"attempted connect to {transport!r} as {client_name!r}"
        )

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fail_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )

    assert int_resp.status_code == 204, int_resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop_session"])
async def test_events_interrupt_and_stop_on_pi_native_enqueue_bridge_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_type: str,
) -> None:
    """
    POST ``/events`` interrupt / stop_session on a pi-native session queues an
    interrupt payload to the Pi extension inbox.

    A pi-native turn runs inside the resident Pi TUI process; the runner's
    harness task only enqueues the user message and returns, so the in-process
    cancel floor has nothing to cancel. Both the ``interrupt`` and
    ``stop_session`` dispatch must route to ``_handle_pi_native_interrupt``,
    which drops an ``interrupt`` payload into the bridge inbox for the extension
    to consume via ``ExtensionContext.abort()``.

    Regression guard: both branches originally enumerated only claude-native
    and codex-native, so pi-native silently fell through to the no-op
    ``_cancel_inprocess_turn`` floor — clicking Stop on a Pi turn did nothing.

    Pins:
    1. 204 returned.
    2. An ``interrupt_*`` payload is written to the session's bridge inbox.
    3. NO ``[System: interrupted]`` marker is persisted (the floor never ran).
    """
    import omnigent.pi_native_bridge as pi_native_bridge
    from omnigent.runner.app import _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = uuid.uuid4().hex
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    pi_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the pi-native spec for any agent_id."""
        del agent_id, session_id
        return pi_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seeds _session_spec_cache so the dispatch detects "pi-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": event_type},
        )

        captured_history = list(_session_histories_ref.get(conv_id, []))
        flagged = conv_id in app.state.interrupted_sessions

    assert resp.status_code == 204, (
        f"pi-native {event_type} must return 204; got {resp.status_code}: {resp.text}"
    )

    # 1) The request reached the bridge inbox (the extension's abort channel). If
    # empty, the dispatch fell through to the no-op in-process cancel floor
    # instead of _handle_pi_native_interrupt.
    inbox = pi_native_bridge.bridge_dir_for_session_id(conv_id) / "inbox"
    queued = sorted(p.name for p in inbox.glob("*.json")) if inbox.exists() else []
    assert any("interrupt_" in name for name in queued), (
        f"pi-native {event_type} must enqueue an interrupt payload to the bridge "
        f"inbox; inbox contained {queued!r}."
    )

    # 2) No synthesized marker — pi-native never goes through the in-process floor.
    marker_texts = [
        b.get("text")
        for data in server_client.posted_items
        for b in (data.get("item_data") or {}).get("content", [])
        if isinstance(b, dict)
    ]
    assert not any("interrupted" in (t or "").lower() for t in marker_texts), (
        f"pi-native {event_type} must NOT persist an interrupted marker; got {marker_texts!r}."
    )

    # 3) Not flagged, and nothing leaks into the runner's in-memory history.
    assert not flagged, f"pi-native session {conv_id!r} must not be flagged interrupted."
    assert all(
        not (
            h.get("role") == "user"
            and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
        )
        for h in captured_history
    ), f"no interrupt marker should enter _session_histories; got {captured_history!r}"


@pytest.mark.asyncio
async def test_events_model_change_on_pi_native_enqueues_bridge_model_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` ``model_change`` on a pi-native session queues a
    ``model_change`` payload to the Pi extension inbox.

    A pi-native turn runs inside the resident Pi TUI process, and the
    ``--model`` launch flag is baked in at spawn. The dispatch must route to
    ``_handle_pi_native_model_change``, which drops a ``model_change`` payload
    the extension applies live via Pi's ``setModel``.

    Regression guard: the ``model_change`` branch originally enumerated only
    claude/codex/cursor/opencode/kiro, so pi-native fell through to the no-op
    and a web-picked model never reached the running Pi process.

    Pins:
    1. 204 returned.
    2. A ``model_change_*`` payload carrying the model id is written to the
       session's bridge inbox.
    """
    import json as _json

    import omnigent.pi_native_bridge as pi_native_bridge
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_pi_native_model_change"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    pi_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return pi_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "model_change", "model": "databricks-claude-opus-4-1"},
        )

    assert resp.status_code == 204, (
        f"pi-native model_change must return 204; got {resp.status_code}: {resp.text}"
    )

    inbox = pi_native_bridge.bridge_dir_for_session_id(conv_id) / "inbox"
    payloads = [
        _json.loads(p.read_text(encoding="utf-8"))
        for p in (inbox.glob("*.json") if inbox.exists() else [])
    ]
    model_changes = [p for p in payloads if p.get("type") == "model_change"]
    assert len(model_changes) == 1, (
        f"pi-native model_change must enqueue exactly one payload; got {payloads!r}."
    )
    assert model_changes[0]["model"] == "databricks-claude-opus-4-1"
    assert model_changes[0]["id"].startswith("model_change_")


def test_interrupted_sessions_isolated_per_app_instance() -> None:
    """
    Each ``create_runner_app()`` gets its own ``_interrupted_sessions`` set.

    Regression guard: when ``_interrupted_sessions`` was a module-global,
    interrupt flags leaked between distinct app instances in the same
    process — app1 flagging a conv made app2 append a bogus
    ``[System: interrupted]`` marker on a normal turn for the same conv id.
    Keeping the set closure-local (exposed on ``app.state`` only for test
    inspection) prevents that.
    """
    app1 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app2 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    # Distinct objects: a shared module-global would make these identical, so
    # a flag added to one app would be visible from the other.
    assert app1.state.interrupted_sessions is not app2.state.interrupted_sessions, (
        "Each app instance must own its _interrupted_sessions set; if they are "
        "the same object, the set is module-global again and flags leak across apps."
    )

    app1.state.interrupted_sessions.add("8af356d908005a65f872c246158c6293")
    assert "8af356d908005a65f872c246158c6293" not in app2.state.interrupted_sessions, (
        "app2 must not observe app1's interrupt flag. If it does, "
        "_interrupted_sessions is shared process-global state and a stale flag "
        "would fire a bogus [System: interrupted] marker on app2's next turn."
    )


@pytest.mark.asyncio
async def test_events_stop_session_on_native_kills_tmux_and_publishes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` ``{"type": "stop_session"}`` on a claude-native
    session kills the tmux session and clears the spinner.

    "Stop session" is the web UI affordance for terminating a
    claude-native session without re-attaching to tmux. Unlike
    ``interrupt`` (a single Escape that cancels the current response
    but leaves the session alive), it must:

    1. Call ``kill_session`` with the bridge dir derived from the
       conversation id and the snappy 1.0s timeout — this is what
       actually ends the ``claude`` process.
    2. Enqueue exactly one ``session.status: idle`` event so the web
       UI's "Working…" spinner clears immediately (Claude's ``Stop``
       hook never fires on a hard kill).
    3. NOT append a ``[System: interrupted]`` marker — the session is
       being torn down, not interrupted mid-turn. A stray marker would
       be the interrupt handler leaking into the stop path.
    """
    from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    captured_kill: list[Any] = []

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured_kill.append((bridge_dir, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "1fb90dd3b9d3f24e2356ace505314db1",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            "/v1/sessions/1fb90dd3b9d3f24e2356ace505314db1/events",
            json={"type": "stop_session"},
        )

        captured_history = list(_session_histories_ref.get("1fb90dd3b9d3f24e2356ace505314db1", []))
        queue = _session_event_queues_ref.get("1fb90dd3b9d3f24e2356ace505314db1")
        assert queue is not None, (
            "Session creation should have initialized the event queue "
            "for ``1fb90dd3b9d3f24e2356ace505314db1``; without it ``_publish_event`` had "
            "nowhere to land its idle event."
        )
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # 1) 204 + exactly one kill_session call on the conversation's
    # bridge dir. 0 = the dispatch fell through to the generic
    # forward-to-harness path (which 404s for native — silent
    # regression); 2+ = the handler ran twice.
    assert stop_resp.status_code == 204, (
        f"Native stop_session must return 204 from /events; "
        f"got {stop_resp.status_code}: {stop_resp.text}"
    )
    assert len(captured_kill) == 1, (
        f"Expected one kill_session call, got {len(captured_kill)}. "
        f"If 0, the dispatch in /events did not route to the native "
        f"stop handler — possibly _session_harness_name returned the "
        f"wrong canonical name."
    )
    bridge_dir, timeout_s = captured_kill[0]
    assert bridge_dir == bridge_dir_for_conversation_id("1fb90dd3b9d3f24e2356ace505314db1")
    # 1.0s short timeout: the UI stop must feel snappy. The helper's
    # 30s default would hang the user's click on a missing tmux.json.
    assert timeout_s == 1.0

    # 2) session.status: idle enqueued exactly once so the spinner
    # clears. 0 = _publish_event was skipped; 2+ = double-publish.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert len(status_idle) == 1, (
        f"Expected exactly one session.status: idle event after a "
        f"native stop, got {len(status_idle)}. Full queue: {queued_events!r}."
    )

    # 3) No [System: interrupted] marker — stop is a teardown, not a
    # mid-turn interrupt. A marker here means the interrupt handler's
    # _append_cancellation_items leaked into the stop path.
    markers = [
        h
        for h in captured_history
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert markers == [], (
        f"stop_session must not append a [System: interrupted] marker; "
        f"got {markers!r}. If non-empty, the stop handler is reusing the "
        f"interrupt cleanup path."
    )


@pytest.mark.asyncio
async def test_stop_session_on_native_subagent_reclaims_work_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Hard-stopping a claude-native SUB-AGENT worker reclaims its work entry.

    When the stopped session is a tracked sub-agent, ``_handle_claude_native_stop``
    must mark the work entry ``cancelled`` and deliver a terminal payload to the
    parent's inbox — so the orchestrator (via ``sys_cancel_task`` → ``stop_session``)
    learns the worker is gone instead of waiting on the wrapper's reconnect loop.
    Pre-fix the kill happened but the entry was never reclaimed (the parent could
    hang thinking the worker was still running).
    """
    from omnigent.runner import app as runner_app
    from omnigent.spec.types import ExecutorSpec

    parent_id = "c4315225d4a12d320df065ed1ac8baad"
    worker_id = "8dcfd4c64c7a29cddaefa4af686da1da"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    monkeypatch.setattr(claude_native_bridge, "kill_session", lambda *a, **k: None)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=worker_id,
        agent="claude_code",
        title="task",
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": worker_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
            )
            assert create_resp.status_code == 201, create_resp.text
            stop_resp = await client.post(
                f"/v1/sessions/{worker_id}/events",
                json={"type": "stop_session"},
            )
            assert stop_resp.status_code == 204, stop_resp.text
    finally:
        runner_app.unregister_subagent_work(worker_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # The killed worker's entry was reclaimed: a single cancelled completion
    # landed in the parent's inbox. If 0, the stop path killed the pane but
    # left the parent thinking the worker was still running (the bug).
    assert session_inbox.qsize() == 1, (
        f"Expected one cancelled completion in the parent inbox after stopping "
        f"the worker, got {session_inbox.qsize()}."
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "cancelled"
    assert delivered["task_id"] == worker_id


@pytest.mark.asyncio
async def test_stop_session_on_native_subagent_without_parent_inbox_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Hard-stopping a tracked native sub-agent succeeds after the kill lands.

    ``stop_session`` is user-initiated stop orchestration, not the native
    terminal-status ACK path. Once the pane is killed, the runner must return
    204 so Omnigent can finish host-runner teardown and write the deliberate-stop
    label even if parent delivery cannot be confirmed.
    """
    from omnigent.runner import app as runner_app
    from omnigent.spec.types import ExecutorSpec

    parent_id = "a87dd01585f0c6f0f82f73d74e4124c0"
    worker_id = "d2af8cd6293253c5937d8c7d35fb3d6b"

    monkeypatch.setattr(claude_native_bridge, "kill_session", lambda *a, **k: None)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve every test session to a claude-native spec.

        :param agent_id: Agent id requested by the runner.
        :param session_id: Optional session id being spawned.
        :returns: Native executor spec for the test.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=worker_id,
        agent="claude_code",
        title="task",
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": worker_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
            )
            assert create_resp.status_code == 201, create_resp.text
            stop_resp = await client.post(
                f"/v1/sessions/{worker_id}/events",
                json={"type": "stop_session"},
            )
        entry = runner_app.get_subagent_work(worker_id)
    finally:
        runner_app.unregister_subagent_work(worker_id)

    assert stop_resp.status_code == 204, stop_resp.text
    assert entry is not None
    # The worker was marked cancelled, but delivery is still unconfirmed. The
    # external_session_status path remains responsible for enforcing delivery
    # ACK failures; explicit stop must not report a failed kill after success.
    assert entry.status == "cancelled"
    assert entry.delivered is False


@pytest.mark.asyncio
async def test_events_stop_session_on_native_returns_503_when_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` stop_session returns 503 when ``kill_session``
    can't reach tmux, and publishes no idle.

    Sister to the happy-path test. If the runner can't deliver the
    kill (tmux pane gone, bridge dir not yet advertised) it must
    surface a 503 rather than lie to the web UI with a 204 + idle
    that says "stopped" while the session may still be alive.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "baabd23def56efdbe0b84b9c924aa6a6",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            "/v1/sessions/baabd23def56efdbe0b84b9c924aa6a6/events",
            json={"type": "stop_session"},
        )

        queue = _session_event_queues_ref.get("baabd23def56efdbe0b84b9c924aa6a6")
        assert queue is not None
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert stop_resp.status_code == 503, (
        f"Native stop_session with kill failure must return 503; "
        f"got {stop_resp.status_code}: {stop_resp.text}"
    )
    body = stop_resp.json()
    assert body.get("error") == "claude_native_stop_failed", (
        f"503 body must carry the stop-failure error code; got {body!r}"
    )
    # No idle on the failure path — clearing the spinner would tell the
    # UI the session stopped when the kill didn't actually land.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"No session.status: idle should be enqueued when kill_session "
        f"failed; got {status_idle!r}."
    )


@pytest.mark.asyncio
async def test_events_stop_session_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept stop_session and 204 without killing tmux.

    In-process harnesses have no external tmux process for the runner to
    kill: stop cancels the in-flight turn via the cancel floor, or — with
    no turn in flight, as here — is a clean 204 no-op. The Omnigent server is
    harness-agnostic and forwards stop_session for any session, so the
    runner must accept it and 204 — never reach ``kill_session``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Fail the test if a non-native session reaches the killer."""
        del bridge_dir, timeout_s
        raise AssertionError(
            "kill_session must never be called for non-native sessions — "
            "stop_session is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "aec413c7f4d6fc308bcaa55ad32c3b98",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/aec413c7f4d6fc308bcaa55ad32c3b98/events",
            json={"type": "stop_session"},
        )

    # 204 = dispatch saw a non-native harness and short-circuited
    # before any kill. Anything else means the event leaked into a
    # code path it shouldn't reach.
    assert resp.status_code == 204, (
        f"Non-native stop_session must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_stop_session_closes_terminal_and_publishes_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Native stop tears the session's terminal resource down.

    A host-spawned (web-UI-created) claude-native session has no CLI
    wrapper watching the pane, so after ``kill_session`` ends ``claude``
    nothing else removes the terminal resource — the web UI keeps showing
    a live terminal for the stopped session (the user-reported bug). The
    stop handler must therefore close each of the session's terminals and
    publish ``session.resource.deleted`` so connected clients drop them.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec
    from tests.runner.helpers import make_test_terminal_instance

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Record nothing; the stub terminal needs no real tmux kill."""
        del bridge_dir, timeout_s

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    # Seed the runner's terminal registry with the session's live
    # ``claude:main`` terminal, mirroring what the host-spawned
    # auto-create path leaves behind. Private-attr seed matches the
    # existing resource-registry test convention (no real tmux).
    conv_id = "778e0486ee2f733acdf021ca8334d0bd"
    terminal_registry = TerminalRegistry(
        conversation_link_base_url="http://127.0.0.1:8000",
    )
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("claude", "main")] = instance

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Precondition: the terminal is live before the stop, so a later
        # absence proves the stop closed it (not that it was never there).
        assert terminal_registry.get(conv_id, "claude", "main") is not None

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )

        queue = _session_event_queues_ref.get(conv_id)
        queued_events: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert stop_resp.status_code == 204, stop_resp.text

    # The terminal is gone from the registry → the resource list the web
    # UI reads no longer shows a live terminal. Still present = the stop
    # handler skipped teardown (the bug this guards against).
    assert terminal_registry.get(conv_id, "claude", "main") is None, (
        "stop_session must close the session's terminal; it is still "
        "registered, so the web UI would keep showing a live terminal."
    )

    # Exactly one session.resource.deleted for the claude terminal so
    # connected clients drop it live (the server relay also persists it).
    # 0 = teardown didn't publish (UI never updates); 2+ = double-publish.
    deleted = [e for e in queued_events if e.get("type") == "session.resource.deleted"]
    assert deleted == [
        {
            "type": "session.resource.deleted",
            "resource_id": "terminal_claude_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
    ], f"expected one terminal session.resource.deleted event, got {deleted!r}"


@pytest.mark.asyncio
async def test_required_terminal_exit_publishes_deleted_and_failed(tmp_path: Path) -> None:
    """
    A required terminal disappearing fails the owning session.

    This uses a generic ``worker`` terminal name to pin the lifecycle rule,
    not a Claude-specific branch: if the terminal was registered as required,
    the runner must publish both resource deletion and ``session.status:
    failed`` when its watcher reports that tmux disappeared.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    parent_id = uuid.uuid4().hex
    conv_id = uuid.uuid4().hex
    parent_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    instance.command = "worker-cli"
    instance.args = ["--profile", "test"]
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot("startup failed\ncomplete setup first")
    terminal_registry._by_conversation.setdefault(conv_id, {})[("worker", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s
        callbacks["on_exit"] = on_exit
        callbacks["replace"] = replace

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    runner_app._session_inboxes_ref[parent_id] = parent_inbox
    runner_app.register_child_session(
        conv_id,
        parent_session_id=parent_id,
        title="worker:main",
        tool="worker",
        session_name="main",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=conv_id,
        agent="worker",
        title="main",
    )

    async def _collect_exit_events() -> list[dict[str, Any]]:
        while True:
            queue = _session_event_queues_ref.get(conv_id)
            if queue is not None and queue.qsize() >= 2:
                events: list[dict[str, Any]] = []
                while not queue.empty():
                    item = queue.get_nowait()
                    if isinstance(item, dict):
                        events.append(item)
                return events
            await asyncio.sleep(0)

    try:
        await resource_registry.observe_required_terminal(
            conv_id,
            "worker",
            "main",
            instance,
        )
        on_exit = callbacks.get("on_exit")
        assert callable(on_exit)
        on_exit()
        queued_events = await asyncio.wait_for(_collect_exit_events(), timeout=1.0)
        for _ in range(100):
            if pm.released:
                break
            await asyncio.sleep(0)
        parent_events = _drain_session_event_queue(_session_event_queues_ref.get(parent_id))
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        _session_event_queues_ref.pop(parent_id, None)
        runner_app.unregister_subagent_work(conv_id)
        runner_app.unregister_child_session(conv_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert terminal_registry.get(conv_id, "worker", "main") is None
    assert {
        "type": "session.resource.deleted",
        "resource_id": "terminal_worker_main",
        "resource_type": "terminal",
        "session_id": conv_id,
    } in queued_events
    failed_events = [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ]
    assert len(failed_events) == 1, f"expected one failed status, got {queued_events!r}"
    assert failed_events[0]["error"]["code"] == "required_terminal_exited"
    assert "Required terminal exited unexpectedly" in failed_events[0]["error"]["message"]
    assert parent_events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": conv_id,
            "child": {
                "id": conv_id,
                "title": "worker:main",
                "tool": "worker",
                "session_name": "main",
                "busy": False,
                "current_task_status": "failed",
                "last_task_error": failed_events[0]["error"],
            },
        }
    ]
    assert pm.released == [conv_id]
    inbox_item = parent_inbox.get_nowait()
    assert inbox_item["status"] == "failed"
    assert "Required terminal exited unexpectedly" in inbox_item["output"]
    assert "command: worker-cli (2 args; argv omitted" in inbox_item["output"]
    assert f"cwd: {tmp_path}" in inbox_item["output"]
    assert "startup failed\ncomplete setup first" in inbox_item["output"]
    assert "Suggested next checks" not in inbox_item["output"]


@pytest.mark.asyncio
async def test_required_terminal_exit_while_idle_does_not_fail_session(tmp_path: Path) -> None:
    """
    A required terminal that exits while the session is idle is a clean shutdown.

    The native agent terminal is long-lived and goes ``idle`` once its turn
    completes. When the pane then disappears, the work for that turn was already
    delivered, so the runner must NOT publish ``session.status: failed`` — doing
    so was the source of spurious "failed" chats in the UI. The terminal
    resource is still removed and the harness subprocess released; the runner
    going offline is surfaced separately via liveness, not a failure.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    parent_id = uuid.uuid4().hex
    conv_id = uuid.uuid4().hex
    parent_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    instance.command = "worker-cli"
    instance.launch_cwd = str(tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("worker", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    runner_app._session_inboxes_ref[parent_id] = parent_inbox
    runner_app.register_child_session(
        conv_id,
        parent_session_id=parent_id,
        title="worker:main",
        tool="worker",
        session_name="main",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=conv_id,
        agent="worker",
        title="main",
    )

    try:
        await resource_registry.observe_required_terminal(
            conv_id,
            "worker",
            "main",
            instance,
        )
        # The session reached idle (turn completed) before the pane vanished.
        resource_registry._last_session_status[conv_id] = "idle"
        on_exit = callbacks.get("on_exit")
        assert callable(on_exit)
        on_exit()
        # Await terminal-exit cleanup deterministically instead of polling;
        # then await any pending harness-release task so ``pm.released`` is set.
        await resource_registry.wait_for_terminal_exit_cleanup()
        release_task_name = f"required-terminal-release:{conv_id}"
        pending_release = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == release_task_name and not task.done()
        ]
        if pending_release:
            await asyncio.gather(*pending_release)

        deleted_event = {
            "type": "session.resource.deleted",
            "resource_id": "terminal_worker_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
        parent_events = _drain_session_event_queue(_session_event_queues_ref.get(parent_id))
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        _session_event_queues_ref.pop(parent_id, None)
        runner_app.unregister_subagent_work(conv_id)
        runner_app.unregister_child_session(conv_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # The terminal resource is still removed...
    assert terminal_registry.get(conv_id, "worker", "main") is None
    assert deleted_event in queued_events
    # ...but no failure is published, and the parent is not woken as failed.
    assert [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ] == []
    assert parent_events == []
    assert parent_inbox.empty()
    # The harness subprocess is still released — the terminal is gone.
    assert pm.released == [conv_id]


@pytest.mark.parametrize("terminal_name", ["qwen", "antigravity"])
@pytest.mark.asyncio
async def test_required_terminal_clean_quit_publishes_idle_not_failed(
    terminal_name: str,
) -> None:
    """A clean ``/quit`` of qwen/antigravity-native is not a crash.

    Both harnesses leave the exit-classification memo stuck on ``running`` at
    quit time — qwen's "powering down" redraw trips the PTY-activity watcher,
    and antigravity-native is deliberately excluded from the PTY ``emit_status``
    role set (the RPC reader owns working-status). So ``session_was_idle`` is
    ``False`` even though the user quit normally. The runner must special-case
    these terminals: publish a final ``idle`` (to clear the web "Working…"
    spinner) and release the harness, but never render the spurious red
    ``required_terminal_exited`` failure card.

    :param terminal_name: The native terminal that the user quit cleanly.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.runner.resource_registry import (
        TerminalExitEvent,
        TerminalLifecycle,
    )

    conv_id = uuid.uuid4().hex
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    resource_registry = app.state.session_resource_registry
    # Grab the runner's terminal-exit publisher (the branch under test) and
    # drive it directly, mimicking the registry firing on a clean quit.
    publish_exit = resource_registry._terminal_exit_publisher
    assert callable(publish_exit)

    try:
        publish_exit(
            TerminalExitEvent(
                session_id=conv_id,
                terminal_id=f"terminal_{terminal_name}_main",
                terminal_name=terminal_name,
                session_key="main",
                lifecycle=TerminalLifecycle.REQUIRED,
                # The memo never flipped to idle, so the generic guard would
                # otherwise misclassify this normal quit as a crash.
                session_was_idle=False,
            )
        )
        queued_events: list[dict[str, Any]] = []
        for _ in range(1000):
            queued_events.extend(
                _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
            )
            if pm.released:
                break
            await asyncio.sleep(0)
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        runner_app.unregister_child_session(conv_id)

    # The terminal resource is removed and a final idle clears the spinner...
    assert {
        "type": "session.resource.deleted",
        "resource_id": f"terminal_{terminal_name}_main",
        "resource_type": "terminal",
        "session_id": conv_id,
    } in queued_events
    assert {"type": "session.status", "status": "idle"} in queued_events
    # ...but no spurious failure card renders — the user quit normally.
    assert [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ] == []
    # The harness subprocess is still released — the terminal is gone.
    assert pm.released == [conv_id]


@pytest.mark.asyncio
async def test_external_idle_status_makes_required_terminal_exit_clean(tmp_path: Path) -> None:
    """
    A structured native ``idle`` status prevents a later pane close from failing.

    Kiro completion is observed from its persisted JSONL session, not only from
    PTY diff-idle. After a web turn marks the required terminal ``running``, the
    forwarded ``external_session_status: idle`` must update the same exit memo
    used by the required-terminal watcher; otherwise a normal user close after
    Kiro answered is misclassified as ``required_terminal_exited``.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    conv_id = uuid.uuid4().hex
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("kiro", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("kiro", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry

    try:
        await resource_registry.observe_required_terminal(
            conv_id,
            "kiro",
            "main",
            instance,
            resource_role=KIRO_NATIVE_TERMINAL_ROLE,
        )
        resource_registry.note_session_turn_started(conv_id)
        async with _runner_client(app) as client:
            status_resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
        assert status_resp.status_code == 204, status_resp.text

        on_exit = callbacks.get("on_exit")
        assert callable(on_exit)
        on_exit()
        # Await terminal-exit cleanup deterministically instead of polling;
        # then await any pending harness-release task so ``pm.released`` is set.
        await resource_registry.wait_for_terminal_exit_cleanup()
        release_task_name = f"required-terminal-release:{conv_id}"
        pending_release = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == release_task_name and not task.done()
        ]
        if pending_release:
            await asyncio.gather(*pending_release)

        deleted_event = {
            "type": "session.resource.deleted",
            "resource_id": "terminal_kiro_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
    finally:
        _session_event_queues_ref.pop(conv_id, None)

    assert terminal_registry.get(conv_id, "kiro", "main") is None
    assert deleted_event in queued_events
    assert [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ] == []
    assert pm.released == [conv_id]


@pytest.mark.asyncio
async def test_events_effort_change_on_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"effort_change","effort":"high"}``
    on a claude-native session injects ``/effort high`` into tmux.

    With the unified-effort refactor Omnigent server no longer POSTs to
    ``/claude-native-effort`` — every PATCH effort goes through the
    generic ``/events`` path. The runner's ``/events`` dispatch must
    recognize the native harness and route to
    ``_handle_claude_native_effort_change``, which assembles the
    slash command and types it into the pane.

    A regression in the dispatch (wrong harness name, missing branch)
    would fall through to the generic harness-forward and 404, leaving
    the dropdown click silently ineffective.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, command, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seed _session_spec_cache so /events can detect "claude-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "c7e9584b9bb34910a0068521106c1abc",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (the claude-native auto-create path
        # enqueues session.terminal_pending) so the post-effort_change
        # drain below isolates only what the control event emits.
        _drain_session_event_queue(
            _session_event_queues_ref.get("c7e9584b9bb34910a0068521106c1abc")
        )

        resp = await client.post(
            "/v1/sessions/c7e9584b9bb34910a0068521106c1abc/events",
            json={"type": "effort_change", "effort": "high"},
        )

        # Drain the event queue before delete clears it, so we can
        # assert that effort_change does NOT enqueue spurious events
        # (it's a control signal, not a session-state change).
        queue = _session_event_queues_ref.get("c7e9584b9bb34910a0068521106c1abc")
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 1) 204 = the dispatch correctly routed to the native handler and
    # the handler completed cleanly. 404 would mean the dispatch fell
    # through to the generic harness-forward.
    assert resp.status_code == 204, (
        f"Native effort_change must return 204 from /events; got {resp.status_code}: {resp.text}"
    )
    # 2) Exactly one inject call. 0 = native dispatch missed (likely
    # _session_harness_name returned the wrong canonical name); 2+ =
    # the handler ran twice.
    assert len(captured) == 1, (
        f"Expected one inject_slash_command call from native effort_change, got {len(captured)}."
    )
    bridge_dir, command, timeout_s = captured[0]
    assert bridge_dir == bridge_dir_for_conversation_id("c7e9584b9bb34910a0068521106c1abc")
    # Body contract: ``/effort high`` is the literal Claude Code's TUI
    # accepts. A regression in shape (``/efforthigh``, ``effort high``,
    # missing leading slash) would either 404 on the slash router or
    # land as plain text in the prompt.
    assert command == "/effort high", f"Expected '/effort high' literal, got {command!r}."
    # 1.0s short timeout: missing tmux.json means the pane isn't
    # attached; persisted effort still applies on next spawn. A 30s
    # default would hang the Omnigent PATCH whenever the pane is detached.
    assert timeout_s == 1.0
    # 3) effort_change is a control signal, not a state change.
    # Any session.status enqueued here would mislead the Omnigent relay.
    assert queued_events == [], (
        f"effort_change must not publish session events; got "
        f"{queued_events!r}. If non-empty, the native handler is "
        f"emitting spurious status events."
    )


async def test_events_interrupt_on_kiro_native_routes_to_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interrupt on a kiro-native session sends Escape via the kiro bridge.

    Regression for #1137: kiro-native had no entry in the interrupt dispatch
    ladder, so the web Stop button fell through to the in-process cancel floor —
    a no-op for a TUI turn the harness task already returned from — and silently
    did nothing. This pins that the dispatch routes kiro-native to
    ``kiro_native_bridge.inject_interrupt`` with the snappy 1.0s timeout.
    """
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []
    monkeypatch.setattr(
        kiro_native_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: captured.append((bridge_dir, timeout_s)),
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "kiro-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "cd6b589814147431cc1a92ec2c979998",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        int_resp = await client.post(
            "/v1/sessions/cd6b589814147431cc1a92ec2c979998/events",
            json={"type": "interrupt"},
        )

    assert int_resp.status_code == 204, int_resp.text
    # 0 = the dispatch fell through to the generic path (the silent no-op bug);
    # 2+ = the handler ran twice.
    assert len(captured) == 1, (
        f"Expected one inject_interrupt call, got {len(captured)}. If 0, the "
        f"kiro-native interrupt dispatch entry is missing."
    )
    bridge_dir, timeout_s = captured[0]
    assert bridge_dir == kiro_native_bridge.bridge_dir_for_session_id(
        "cd6b589814147431cc1a92ec2c979998"
    )
    assert timeout_s == 1.0


async def test_events_stop_session_on_kiro_native_kills_tmux_and_publishes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop_session on a kiro-native session kills the tmux pane and clears the spinner.

    Mirrors the goose/claude-native stop path: route to
    ``kiro_native_bridge.kill_session`` and enqueue exactly one
    ``session.status: idle`` (kiro-cli has no Stop hook on a hard kill).
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []
    monkeypatch.setattr(
        kiro_native_bridge,
        "kill_session",
        lambda bridge_dir, *, timeout_s: captured.append((bridge_dir, timeout_s)),
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "kiro-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "cd2a2b575af18bbc3a38fd025e379be0",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        stop_resp = await client.post(
            "/v1/sessions/cd2a2b575af18bbc3a38fd025e379be0/events",
            json={"type": "stop_session"},
        )
        queue = _session_event_queues_ref.get("cd2a2b575af18bbc3a38fd025e379be0")
        queued_events: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert stop_resp.status_code == 204, stop_resp.text
    assert len(captured) == 1, (
        f"Expected one kill_session call, got {len(captured)}. If 0, the "
        f"kiro-native stop dispatch entry is missing."
    )
    bridge_dir, timeout_s = captured[0]
    assert bridge_dir == kiro_native_bridge.bridge_dir_for_session_id(
        "cd2a2b575af18bbc3a38fd025e379be0"
    )
    assert timeout_s == 1.0
    idle_events = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert len(idle_events) == 1, f"stop must publish exactly one idle; got {queued_events!r}"


async def test_events_interrupt_on_kiro_native_503_skips_idle_when_inject_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interrupt returns 503 and publishes no idle when Escape can't reach tmux.

    Failure-path parity with the sibling harnesses (e.g.
    ``..._interrupt_on_native_session_503_skips_cleanup_when_inject_fails``): if
    the bridge can't deliver Escape (pane gone, bridge dir not advertised), the
    runner must surface a 503 and must NOT publish ``session.status: idle`` — idle
    would clear the web-UI spinner while the kiro turn keeps generating. Guards
    against a reorder that moves the idle publish ahead of the ``try``.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(kiro_native_bridge, "inject_interrupt", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "kiro-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "b756aafcdc68c0ed2cf92b34085be5bb",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        int_resp = await client.post(
            "/v1/sessions/b756aafcdc68c0ed2cf92b34085be5bb/events",
            json={"type": "interrupt"},
        )
        queue = _session_event_queues_ref.get("b756aafcdc68c0ed2cf92b34085be5bb")
        queued_events: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert int_resp.status_code == 503, (
        f"kiro-native interrupt with inject_interrupt failure must return 503; "
        f"got {int_resp.status_code}: {int_resp.text}"
    )
    body = int_resp.json()
    assert body.get("error") == "kiro_native_interrupt_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"No session.status: idle should be enqueued when Escape injection "
        f"failed; got {status_idle!r}."
    )


async def test_events_stop_session_on_kiro_native_503_when_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop_session returns 503 and publishes no idle when the kill can't reach tmux.

    Failure-path parity with ``..._stop_session_on_native_returns_503_when_kill_fails``:
    a failed kill must surface 503 rather than lie to the web UI with 204 + idle
    while the ``kiro-cli`` process may still be alive.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(kiro_native_bridge, "kill_session", _fake_kill)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "kiro-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "695c27b61206353f312efe5f6a7ca0f6",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        stop_resp = await client.post(
            "/v1/sessions/695c27b61206353f312efe5f6a7ca0f6/events",
            json={"type": "stop_session"},
        )
        queue = _session_event_queues_ref.get("695c27b61206353f312efe5f6a7ca0f6")
        queued_events: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert stop_resp.status_code == 503, (
        f"kiro-native stop_session with kill failure must return 503; "
        f"got {stop_resp.status_code}: {stop_resp.text}"
    )
    body = stop_resp.json()
    assert body.get("error") == "kiro_native_stop_failed", (
        f"503 body must carry the stop-failure error code; got {body!r}"
    )
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"No session.status: idle should be enqueued when kill_session failed; "
        f"got {status_idle!r}."
    )
