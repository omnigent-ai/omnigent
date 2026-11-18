"""Integration tests for session MCP server management routes."""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.bundles import bundle_location
from omnigent.server.routes import session_mcp_servers as mcp_routes
from omnigent.stores.artifact_store.local import LocalArtifactStore
from tests.server.helpers import build_agent_bundle, create_test_session

pytestmark = pytest.mark.asyncio


async def test_create_mcp_server_updates_agent_bundle(client: httpx.AsyncClient) -> None:
    """POST creates an MCP YAML file and the session agent reports it."""
    session = await create_test_session(client, name="mcp-agent")
    session_id = session["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "github",
            "transport": "http",
            "url": "https://example.com/sse",
            "description": "GitHub tools",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "name": "github",
        "transport": "http",
        "description": "GitHub tools",
        "url": "https://example.com/sse",
        "headers": {},
        "command": None,
        "args": [],
    }

    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_resp.status_code == 200, agent_resp.text
    assert agent_resp.json()["mcp_servers"] == [resp.json()]
    assert _mcp_file_from_bundle(
        await _agent_bundle(client, session_id),
        "github.yaml",
    ) == {
        "name": "github",
        "transport": "http",
        "description": "GitHub tools",
        "url": "https://example.com/sse",
    }


async def test_mcp_server_edit_conflict_preserves_concurrent_update(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer committing after the MCP read wins without losing its bundle."""
    session = await create_test_session(client, name="mcp-conflict-agent")
    session_id = session["id"]
    agent_store = app.state.agent_store
    original = agent_store.get(session["agent_id"])
    assert original is not None
    winner_bundle = build_agent_bundle(original.name, description="Concurrent winner")
    winner_location = bundle_location(original.id, winner_bundle)
    put = LocalArtifactStore.put

    def put_with_concurrent_update(
        artifacts: LocalArtifactStore, location: str, content: bytes
    ) -> None:
        put(artifacts, location, content)
        put(artifacts, winner_location, winner_bundle)
        winner = agent_store.update_configuration(
            original, winner_location, description="Concurrent winner"
        )
        assert winner is not None

    monkeypatch.setattr(LocalArtifactStore, "put", put_with_concurrent_update)
    response = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "loser", "transport": "http", "url": "https://example.com/sse"},
    )

    assert response.status_code == 409, response.text
    winner = agent_store.get(original.id)
    assert winner is not None
    assert winner.bundle_location == winner_location
    assert winner.description == "Concurrent winner"
    assert winner.version == original.version + 1
    assert await _agent_bundle(client, session_id) == winner_bundle
    agent_response = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_response.status_code == 200, agent_response.text
    assert agent_response.json()["mcp_servers"] == []


async def test_mcp_server_mutations_reset_bound_runner_agent_cache(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP server mutations invalidate stale runner-side agent caches."""
    calls: list[tuple[str, str, object]] = []

    async def _fake_reset(
        session_id: str,
        agent_id: str,
        runner_router: object,
    ) -> None:
        calls.append((session_id, agent_id, runner_router))

    monkeypatch.setattr(mcp_routes, "_reset_runner_session_agent_cache", _fake_reset)
    session = await create_test_session(client, name="mcp-reset-agent")
    session_id = session["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "echo",
            "transport": "stdio",
            "command": "python",
            "args": ["echo_server.py"],
        },
    )

    assert resp.status_code == 200, resp.text

    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/echo",
        json={
            "name": "echo-renamed",
            "transport": "stdio",
            "command": "python",
            "args": ["echo_server.py"],
        },
    )
    assert update.status_code == 200, update.text

    delete = await client.delete(f"/v1/sessions/{session_id}/agent/mcp-servers/echo-renamed")
    assert delete.status_code == 204, delete.text

    assert [(sid, aid) for sid, aid, _ in calls] == [
        (session_id, session["agent_id"]),
        (session_id, session["agent_id"]),
        (session_id, session["agent_id"]),
    ]
    assert all(runner_router is not None for _, _, runner_router in calls)


async def test_update_mcp_server_can_rename_and_change_transport(
    client: httpx.AsyncClient,
) -> None:
    """PUT replaces the existing declaration and validates transport fields."""
    session = await create_test_session(client, name="mcp-update-agent")
    session_id = session["id"]
    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "search", "transport": "http", "url": "https://example.com/sse"},
    )
    assert create.status_code == 200, create.text

    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/search",
        json={
            "name": "local-search",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-search"],
        },
    )

    assert update.status_code == 200, update.text
    assert update.json() == {
        "name": "local-search",
        "transport": "stdio",
        "description": None,
        "url": None,
        "headers": {},
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-search"],
    }
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert [server["name"] for server in agent_resp.json()["mcp_servers"]] == ["local-search"]


