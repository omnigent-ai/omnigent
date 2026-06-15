"""
Integration tests for the JSON Content-Type CSRF guard on session POSTs.

Eight session POST handlers parse their body with Starlette's
``await request.json()``, which decodes any body as JSON regardless of
the declared ``Content-Type``. That left them reachable by a cross-site
*simple* request — one carrying ``Content-Type: text/plain`` (so the
browser skips the CORS preflight) whose plain-text payload is actually
valid JSON. The ``require_json_content_type`` /
``require_json_or_multipart_content_type`` dependencies close that gap by
requiring an explicit JSON (or, for the bundled-create route, multipart)
Content-Type before the handler runs.

These tests drive the real routes through the shared ``client`` fixture
(real stores + mock LLM, permissions disabled) and assert:

- a ``text/plain`` body of valid JSON now returns 415 (was accepted),
- a request with no ``Content-Type`` returns 415,
- a correct ``application/json`` request still reaches the handler
  (status is whatever the handler returns — crucially NOT 415),
- ``POST /v1/sessions`` still accepts both JSON and ``multipart/form-data``
  while rejecting ``text/plain``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


# A body that is valid JSON but is sent with the wrong Content-Type. The
# point of the guard is that the *content* parses fine — only the declared
# media type makes it a cross-site CSRF vector — so the rejection must be
# driven by the header, not by the payload being unparseable.
_VALID_JSON_BODY = {"hello": "world"}


# All seven handlers guarded by ``require_json_content_type``. A fake
# session id is sufficient: the content-type dependency runs before any
# session lookup, so the 415 fires regardless of whether the session
# exists. ``term_x`` / ``env_x`` are likewise never resolved.
_JSON_ONLY_ENDPOINTS = [
    pytest.param(
        "/v1/sessions/conv_missing/hooks/permission-request",
        id="claude_permission_request_hook",
    ),
    pytest.param(
        "/v1/sessions/conv_missing/hooks/codex-elicitation-request",
        id="codex_elicitation_request_hook",
    ),
    pytest.param(
        "/v1/sessions/conv_missing/policies/evaluate",
        id="evaluate_policy",
    ),
    pytest.param(
        "/v1/sessions/conv_missing/resources/terminals",
        id="create_session_terminal",
    ),
    pytest.param(
        "/v1/sessions/conv_missing/resources/terminals/term_x/transfer",
        id="transfer_session_terminal",
    ),
    pytest.param(
        "/v1/sessions/conv_missing/resources/environments/env_x/shell",
        id="run_environment_shell",
    ),
    pytest.param(
        "/v1/sessions/conv_missing/mcp",
        id="mcp_proxy",
    ),
]


async def _post_text_plain(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, object],
) -> httpx.Response:
    """
    POST a valid-JSON payload with a ``text/plain`` Content-Type.

    Sends the bytes explicitly so httpx does not set ``application/json``
    for us — the whole point is that the declared type is ``text/plain``
    while the body is parseable JSON (the CSRF simple-request shape).

    :param client: The test HTTP client.
    :param url: The route path to POST to.
    :param payload: A JSON-serializable body, e.g. ``{"hello": "world"}``.
    :returns: The HTTP response.
    """
    return await client.post(
        url,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )


async def _post_without_content_type(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, object],
) -> httpx.Response:
    """
    POST a valid-JSON payload with no ``Content-Type`` header at all.

    Passing raw ``bytes`` (not ``json=`` / ``str``) means httpx attaches
    no ``Content-Type``, modelling a request that omits the header.

    :param client: The test HTTP client.
    :param url: The route path to POST to.
    :param payload: A JSON-serializable body.
    :returns: The HTTP response.
    """
    return await client.post(url, content=json.dumps(payload).encode("utf-8"))


# ── JSON-only handlers: text/plain and missing header are rejected ──


@pytest.mark.parametrize("url", _JSON_ONLY_ENDPOINTS)
async def test_json_only_endpoint_rejects_text_plain(
    client: httpx.AsyncClient,
    url: str,
) -> None:
    """
    A ``text/plain`` body of valid JSON now returns 415 on every guarded route.

    Before the guard, ``await request.json()`` decoded this body and the
    handler ran — the CSRF vector. A non-415 status here would mean the
    cross-site simple request still reaches the handler.
    """
    resp = await _post_text_plain(client, url, _VALID_JSON_BODY)
    assert resp.status_code == 415, (
        f"{url} accepted a text/plain JSON body (status {resp.status_code}); "
        "the CSRF content-type guard did not fire."
    )


@pytest.mark.parametrize("url", _JSON_ONLY_ENDPOINTS)
async def test_json_only_endpoint_rejects_missing_content_type(
    client: httpx.AsyncClient,
    url: str,
) -> None:
    """
    A request with no ``Content-Type`` returns 415 on every guarded route.

    A missing header is treated as "no acceptable type"; if it slipped
    through, a client could omit the header to dodge the guard.
    """
    resp = await _post_without_content_type(client, url, _VALID_JSON_BODY)
    assert resp.status_code == 415, (
        f"{url} accepted a body with no Content-Type (status {resp.status_code}); "
        "a missing header must be rejected."
    )


# ── JSON-only handlers: application/json still reaches the handler ──
#
# Each positive case asserts the SPECIFIC status the handler returns for
# the chosen input, which is never 415. That proves the guard let the
# request through (the gate passed) AND the handler behaves exactly as it
# did before this change.


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(
            "/v1/sessions/conv_missing/hooks/permission-request",
            id="claude_permission_request_hook",
        ),
        pytest.param(
            "/v1/sessions/conv_missing/hooks/codex-elicitation-request",
            id="codex_elicitation_request_hook",
        ),
    ],
)
async def test_hook_handlers_reached_with_application_json(
    client: httpx.AsyncClient,
    url: str,
) -> None:
    """
    ``application/json`` reaches the hook handlers (400, not 415).

    A JSON array body passes the content-type guard, then the handler's
    own "body must be a JSON object" validation rejects it with 400 —
    before any elicitation publish or long-poll. A 415 here would mean a
    valid JSON request was wrongly blocked at the gate.
    """
    # ``json=[]`` sends a valid application/json body that is a JSON array,
    # which both hook handlers reject as "not a JSON object" with 400.
    resp = await client.post(url, json=[])
    assert resp.status_code == 400, (
        f"{url} returned {resp.status_code} for an application/json array body; "
        "expected the handler's own 400 (proves the guard passed it through)."
    )


async def test_evaluate_policy_reached_with_application_json(
    client: httpx.AsyncClient,
) -> None:
    """
    ``application/json`` reaches ``evaluate_policy`` and returns its verdict.

    With a real session and a valid ``PHASE_TOOL_CALL`` envelope, the
    endpoint evaluates policies and returns 200 with an ALLOW verdict (no
    policies configured) — exactly as before the guard. Proves the guard
    is transparent to a correct JSON request.
    """
    agent = await create_test_agent(client)
    create = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert create.status_code == 201, create.text
    session_id = create.json()["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_TOOL_CALL",
                "data": {"name": "Read", "arguments": {}},
                "context": {},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    # No policies configured → ALLOW. Confirms the body traversed the full
    # handler pipeline, not just the content-type gate.
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_create_terminal_reached_with_application_json(
    client: httpx.AsyncClient,
) -> None:
    """
    ``application/json`` reaches ``create_session_terminal`` (404, not 415).

    A valid JSON body against a non-existent session passes the
    content-type guard, then the handler's session lookup returns 404. A
    415 would mean the guard wrongly blocked a correct JSON request.
    """
    resp = await client.post(
        "/v1/sessions/conv_missing/resources/terminals",
        json={"terminal": "shell", "session_key": "main"},
    )
    assert resp.status_code == 404, (
        f"expected 404 (missing session) once the guard passed the JSON body "
        f"through, got {resp.status_code}."
    )


async def test_mcp_proxy_reached_with_application_json(
    client: httpx.AsyncClient,
) -> None:
    """
    ``application/json`` reaches ``mcp_proxy`` and handles ``initialize`` (200).

    A JSON-RPC ``initialize`` request passes the guard and is answered
    locally with a 200 capability response (no runner round-trip). A 415
    here would mean the guard blocked a valid MCP request.
    """
    resp = await client.post(
        "/v1/sessions/conv_missing/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # JSON-RPC envelope echoed back → the handler ran, not the gate.
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "omnigent-mcp-proxy"


# ── create_session: JSON and multipart both work; text/plain is 415 ──


async def test_create_session_rejects_text_plain(
    client: httpx.AsyncClient,
) -> None:
    """
    ``POST /v1/sessions`` with a ``text/plain`` JSON body returns 415.

    The bundled-create route dispatches on Content-Type, so the guard must
    reject simple types up front while still allowing JSON and multipart
    (covered below). Before the guard this text/plain body would have been
    decoded by ``request.json()`` and a session created.
    """
    resp = await _post_text_plain(client, "/v1/sessions", {"agent_id": "agent_x"})
    assert resp.status_code == 415, (
        f"create_session accepted a text/plain JSON body (status {resp.status_code})."
    )


async def test_create_session_rejects_missing_content_type(
    client: httpx.AsyncClient,
) -> None:
    """
    ``POST /v1/sessions`` with no ``Content-Type`` returns 415.
    """
    resp = await _post_without_content_type(client, "/v1/sessions", {"agent_id": "agent_x"})
    assert resp.status_code == 415, (
        f"create_session accepted a body with no Content-Type (status {resp.status_code})."
    )


async def test_create_session_accepts_application_json(
    client: httpx.AsyncClient,
) -> None:
    """
    ``POST /v1/sessions`` with ``application/json`` still creates a session.

    Binding to an existing agent by ``agent_id`` over JSON returns 201 —
    the JSON-create contract is unchanged by the guard.
    """
    agent = await create_test_agent(client)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    # Real session id returned → JSON create reached the handler and succeeded.
    assert resp.json()["id"].startswith("conv_")


async def test_create_session_accepts_multipart_bundled_create(
    client: httpx.AsyncClient,
) -> None:
    """
    ``POST /v1/sessions`` with ``multipart/form-data`` still bundled-creates.

    The multipart upload path (JSON ``metadata`` part + ``bundle`` file
    part) must remain reachable: the json-or-multipart guard accepts
    ``multipart/form-data`` and the handler's content-type dispatch routes
    it to the bundled-create form path, returning 201. A 415 here would
    mean the guard broke the runner-state create path.
    """
    bundle = build_agent_bundle(name="csrf-multipart-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    # The bundled-create response carries the new session id → multipart
    # routed to the bundled-create branch, not rejected at the gate.
    assert "session_id" in resp.json()
