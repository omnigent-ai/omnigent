"""Integration tests for the explicit ``clear`` control event.

The web-UI ``/clear`` command POSTs ``{"type": "clear"}`` to
``POST /v1/sessions/{id}/events``. Like ``compact`` (see
``test_sessions_compact.py``), the server forwards the control to the bound
runner and lets the runner's harness-specific handler own the operation —
that is what lets the composer offer one ``/clear`` while each vendor keeps
its own command name (``/clear`` for Claude Code, ``/new`` for Codex).

The runner's dispatch contract (verified in
``tests/runner/test_app_sessions_native_events_options.py``):

* Harnesses with a new-conversation handler inject the vendor command and
  return **200** on success or **5xx** on failure.
* Every other harness returns **204** (no-op); the server surfaces a 400.

These tests pin the server side of that contract by stubbing the runner's
HTTP response.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a bare session bound to *agent_id* and return its id.

    :param client: The test HTTP client.
    :param agent_id: Agent id to bind, e.g. ``"ag_abc123"``.
    :returns: The new session id, e.g. ``"conv_abc123"``.
    """
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _fake_runner_returning(clear_status: int) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """
    Build a mock runner client that returns *clear_status* for clear.

    The transport records every ``{"type": "clear"}`` body it sees so the
    test can assert the server actually forwarded the control, and returns
    204 for any other runner POST so unrelated session traffic passes
    through.

    :param clear_status: HTTP status the fake runner returns for a ``clear``
        ``/events`` POST, e.g. ``200`` (vendor command injected), ``204``
        (no handler for this harness), or ``503`` (pane not attached).
    :returns: The mock ``httpx.AsyncClient`` and the list that captures
        forwarded clear bodies.
    """
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record clear POSTs and return the configured status."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        if isinstance(body, dict) and body.get("type") == "clear":
            captured.append(body)
            return httpx.Response(clear_status)
        return httpx.Response(204)

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    return runner, captured


async def test_clear_is_forwarded_to_the_runner_and_reported_handled(
    client: httpx.AsyncClient,
) -> None:
    """
    A 200 from the runner (vendor command injected) returns ``queued=False``.

    Nothing is persisted server-side: the harness's own rotation supersedes
    this conversation and the resulting ``session.superseded`` event is what
    moves the open client.
    """
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner_returning(200)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "clear", "data": {}},
        )
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}, resp.text
    assert captured == [{"type": "clear"}], (
        f"Server must forward exactly one clear control to the runner; got {captured!r}."
    )


async def test_clear_returns_error_when_runner_noops(
    client: httpx.AsyncClient,
) -> None:
    """
    A 204 from the runner (harness with no handler) surfaces a 400.

    This is what keeps the composer's harness gate honest: reporting success
    here would tell the user a new conversation started when the runner did
    nothing at all.
    """
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner_returning(204)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "clear", "data": {}},
        )
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 400, resp.text
    assert "/clear is not available" in resp.text
    # Control was still forwarded before the error.
    assert captured == [{"type": "clear"}], (
        f"Server must forward clear to the runner before returning the error; got {captured!r}."
    )


async def test_clear_errors_when_runner_injection_fails(
    client: httpx.AsyncClient,
) -> None:
    """
    A 503 from the runner (pane not attached) surfaces as an error.

    The vendor TUI is the only thing that can rotate the session, so a failed
    inject must reach the user rather than being reported as a fresh
    conversation that does not exist.
    """
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner_returning(503)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "clear", "data": {}},
        )
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 500, resp.text
    assert captured == [{"type": "clear"}], (
        f"Server must have forwarded the clear control before surfacing the "
        f"runner failure; got {captured!r}."
    )


async def test_clear_without_a_runner_returns_not_available(
    client: httpx.AsyncClient,
) -> None:
    """
    A clear for an SDK-harness session with no runner returns a clear 400.

    Without a runner there is no vendor terminal to rotate, and the server has
    no equivalent of its own.
    """
    agent = await create_test_agent(
        client,
        name="sdk-no-runner-clear",
        executor={"type": "omnigent", "config": {"harness": "openai-agents"}},
        include_llm=False,
    )
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "clear", "data": {}},
    )

    assert resp.status_code == 400, resp.text
    assert "/clear is not available" in resp.text