async def test_delete_mcp_server_removes_it_from_agent(client: httpx.AsyncClient) -> None:
    """DELETE removes the MCP declaration from the stored bundle."""
    session = await create_test_session(client, name="mcp-delete-agent")
    session_id = session["id"]
    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "github", "transport": "http", "url": "https://example.com/sse"},
    )
    assert create.status_code == 200, create.text

    delete = await client.delete(f"/v1/sessions/{session_id}/agent/mcp-servers/github")

    assert delete.status_code == 204, delete.text
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_resp.status_code == 200, agent_resp.text
    assert agent_resp.json()["mcp_servers"] == []


async def test_create_mcp_server_rejects_duplicate_name(client: httpx.AsyncClient) -> None:
    """Creating the same MCP server twice returns 409."""
    session = await create_test_session(client, name="mcp-dup-agent")
    session_id = session["id"]
    payload = {"name": "github", "transport": "http", "url": "https://example.com/sse"}
    first = await client.post(f"/v1/sessions/{session_id}/agent/mcp-servers", json=payload)
    assert first.status_code == 200, first.text

    second = await client.post(f"/v1/sessions/{session_id}/agent/mcp-servers", json=payload)

    assert second.status_code == 409, second.text


async def test_create_mcp_server_supports_single_yaml_bundle(client: httpx.AsyncClient) -> None:
    """Single-file omnigent YAML bundles are updated inline."""
    create_session = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": (
                "agent.tar.gz",
                _single_yaml_bundle(
                    """\
name: single_yaml_agent
prompt: Say hello.
executor:
  model: gpt-4o-mini
  harness: openai-agents
"""
                ),
                "application/gzip",
            )
        },
    )
    assert create_session.status_code == 201, create_session.text
    session_id = create_session.json()["session_id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "browser-search", "transport": "http", "url": "https://example.com/sse"},
    )

    assert resp.status_code == 200, resp.text
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert [server["name"] for server in agent_resp.json()["mcp_servers"]] == ["browser-search"]


async def test_update_mcp_server_preserves_headers_on_redacted_roundtrip(
    client: httpx.AsyncClient,
) -> None:
    """Editing a server while sending [REDACTED] header values must not overwrite secrets."""
    session = await create_test_session(client, name="mcp-headers-agent")
    session_id = session["id"]

    # Create server with a real Authorization header.
    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse",
            "headers": {"Authorization": "Bearer real-token"},
        },
    )
    assert create.status_code == 200, create.text

    # Simulate the UI round-trip: the GET returns [REDACTED] values; the client
    # sends them back verbatim when editing only the URL.
    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/secure",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse-v2",
            "headers": {"Authorization": "[REDACTED]"},
        },
    )
    assert update.status_code == 200, update.text

    # The bundle must still contain the real token, not the sentinel.
    bundle = await _agent_bundle(client, session_id)
    mcp_file = _mcp_file_from_bundle(bundle, "secure.yaml")
    assert mcp_file["url"] == "https://example.com/sse-v2"
    assert mcp_file.get("headers") == {"Authorization": "Bearer real-token"}


async def test_update_mcp_server_clears_headers_when_empty_dict_sent(
    client: httpx.AsyncClient,
) -> None:
    """Sending headers={} on update must remove all headers from the bundle."""
    session = await create_test_session(client, name="mcp-clear-headers-agent")
    session_id = session["id"]

    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse",
            "headers": {"Authorization": "Bearer real-token"},
        },
    )
    assert create.status_code == 200, create.text

    # User removes all header rows — client sends {}.
    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/secure",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse",
            "headers": {},
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["headers"] == {}

    bundle = await _agent_bundle(client, session_id)
    mcp_file = _mcp_file_from_bundle(bundle, "secure.yaml")
    assert "headers" not in mcp_file


async def _agent_bundle(client: httpx.AsyncClient, session_id: str) -> bytes:
    """Download the session agent bundle."""
    resp = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert resp.status_code == 200, resp.text
    return resp.content


def _mcp_file_from_bundle(bundle: bytes, filename: str) -> dict[str, Any]:
    """Read one MCP YAML file from a bundle by basename."""
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith(f"/tools/mcp/{filename}"))
        extracted = tf.extractfile(member)
        assert extracted is not None
        data = yaml.safe_load(extracted.read())
    assert isinstance(data, dict)
    return data


