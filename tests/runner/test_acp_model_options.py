"""Runner tests for dynamic ACP model/effort discovery and live settings forwarding."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _runner_client, _spec_resolver_returning
from tests.runner.helpers import NullServerClient


class _FakeAcpHarnessClient:
    """Minimal harness HTTP client recording /model-options and /session-settings."""

    def __init__(
        self,
        *,
        models_payload: dict[str, Any] | None = None,
        models_status: int = 200,
        settings_payload: dict[str, Any] | None = None,
        settings_status: int = 200,
    ) -> None:
        self.models_payload = models_payload or {"models": []}
        self.models_status = models_status
        self.settings_payload = settings_payload or {}
        self.settings_status = settings_status
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, *, timeout: Any = None) -> httpx.Response:
        del timeout
        self.get_calls.append(url)
        req = httpx.Request("GET", f"http://harness.local{url}")
        return httpx.Response(self.models_status, json=self.models_payload, request=req)

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> httpx.Response:
        del timeout
        self.post_calls.append((url, json))
        req = httpx.Request("POST", f"http://harness.local{url}")
        return httpx.Response(self.settings_status, json=self.settings_payload, request=req)


class _AcpControlProcessManager:
    """ProcessManager stub supporting both live-only (``any``) and spawn (``acp``) lookups."""

    handles_tool_dispatch = True

    def __init__(self, client: _FakeAcpHarnessClient, *, initially_live: bool) -> None:
        self._client = client
        self._live = initially_live
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _FakeAcpHarnessClient:
        self.get_client_calls.append((conversation_id, harness, env))
        if harness == "any" and not self._live:
            raise NoLiveHarnessError(conversation_id)
        self._live = True
        return self._client

    def registered_client(self, conversation_id: str) -> _FakeAcpHarnessClient | None:
        del conversation_id
        return self._client if self._live else None

    async def release(self, conversation_id: str, **_kwargs: Any) -> None:
        del conversation_id
        self._live = False


class _RecordingServerClient(NullServerClient):
    """ServerClient stub capturing mirrored ``external_model_change`` posts."""

    def __init__(self) -> None:
        super().__init__()
        self.posted: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, url: str, *, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> NullServerClient._Response:
        del kwargs
        self.posted.append((url, dict(json or {})))
        return NullServerClient._Response()

    async def get_session_model_override(self, session_id: str) -> str | None:
        del session_id
        return None


def _acp_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="acp-dynamic",
        instructions="Test ACP agent",
        executor=ExecutorSpec(
            type="omnigent",
            config={
                "harness": "acp",
                "acp_agent": {
                    "name": "Fake ACP",
                    "command": "fake-acp --stdio",
                },
            },
        ),
    )


async def _build_acp_app(
    pm: _AcpControlProcessManager,
    *,
    server_client: Any = None,
) -> Any:
    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=await _spec_resolver_returning(_acp_spec()),
        server_client=server_client or NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_acp_model_options_route_spawns_on_demand_and_returns_catalog() -> None:
    """GET /v1/sessions/{id}/model-options spawns ACP when cold and returns models + efforts."""
    harness_client = _FakeAcpHarnessClient(
        models_payload={
            "models": [
                {
                    "id": "ag-pro",
                    "displayName": "AG Pro",
                    "isDefault": True,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low"},
                        {"reasoningEffort": "high"},
                    ],
                    "defaultReasoningEffort": "low",
                }
            ]
        }
    )
    pm = _AcpControlProcessManager(harness_client, initially_live=False)
    app = await _build_acp_app(pm)

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "sess_acp_opts", "agent_id": "agent_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # Simulate idle-reaped / cold harness before probing /model-options.
        await pm.release("sess_acp_opts")
        pm.get_client_calls.clear()

        resp = await client.get("/v1/sessions/sess_acp_opts/model-options")

    assert resp.status_code == 200, resp.text
    assert resp.json() == harness_client.models_payload
    assert harness_client.get_calls == ["/v1/sessions/sess_acp_opts/model-options"]
    assert pm.get_client_calls[0][:2] == ("sess_acp_opts", "any")
    assert pm.get_client_calls[1][:2] == ("sess_acp_opts", "acp")
    spawn_env = pm.get_client_calls[1][2]
    assert spawn_env is not None
    assert spawn_env["HARNESS_ACP_COMMAND"] == "fake-acp --stdio"


@pytest.mark.asyncio
async def test_acp_model_options_route_returns_503_on_harness_error() -> None:
    """A failing harness /model-options probe surfaces as HTTP 503."""
    harness_client = _FakeAcpHarnessClient(models_status=500, models_payload={"error": "boom"})
    pm = _AcpControlProcessManager(harness_client, initially_live=True)
    app = await _build_acp_app(pm)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "sess_acp_err", "agent_id": "agent_1"},
        )
        resp = await client.get("/v1/sessions/sess_acp_err/model-options")

    assert resp.status_code == 503, resp.text


@pytest.mark.asyncio
async def test_acp_events_forward_model_and_effort_changes_and_mirror_model() -> None:
    """model_change and effort_change forward to /session-settings and mirror active_model."""
    harness_client = _FakeAcpHarnessClient(
        settings_payload={"active_model": "ag-flash", "active_effort": "high", "applied": True}
    )
    pm = _AcpControlProcessManager(harness_client, initially_live=True)
    server_client = _RecordingServerClient()
    app = await _build_acp_app(pm, server_client=server_client)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "sess_acp_evt", "agent_id": "agent_1"},
        )
        r_effort = await client.post(
            "/v1/sessions/sess_acp_evt/events",
            json={"type": "effort_change", "effort": "high"},
        )
        r_model = await client.post(
            "/v1/sessions/sess_acp_evt/events",
            json={"type": "model_change", "model": "ag-flash"},
        )
        r_reset = await client.post(
            "/v1/sessions/sess_acp_evt/events",
            json={"type": "model_change", "model": None},
        )

    assert r_effort.status_code == 204, r_effort.text
    assert r_model.status_code == 204, r_model.text
    assert r_reset.status_code == 204, r_reset.text
    assert harness_client.post_calls == [
        ("/v1/sessions/sess_acp_evt/session-settings", {"reasoning_effort": "high"}),
        ("/v1/sessions/sess_acp_evt/session-settings", {"model": "ag-flash"}),
        ("/v1/sessions/sess_acp_evt/session-settings", {"model": None}),
    ]
    # Effort updates and model resets (model=None) must not re-pin model_override.
    assert server_client.posted == [
        (
            "/v1/sessions/sess_acp_evt/events",
            {"type": "external_model_change", "data": {"model": "ag-flash"}},
        ),
    ]


@pytest.mark.asyncio
async def test_acp_events_preserve_400_session_settings_rejected() -> None:
    """Runner preserves HTTP 400 error/detail JSON when live ACP settings reject a model."""
    harness_client = _FakeAcpHarnessClient(
        settings_status=400,
        settings_payload={
            "error": "session_settings_rejected",
            "detail": "ACP switch failed",
        },
    )
    pm = _AcpControlProcessManager(harness_client, initially_live=True)
    app = await _build_acp_app(pm)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "sess_acp_rej", "agent_id": "agent_1"},
        )
        resp = await client.post(
            "/v1/sessions/sess_acp_rej/events",
            json={"type": "model_change", "model": "bad-model"},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "error": "session_settings_rejected",
        "detail": "ACP switch failed",
    }


@pytest.mark.asyncio
async def test_acp_events_noop_when_harness_not_live() -> None:
    """Cold ACP sessions return 204 on model_change/effort_change without spawning."""
    harness_client = _FakeAcpHarnessClient()
    pm = _AcpControlProcessManager(harness_client, initially_live=False)
    app = await _build_acp_app(pm)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "sess_acp_cold", "agent_id": "agent_1"},
        )
        await pm.release("sess_acp_cold")
        pm.get_client_calls.clear()

        r1 = await client.post(
            "/v1/sessions/sess_acp_cold/events",
            json={"type": "effort_change", "effort": "medium"},
        )
        r2 = await client.post(
            "/v1/sessions/sess_acp_cold/events",
            json={"type": "model_change", "model": "ag-flash"},
        )

    assert r1.status_code == 204
    assert r2.status_code == 204
    assert harness_client.post_calls == []
    # Only "any" lookup occurred; never spawned an "acp" process.
    assert all(call[1] == "any" for call in pm.get_client_calls)
