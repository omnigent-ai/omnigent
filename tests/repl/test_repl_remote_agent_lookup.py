"""REPL agent lookup on a remote server: an unknown agent name is reported safely."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from omnigent_client import RegisteredAgent, SessionsNamespace
from omnigent_client._sessions import Session

import omnigent.host.identity as identity
from omnigent.chat import _route_remote_session_request
from omnigent.repl._repl import _SessionsChatReplAdapter


@pytest.mark.asyncio
async def test_remote_repl_unknown_agent_error_is_escaped() -> None:
    """A missing agent name keeps its LookupError, with control characters escaped."""

    async def _resolve_agent(name: str) -> object:
        raise LookupError(f"No agent named {name!r} is registered on this server.")

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "readiness", "expected_error"),
    [
        ("online-runner", True, None),
        ("local-host", True, None),
        ("mismatched-runner", True, None),
        ("local-host", "needs-auth", None),
        ("local-host", "binary-missing", "binary-missing"),
        ("other-host", True, "other machines"),
        ("no-host", True, "no online runner"),
        ("host-listing-unsupported", True, "no online runner"),
        ("host-listing-failed", True, "GET /v1/hosts failed.*HTTP 500"),
        ("resume", True, None),
    ],
)
async def test_remote_repl_runner_startup_uses_sdk_host_policy(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    readiness: str | bool,
    expected_error: str | None,
) -> None:
    """Use real SDK decoding and creation; only fresh sessions may launch a runner."""
    monkeypatch.setattr(
        identity,
        "load_host_identity_if_present",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "host_unrelated")
    created: list[dict[str, object]] = []
    requests: list[str] = []
    snapshot = {
        "id": "conv_remote",
        "agent_id": "ag_selected",
        "agent_name": "uploaded-only" if scenario == "resume" else "reviewer",
        "runner_id": (
            None
            if scenario == "online-runner"
            else "runner_existing"
            if scenario == "resume"
            else "runner_launched"
        ),
        "harness": "claude-sdk",
        "status": "idle",
        "created_at": 0,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(f"{request.method} {path}")
        if path == "/v1/runners":
            runners = (
                [
                    {
                        "runner_id": "runner_existing",
                        "harnesses": [
                            "codex" if scenario == "mismatched-runner" else "claude-sdk"
                        ],
                        "online": True,
                    }
                ]
                if scenario in {"online-runner", "mismatched-runner"}
                else []
            )
            return httpx.Response(200, json={"data": runners})
        if path == "/v1/hosts":
            assert scenario not in {"online-runner", "resume"}
            if scenario.startswith("host-listing-"):
                status_code = 404 if scenario == "host-listing-unsupported" else 500
                return httpx.Response(status_code, json={"detail": "lookup failed"})
            hosts = (
                []
                if scenario == "no-host"
                else [
                    {
                        "host_id": "host_other" if scenario == "other-host" else "host_local",
                        "name": "devbox.local",
                        "status": "online",
                        "configured_harnesses": {"claude-sdk": readiness},
                    }
                ]
            )
            return httpx.Response(200, json={"hosts": hosts})
        if path == "/v1/sessions" and request.method == "POST":
            if created:
                assert request.headers["X-Databricks-Omnigent-Slice-Key"] == "host_unrelated"
            created.append(json.loads(request.content))
            if created[-1].get("host_id"):
                assert request.headers["X-Databricks-Omnigent-Slice-Key"] == "host_local"
                assert request.extensions["timeout"]["read"] > 3600
            return httpx.Response(201, json=snapshot)
        if path == f"/v1/sessions/{snapshot['id']}" and request.method == "GET":
            if len(created) > 1:
                assert request.headers["X-Databricks-Omnigent-Slice-Key"] == "host_next"
                return httpx.Response(200, json=snapshot)
            if scenario != "resume":
                assert created[-1]["host_id"] == "host_local"
                assert request.headers["X-Databricks-Omnigent-Slice-Key"] == "host_local"
            return httpx.Response(200, json=snapshot)
        if path == "/v1/sessions/conv_remote" and request.method == "PATCH":
            if json.loads(request.content) == {"runner_id": ""}:
                return httpx.Response(200, json=snapshot)
            assert scenario == "online-runner"
            assert json.loads(request.content) == {"runner_id": "runner_existing"}
            snapshot["runner_id"] = "runner_existing"
            return httpx.Response(200, json=snapshot)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def _stream() -> None:
        return

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        event_hooks={"request": [_route_remote_session_request]},
    ) as http:
        adapter = _SessionsChatReplAdapter(
            SimpleNamespace(sessions=SessionsNamespace(http, "http://srv")),  # type: ignore[arg-type]
            "reviewer",
            session_id="conv_remote" if scenario == "resume" else None,
            registered_agent=RegisteredAgent(id="ag_selected", harness="claude"),
        )
        monkeypatch.setattr(adapter, "_stream_pump", _stream)
        try:
            if expected_error is not None:
                with pytest.raises(RuntimeError, match=expected_error):
                    await adapter._ensure_session()
                assert created == []
                assert adapter._session_id is None
                return
            assert await adapter._ensure_session() == "conv_remote"
            assert adapter._runner_id == snapshot["runner_id"]
            if scenario == "resume":
                assert requests == ["GET /v1/sessions/conv_remote"]
                assert adapter._agent_name == "uploaded-only"
            else:
                expected_body: dict[str, object] = {
                    "agent_id": "ag_selected",
                    "workspace": os.getcwd(),
                }
                if scenario in {"local-host", "mismatched-runner"}:
                    expected_body["host_id"] = "host_local"
                assert created == [expected_body]
                if "host_id" in expected_body:
                    await adapter._client.sessions.get("conv_remote")
                assert "X-Databricks-Omnigent-Slice-Key" not in http.headers
                if scenario == "local-host" and readiness is True:
                    await adapter.start_new_conversation()
                    snapshot.update(id="conv_next", host_id="host_next")
                    assert await adapter._ensure_session() == "conv_next"
                    await adapter._client.sessions.get("conv_next")
                    assert "host_id" not in created[-1]
                    assert "X-Databricks-Omnigent-Slice-Key" not in http.headers
        finally:
            await adapter.aclose()