def _single_yaml_bundle(yaml_text: str) -> bytes:
    """Build a tar.gz bundle containing one omnigent YAML file."""
    buf = io.BytesIO()
    data = yaml_text.encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="agent.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_mcp_mutation_rejects_legacy_source_tool_bundle(
    client: httpx.AsyncClient,
    method: str,
) -> None:
    from omnigent.runtime import get_agent_store, get_artifact_store
    from tests.server.test_bundles import _MIN_CONFIG, _make_bundle_bytes

    session = await create_test_session(client, name="legacy-source-agent")
    agent = get_agent_store().get(session["agent_id"])
    assert agent is not None
    artifacts = get_artifact_store()
    assert artifacts is not None
    legacy_bundle = _make_bundle_bytes(
        {
            "config.yaml": _MIN_CONFIG.format(name="legacy-source-agent"),
            "tools/python/echo.py": "def echo(text: str) -> str:\n    return text\n",
            "tools/mcp/docs.yaml": "name: docs\ntransport: http\nurl: https://example.com/mcp\n",
        }
    )
    # Seed the old stored representation directly; current HTTP uploads reject it.
    artifacts.put(agent.bundle_location, legacy_bundle)
    path = f"/v1/sessions/{session['id']}/agent/mcp-servers"
    if method != "POST":
        path += "/docs"
    payload = {"name": "updated", "transport": "http", "url": "https://example.com/mcp"}
    response = await client.request(method, path, json=payload if method != "DELETE" else None)
    assert response.status_code == 400, response.text
    assert "may not ship a server-side Python/TS tool file" in response.text
    persisted = get_agent_store().get(agent.id)
    assert persisted is not None
    assert persisted.bundle_location == agent.bundle_location
    assert artifacts.get(agent.bundle_location) == legacy_bundle

    # Replace the bundle to remove the obsolete source tool before editing MCP settings.
    migrated = _make_bundle_bytes(
        {
            "config.yaml": _MIN_CONFIG.format(name="legacy-source-agent"),
            "tools/mcp/docs.yaml": "name: docs\ntransport: http\nurl: https://example.com/mcp\n",
        }
    )
    replaced = await client.put(
        f"/v1/sessions/{session['id']}/agent",
        files={"bundle": ("agent.tar.gz", migrated, "application/gzip")},
    )
    assert replaced.status_code == 200, replaced.text
    response = await client.request(method, path, json=payload if method != "DELETE" else None)
    assert response.status_code == (204 if method == "DELETE" else 200), response.text


