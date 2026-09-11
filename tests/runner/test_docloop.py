"""The notebook relay reuses live processes and bounds uncertain writes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.docloop_gateway import notebook_gateway_router
from omnigent.runner import create_runner_app
from omnigent.runner.docloop import (
    AssignedRunnerNotebookTransport,
    LiveHarnessNotebookTransport,
    _forward,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from tests.runner.helpers import NullServerClient

SESSION = "notebook-session"
URL = f"/v1/sessions/{SESSION}/docloop/document"
EDIT = {
    "revision": "a" * 64,
    "binding_id": "b" * 64,
    "change_id": str(uuid4()),
    "changes": [{"op": "create", "id": "note", "kind": "markdown", "source": "draft"}],
}


def snapshot():
    return {
        "schema_version": 1,
        "session_id": SESSION,
        "revision": "a" * 64,
        "binding_id": "b" * 64,
        "format": "org",
        "document_name": "work.org",
        "nodes": [],
        "capabilities": {"edit_source": True, "create_node": True, "direct_execution": False},
    }


async def allow(_request, _session_id):
    pass


@pytest.mark.asyncio
async def test_runner_auth_and_no_live_harness_do_not_spawn(tmp_path, monkeypatch):
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    spawn = AsyncMock(side_effect=AssertionError("Notebook route must not spawn"))
    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    await manager.start()
    try:
        app = create_runner_app(
            process_manager=manager,
            server_client=NullServerClient(),
            auth_token="synthetic-runner-token",
            docloop_notebook_enabled=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            assert (await client.get(URL)).status_code == 401
            response = await client.get(
                URL, headers={"Authorization": "Bearer synthetic-runner-token"}
            )
            assert response.status_code == 503
            assert response.json()["outcome"] == "not_dispatched"
        spawn.assert_not_called()
        assert not manager._entries
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_default_off_has_no_notebook_route():
    app = create_runner_app(server_client=NullServerClient(), docloop_notebook_enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        assert (await client.get(URL)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_both_hops_reuse_clients_and_drop_browser_headers(method):
    received = []
    harness = FastAPI()

    @harness.api_route(URL, methods=["GET", "PATCH"])
    async def document(request: Request):
        received.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-harness-token"
        assert "cookie" not in request.headers
        assert "x-user-id" not in request.headers
        if method == "PATCH":
            assert await request.json() == EDIT
            assert request.headers["x-docloop-edit"] == "1"
        value = snapshot() if method == "GET" else {"document": snapshot(), "replayed": False}
        return JSONResponse(value, headers={"Set-Cookie": "must-not-escape=1"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness),
        base_url="http://harness",
        headers={"Authorization": "Bearer synthetic-harness-token"},
    ) as harness_client:
        manager = SimpleNamespace(get_client=AsyncMock(return_value=harness_client))
        runner = create_runner_app(
            process_manager=manager,
            server_client=NullServerClient(),
            auth_token="synthetic-runner-token",
            docloop_notebook_enabled=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner),
            base_url="http://runner",
            headers={"Authorization": "Bearer synthetic-runner-token"},
        ) as runner_client:
            lookups = []

            def existing_resource(session_id):
                lookups.append(session_id)
                return SimpleNamespace(client=runner_client, runner_id="assigned")

            router = SimpleNamespace(client_for_session_resources=existing_resource)
            central = FastAPI()
            central.include_router(
                notebook_gateway_router(allow, AssignedRunnerNotebookTransport(router))
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=central), base_url="http://central"
            ) as client:
                response = await client.request(
                    method,
                    URL,
                    json=EDIT if method == "PATCH" else None,
                    headers={
                        "Authorization": "Bearer synthetic-browser-token",
                        "Cookie": "synthetic-session=1",
                        "X-User-ID": "browser",
                        "X-Docloop-Edit": "1",
                    },
                )
            assert response.status_code == 200, response.text
            assert "set-cookie" not in response.headers
            assert response.headers["cache-control"] == "no-store"
            assert lookups == [SESSION]
            manager.get_client.assert_awaited_once_with(SESSION, "any")
            assert len(received) == 1


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks", "expected_reads"),
    [
        ({"Content-Length": "99"}, [b"x"], 0),
        ({"Content-Encoding": "gzip"}, [b"x"], 0),
        ({}, [b"12345", b"6789", b"unread"], 2),
        ({"Content-Length": "5"}, [b"1234"], 1),
    ],
)
async def test_response_limit_closes_stream(headers, chunks, expected_reads):
    stream = Chunks(chunks)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=stream)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
        with pytest.raises(ValueError):
            await _forward(client, SESSION, "PATCH", b"{}", max_response_bytes=8)
    assert stream.closed
    assert stream.reads == expected_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 400, 500])
async def test_unconfirmed_write_is_not_retried_or_reported_as_unapplied(status):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "http://unused.invalid", "Content-Type": "application/json"},
            stream=Chunks([json.dumps({"error": "untrusted runner detail"}).encode()]),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), base_url="http://harness", follow_redirects=True
    ) as harness_client:
        manager = SimpleNamespace(get_client=AsyncMock(return_value=harness_client))
        app = FastAPI()
        app.include_router(notebook_gateway_router(allow, LiveHarnessNotebookTransport(manager)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay"
        ) as client:
            response = await client.patch(URL, json=EDIT, headers={"X-Docloop-Edit": "1"})
        assert response.status_code == 502
        assert response.json()["outcome"] == "unknown"
        assert "untrusted" not in response.text
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_storage_outcomes_and_explicit_recovery_survive_both_relays():
    seen = []
    failure = {
        "code": "save_failed",
        "phase": "history_ref",
        "installed": True,
        "durability": "confirmed",
        "history": "pending",
        "history_durability": "unknown",
        "attempted_revision": "c" * 64,
        "current_revision": "c" * 64,
        "revision": "c" * 64,
        "retry_action": False,
        "error": "private runner exception text",
        "connection_headers": {"secret": "hidden"},
    }

    harness_app = FastAPI()

    @harness_app.api_route(
        "/v1/sessions/{sid}/docloop/{operation}", methods=["GET", "PATCH", "POST"]
    )
    async def harness(request: Request, sid: str, operation: str):
        seen.append(
            {"path": request.url.path, "headers": request.headers, "body": await request.body()}
        )
        if request.method == "PATCH":
            return JSONResponse(failure, status_code=503)
        result = snapshot()
        result["revision"] = "c" * 64
        result["history_status"] = {"status": "recorded", "private": "hidden"}
        return JSONResponse(result)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(harness_app), base_url="http://harness"
    )
    transport = LiveHarnessNotebookTransport(
        SimpleNamespace(get_client=AsyncMock(return_value=client))
    )
    app = FastAPI()
    app.include_router(notebook_gateway_router(allow, transport))
    runner_client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runner")
    assigned = AssignedRunnerNotebookTransport(
        SimpleNamespace(
            client_for_session_resources=lambda sid: SimpleNamespace(client=runner_client)
        )
    )
    central = FastAPI()
    central.include_router(notebook_gateway_router(allow, assigned))
    async with (
        client,
        runner_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(central), base_url="http://central"
        ) as browser,
    ):
        headers = {"X-Docloop-Edit": "1"}
        response = await browser.patch(URL, json=EDIT, headers=headers)
        assert response.status_code == 503 and response.json()["retry_action"] is False
        assert response.json()["current_revision"] == "c" * 64
        assert "private" not in response.text and "secret" not in response.text
        recovery = {"binding_id": "b" * 64, "revision": "c" * 64}
        path = URL.replace("document", "recover-history")
        assert (await browser.post(path, json=recovery)).status_code == 403
        result = await browser.post(path, json=recovery, headers=headers)
        assert result.status_code == 200 and result.json()["history_status"] == {
            "status": "recorded"
        }
        assert len(seen) == 2 and seen[1]["path"] == path
        assert seen[1]["headers"]["x-docloop-edit"] == "1"
        assert json.loads(seen[1]["body"]) == recovery
        failure["phase"] = "untrusted arbitrary private details"
        rejected = await browser.patch(URL, json=EDIT, headers=headers)
        assert (
            rejected.status_code == 502 and rejected.json()["code"] == "notebook_edit_unconfirmed"
        )
