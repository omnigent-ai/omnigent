"""A managed create from the SDK names its new host, and the session routes by it.

Drives a real :class:`OmnigentClient` through the
:class:`~omnigent.session_host.SessionRoutingTransport` against a mock network on
a host-sharded mount, so each test sees the routing key every request carries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from omnigent_client import OmnigentClient

from omnigent.cli_auth import DATABRICKS_ORG_ID_HEADER, OMNIGENT_SLICE_KEY_HEADER
from omnigent.session_host import SessionRoutingTransport, set_session_host
from tests.frontends.sdk.test_sessions_namespace import _session_response_body

# The workspace API mount is what makes a deployment host-sharded.
BASE_URL = "https://workspace.example.com/api/2.0/omnigent"
SESSION_ID = "9e4b1c7a2d3f4e5a8b6c0d1e2f3a4b5c"
HEX_ID = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(autouse=True)
def _forget_session_host() -> Iterator[None]:
    set_session_host(SESSION_ID, None)
    yield
    set_session_host(SESSION_ID, None)


def _client(
    seen: list[httpx.Request], *, accepts_host_id: bool, headers: dict[str, str] | None = None
) -> OmnigentClient:
    """A client whose server reports *accepts_host_id* and records every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/v1/info"):
            return httpx.Response(200, json={"request_managed_sandbox_host_id": accepts_host_id})
        if request.method == "POST" and "multipart/form-data" in request.headers["content-type"]:
            return httpx.Response(201, json={"session_id": SESSION_ID})
        return httpx.Response(200, json=_session_response_body(session_id=SESSION_ID))

    transport = SessionRoutingTransport(transport=httpx.MockTransport(handler))
    return OmnigentClient(BASE_URL, headers=headers, transport=transport)


def _creates(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.method == "POST" and r.url.path.endswith("/v1/sessions")]


def _multipart_metadata(request: httpx.Request) -> dict[str, Any]:
    body = request.content.decode()
    part = body.split('name="metadata"', 1)[1].split("\r\n\r\n", 1)[1]
    return json.loads(part.split("\r\n--", 1)[0])


@pytest.mark.asyncio
async def test_managed_json_create_names_its_host_and_routes_by_it() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen, accepts_host_id=True) as client:
        session = await client.sessions.create_from_agent_id(
            "ag_abc", host_type="managed", sandbox_provider="modal"
        )
        await client.sessions.get(session.id)

    (create,) = _creates(seen)
    host_id = json.loads(create.content)["host_id"]
    assert HEX_ID.fullmatch(host_id), host_id
    assert create.headers[OMNIGENT_SLICE_KEY_HEADER] == host_id
    # A server-minted host id would not extend the read timeout for a host launch.
    assert create.extensions["timeout"]["read"] == 30.0
    assert seen[-1].headers[OMNIGENT_SLICE_KEY_HEADER] == host_id


@pytest.mark.asyncio
async def test_managed_bundle_create_names_its_host_and_routes_by_it() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen, accepts_host_id=True) as client:
        await client.sessions.create(b"bundle", host_type="managed")

    (create,) = _creates(seen)
    host_id = _multipart_metadata(create)["host_id"]
    assert HEX_ID.fullmatch(host_id), host_id
    assert create.headers[OMNIGENT_SLICE_KEY_HEADER] == host_id
    # The snapshot read right after the create is keyed by the new host too.
    assert seen[-1].method == "GET"
    assert seen[-1].headers[OMNIGENT_SLICE_KEY_HEADER] == host_id


@pytest.mark.asyncio
async def test_managed_create_names_no_host_for_a_server_that_rejects_one() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen, accepts_host_id=False) as client:
        await client.sessions.create_from_agent_id("ag_abc", host_type="managed")
        await client.sessions.create(b"bundle", host_type="managed")

    json_create, bundle_create = _creates(seen)
    assert "host_id" not in json.loads(json_create.content)
    assert "host_id" not in _multipart_metadata(bundle_create)


@pytest.mark.asyncio
async def test_external_create_does_not_probe_or_name_a_host() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen, accepts_host_id=True) as client:
        await client.sessions.create_from_agent_id("ag_abc")

    assert not any(r.url.path.endswith("/v1/info") for r in seen)
    assert "host_id" not in json.loads(_creates(seen)[0].content)


@pytest.mark.asyncio
async def test_routing_keeps_the_callers_workspace_selector_and_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keying a request by its host changes only the routing key."""
    from omnigent import cli_auth

    monkeypatch.setattr(cli_auth, "load_databricks_org_id", lambda _url: "100")
    seen: list[httpx.Request] = []
    async with _client(
        seen, accepts_host_id=True, headers={DATABRICKS_ORG_ID_HEADER: "200"}
    ) as client:
        await client.sessions.create(b"bundle", host_type="managed")

    info, create, snapshot = seen
    assert info.headers[DATABRICKS_ORG_ID_HEADER] == "200"
    for request in (create, snapshot):
        assert request.headers[DATABRICKS_ORG_ID_HEADER] == "200"
        assert request.headers["User-Agent"] == info.headers["User-Agent"]
        assert HEX_ID.fullmatch(request.headers[OMNIGENT_SLICE_KEY_HEADER])


def test_a_default_client_routes_session_requests() -> None:
    client = OmnigentClient(BASE_URL)
    assert isinstance(client._http._transport, SessionRoutingTransport)