async def test_live_mcp_status_maps_each_runner_answer(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel's status read never fails; each runner answer becomes a ``reason``."""
    from omnigent.errors import ErrorCode, OmnigentError

    answers: list[Any] = []
    requests: list[tuple[str, str, Any]] = []

    async def runner_request(
        _router: object, _session_id: str, method: str, path: str, **kwargs: Any
    ):
        requests.append((method, path, kwargs.get("json")))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(mcp_routes, "_live_mcp_runner_request", runner_request)
    session = await create_test_session(client, name="live-mcp-agent")
    url = f"/v1/sessions/{session['id']}/live-mcp-servers"
    live = {
        "running": True,
        "servers": [
            {"name": "plugin:tools:search", "status": "needs-auth"},
            {"name": "docs", "status": "failed", "error": "connection refused", "scope": "user"},
            {"status": "connected"},
        ],
    }
    answers[:] = [
        httpx.Response(200, json=live),
        None,
        httpx.Response(200, json={"running": False, "servers": []}),
        httpx.Response(404, json={"error": "unsupported"}),
        httpx.Response(504, json={"error": "harness_timeout"}),
        httpx.Response(502, json={"error": "harness_unreachable", "detail": "harness exited"}),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
        ConnectionError("host tunnel closed"),
        OmnigentError("The session's host is offline.", code=ErrorCode.RUNNER_UNAVAILABLE),
    ]
    results = []
    for _ in range(len(answers)):
        response = await client.get(url)
        assert response.status_code == 200, response.text
        results.append(response.json())

    assert results[0] == {
        "live": True,
        "reason": None,
        "detail": None,
        "servers": [
            {"name": "plugin:tools:search", "status": "needs-auth", "error": None, "scope": None},
            {"name": "docs", "status": "failed", "error": "connection refused", "scope": "user"},
        ],
        "reconnect_error": None,
    }
    assert [(result["live"], result["reason"], result["detail"]) for result in results[1:]] == [
        (False, "not_running", None),
        (False, "not_running", None),
        (False, "unsupported", None),
        (False, "unavailable", mcp_routes._HARNESS_BUSY_MESSAGE),
        (False, "unavailable", "harness exited"),
        (False, "unavailable", mcp_routes._HARNESS_BUSY_MESSAGE),
        (False, "unavailable", "Could not reach the session's host."),
        (False, "unavailable", "Could not reach the session's host."),
        (False, "unavailable", "The session's host is offline."),
    ]
    assert {request[:2] for request in requests} == {("GET", "live-mcp-servers")}

    # A runner on another replica keeps its routing error so the client re-addresses.
    answers.append(OmnigentError("Session is on another replica.", code=ErrorCode.WRONG_REPLICA))
    misrouted = await client.get(url)
    assert misrouted.status_code == 400, misrouted.text
    assert misrouted.json()["error"]["code"] == ErrorCode.WRONG_REPLICA


async def test_live_mcp_reconnect_maps_each_runner_answer(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused reconnect answers 200 with its reason; a missing harness or server does not."""
    from omnigent.errors import ErrorCode, OmnigentError

    answers: list[Any] = []
    requests: list[tuple[str, str, Any]] = []

    async def runner_request(
        _router: object, _session_id: str, method: str, path: str, **kwargs: Any
    ):
        requests.append((method, path, kwargs.get("json")))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(mcp_routes, "_live_mcp_runner_request", runner_request)
    session = await create_test_session(client, name="live-mcp-reconnect-agent")
    url = f"/v1/sessions/{session['id']}/live-mcp-servers/reconnect"

    answers.append(
        httpx.Response(
            200,
            json={
                "running": True,
                "servers": [{"name": "docs", "status": "failed", "error": "token expired"}],
                "reconnect_error": "token expired",
            },
        )
    )
    refused = await client.post(url, json={"name": "docs"})
    assert refused.status_code == 200, refused.text
    assert refused.json()["reconnect_error"] == "token expired"
    assert refused.json()["servers"][0]["status"] == "failed"
    assert requests == [("POST", "live-mcp-servers/reconnect", {"name": "docs"})]

    # The harness is running but could not report after the reconnect.
    answers.append(
        httpx.Response(
            200,
            json={
                "running": True,
                "servers": [],
                "status_unavailable": True,
                "reconnect_error": "token expired",
            },
        )
    )
    unreported = (await client.post(url, json={"name": "docs"})).json()
    assert (unreported["live"], unreported["reason"], unreported["reconnect_error"]) == (
        False,
        "unavailable",
        "token expired",
    )

    cases: list[tuple[Any, int, str]] = [
        (None, 409, "isn't running"),
        (httpx.Response(409, json={"error": "harness_not_running"}), 409, "isn't running"),
        (
            httpx.Response(404, json={"error": "mcp_server_unknown"}),
            404,
            "no MCP server named 'docs'",
        ),
        (httpx.Response(404, json={"error": "unsupported"}), 404, "can't reconnect"),
        (httpx.Response(504, json={"error": "harness_timeout"}), 503, "did not answer in time"),
        (
            httpx.Response(502, json={"error": "harness_unreachable", "detail": "gone"}),
            503,
            "gone",
        ),
        (httpx.ReadTimeout("slow"), 503, "did not answer in time"),
        (httpx.ConnectError("refused"), 503, "Could not reach"),
        (ConnectionError("host tunnel closed"), 503, "Could not reach"),
        (OmnigentError("on another replica", code=ErrorCode.WRONG_REPLICA), 400, "replica"),
    ]
    for answer, status_code, message in cases:
        answers.append(answer)
        response = await client.post(url, json={"name": "docs"})
        assert response.status_code == status_code, (answer, response.text)
        assert message in response.text, (answer, response.text)

    for body in ({}, {"name": ""}, {"name": "  "}, {"name": "docs", "extra": 1}):
        invalid = await client.post(url, json=body)
        assert invalid.status_code in (400, 422), invalid.text
    assert answers == []


class _LiveMcpRouter:
    """Routes one conversation to a fake runner, or raises the offline error."""

    def __init__(self, routed: Any) -> None:
        self.routed = routed

    def client_for_existing_conversation(self, conversation_id: str) -> Any:
        if isinstance(self.routed, Exception):
            raise self.routed
        return self.routed


async def test_live_mcp_runner_request_forwards_to_the_sessions_runner() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"live": True, "servers": []})

    async with httpx.AsyncClient(
        base_url="http://runner.example.com", transport=httpx.MockTransport(handler)
    ) as client:
        router: Any = _LiveMcpRouter(SimpleNamespace(client=client))
        response = await mcp_routes._live_mcp_runner_request(
            router,
            "conv/a b",
            "POST",
            "live-mcp-servers/reconnect",
            json={"name": "docs"},
            timeout=5,
        )
    assert response is not None and response.status_code == 200
    assert seen[0].method == "POST"
    assert seen[0].url.raw_path == b"/v1/sessions/conv%2Fa%20b/live-mcp-servers/reconnect"
    assert seen[0].read() == b'{"name":"docs"}'

    # No router, or no runner holding the session, means nothing is running.
    no_runner: Any = _LiveMcpRouter(None)
    for absent in (None, no_runner):
        assert (
            await mcp_routes._live_mcp_runner_request(
                absent, "conv_a", "GET", "live-mcp-servers", timeout=5
            )
            is None
        )
    offline: Any = _LiveMcpRouter(
        OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE)
    )
    with pytest.raises(OmnigentError, match="runner offline"):
        await mcp_routes._live_mcp_runner_request(
            offline, "conv_a", "GET", "live-mcp-servers", timeout=5
        )
