"""
Integration tests for the embedded-browser action bridge routes.

Covers the risk-critical server-side routes that carry a runner ``browser_*``
tool call to the desktop renderer and back:

- ``POST /sessions/{id}/browser/action_request`` — parks a Future,
  publishes ``browser.action_request``, awaits, and returns the renderer
  result or a clean timeout error.
- ``POST /sessions/{id}/browser/action_claim/{action_id}`` — the atomic
  one-winner claim lease that prevents double execution when several
  renderers are subscribed to the same session stream.
- ``POST /sessions/{id}/browser/action_result/{action_id}`` — the
  claim-token + owner guarded resolution.

These are concurrency- and security-sensitive: a future refactor could
reintroduce an ``await`` in the claim critical section or weaken a
guard, so the guarantees are pinned here — two concurrent claims yield
exactly one winner, tokenless / wrong-token / wrong-session results are
all rejected (403), a post-``future.done()`` result is a graceful
no-op, and the timeout path leaves the registry clean.

Uses the shared ``client`` fixture (real stores + mock LLM); the
``auth_client`` cross-user gate mirrors the elicitation-resolve tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime import session_stream
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.routes import sessions as sessions_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Auth-enabled fixtures (mirroring test_sessions_elicitation_resolve_url) ──


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App fixture with a permission store + auth provider enabled."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_browser_registries() -> Any:
    """
    Clear the module-global browser registries around each test.

    They are keyed by ``action_id`` and process-global, so a leaked
    Future / owner / claim from one test would be visible to the next.
    """
    sessions_routes._browser_action_registry.clear()
    sessions_routes._browser_action_owners.clear()
    sessions_routes._browser_action_claims.clear()
    yield
    sessions_routes._browser_action_registry.clear()
    sessions_routes._browser_action_owners.clear()
    sessions_routes._browser_action_claims.clear()


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    user: str | None = None,
) -> str:
    """Create a minimal session and return its id."""
    headers = {"X-Forwarded-Email": user} if user is not None else None
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id}, headers=headers)
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _drain_until_action_request(
    session_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Block on a session stream until a ``browser.action_request`` arrives.

    The request route publishes the SSE event before awaiting the
    Future, so subscribing is how a test learns the minted ``action_id``
    without monkey-patching ``secrets``.

    :param session_id: Session to subscribe to.
    :param subscribed: Optional event set once the subscriber registers.
    :param timeout_s: Max seconds before failing the test.
    :returns: The full ``browser.action_request`` event.
    """

    async def _on_subscribed() -> tuple[dict[str, Any], ...]:
        if subscribed is not None:
            subscribed.set()
        return ()

    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id, on_subscribed=_on_subscribed):
            if event.get("type") == "browser.action_request":
                return event
    raise AssertionError("subscribe loop ended without a browser.action_request event")


async def _park_action_request(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    action: str = "navigate",
    args: dict[str, Any] | None = None,
) -> tuple[asyncio.Task[httpx.Response], str]:
    """
    Fire the action_request route and capture its minted ``action_id``.

    Starts the request POST as a background task (it blocks on the
    server-side Future until a result arrives or the await elapses) and
    subscribes to learn the ``action_id``.

    :returns: The in-flight request-POST task and the ``action_id``.
    """
    subscribed = asyncio.Event()
    drain = asyncio.create_task(_drain_until_action_request(session_id, subscribed=subscribed))
    await subscribed.wait()
    request_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/browser/action_request",
            json={"action": action, "args": args or {"url": "https://example.com"}},
        )
    )
    event = await drain
    action_id = event["action_id"]
    assert isinstance(action_id, str) and action_id.startswith("baction_")
    assert event["action"] == action
    return request_task, action_id


# ── request/result round-trip ────────────────────────────────────


async def test_action_request_resolves_on_result(client: httpx.AsyncClient) -> None:
    """
    A claimed result resolves the parked Future: the request POST returns
    the renderer's result JSON — the core happy path.
    """
    agent = await create_test_agent(client, "test-browser-roundtrip")
    session_id = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_id, action="navigate")

    claim = await client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}")
    assert claim.status_code == 200, claim.text
    token = claim.json()["claim_token"]

    result = await client.post(
        f"/v1/sessions/{session_id}/browser/action_result/{action_id}",
        json={"result": {"final_url": "https://example.com/landed"}, "claim_token": token},
    )
    assert result.status_code == 202, result.text
    assert result.json() == {"resolved": True}

    resp = await request_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"final_url": "https://example.com/landed"}


# ── atomic claim lease ───────────────────────────────────────────


async def test_two_concurrent_claims_exactly_one_winner(client: httpx.AsyncClient) -> None:
    """
    Two renderers claiming the same action concurrently → exactly one
    ``{claimed: true}``. Guards the atomic check-and-set against double
    execution via session-stream fan-out.
    """
    agent = await create_test_agent(client, "test-browser-claim-race")
    session_id = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_id)

    try:
        claim_a, claim_b = await asyncio.gather(
            client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}"),
            client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}"),
        )
        assert claim_a.status_code == 200 and claim_b.status_code == 200
        winners = [c.json() for c in (claim_a, claim_b) if c.json().get("claimed")]
        losers = [c.json() for c in (claim_a, claim_b) if not c.json().get("claimed")]
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"
        assert len(losers) == 1
        assert winners[0]["claim_token"]
        assert losers[0] == {"claimed": False}
    finally:
        request_task.cancel()
        # Await the cancellation so no task is left pending; gather with
        # return_exceptions swallows the CancelledError (and gives the await an
        # observable result, unlike a bare `await` the linter flags as no-op).
        await asyncio.gather(request_task, return_exceptions=True)


async def test_concurrent_claims_winner_token_is_the_stored_token(
    client: httpx.AsyncClient,
) -> None:
    """
    Two concurrent claims on one action → exactly one winner, and the
    token the winner receives is precisely the one persisted in
    ``_browser_action_claims``. Pins the atomic ``setdefault`` lease: the
    loser must see the winner's stored token (not its own) and bail, so
    the winner's token can never be clobbered by the racer.
    """
    agent = await create_test_agent(client, "test-browser-claim-stored-token")
    session_id = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_id)

    try:
        claim_a, claim_b = await asyncio.gather(
            client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}"),
            client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}"),
        )
        assert claim_a.status_code == 200 and claim_b.status_code == 200
        winners = [c.json() for c in (claim_a, claim_b) if c.json().get("claimed")]
        losers = [c.json() for c in (claim_a, claim_b) if not c.json().get("claimed")]
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"
        assert len(losers) == 1
        assert losers[0] == {"claimed": False}
        # The winner's token is exactly what's stored in the registry.
        assert sessions_routes._browser_action_claims[action_id] == winners[0]["claim_token"], (
            "stored claim token must equal the winner's token"
        )
    finally:
        request_task.cancel()
        # Await the cancellation so no task is left pending; gather with
        # return_exceptions swallows the CancelledError (and gives the await an
        # observable result, unlike a bare `await` the linter flags as no-op).
        await asyncio.gather(request_task, return_exceptions=True)


async def test_claim_unknown_action_returns_not_claimed(client: httpx.AsyncClient) -> None:
    """Claiming an unknown / already-resolved action → ``{claimed: false}``."""
    agent = await create_test_agent(client, "test-browser-claim-unknown")
    session_id = await _create_session(client, agent["id"])
    claim = await client.post(
        f"/v1/sessions/{session_id}/browser/action_claim/baction_does_not_exist"
    )
    assert claim.status_code == 200, claim.text
    assert claim.json() == {"claimed": False}


# ── result-route guards ──────────────────────────────────────────


async def test_result_without_token_rejected(client: httpx.AsyncClient) -> None:
    """A result with no claim_token is rejected (403); Future stays parked."""
    agent = await create_test_agent(client, "test-browser-no-token")
    session_id = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_id)

    try:
        # Claim first so a token EXISTS — proving it's the *missing body*
        # token, not the absence of any claim, that triggers the 403.
        await client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}")
        resp = await client.post(
            f"/v1/sessions/{session_id}/browser/action_result/{action_id}",
            json={"result": {"ok": True}},
        )
        assert resp.status_code == 403, resp.text
        assert not request_task.done(), "tokenless result wrongly resolved the Future"
    finally:
        request_task.cancel()
        # Await the cancellation so no task is left pending; gather with
        # return_exceptions swallows the CancelledError (and gives the await an
        # observable result, unlike a bare `await` the linter flags as no-op).
        await asyncio.gather(request_task, return_exceptions=True)


async def test_result_wrong_token_rejected(client: httpx.AsyncClient) -> None:
    """A result with a mismatched claim_token is rejected (403)."""
    agent = await create_test_agent(client, "test-browser-wrong-token")
    session_id = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_id)

    try:
        await client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}")
        resp = await client.post(
            f"/v1/sessions/{session_id}/browser/action_result/{action_id}",
            json={"result": {"ok": True}, "claim_token": "not_the_real_token"},
        )
        assert resp.status_code == 403, resp.text
        assert not request_task.done()
    finally:
        request_task.cancel()
        # Await the cancellation so no task is left pending; gather with
        # return_exceptions swallows the CancelledError (and gives the await an
        # observable result, unlike a bare `await` the linter flags as no-op).
        await asyncio.gather(request_task, return_exceptions=True)


async def test_result_wrong_session_rejected(client: httpx.AsyncClient) -> None:
    """
    A result delivered under a DIFFERENT session is rejected even with a
    valid token minted under the owning session — the owner guard.
    """
    agent = await create_test_agent(client, "test-browser-wrong-session")
    session_a = await _create_session(client, agent["id"])
    session_b = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_a)

    try:
        claim = await client.post(f"/v1/sessions/{session_a}/browser/action_claim/{action_id}")
        token = claim.json()["claim_token"]
        # Post the result under session B with A's real token.
        resp = await client.post(
            f"/v1/sessions/{session_b}/browser/action_result/{action_id}",
            json={"result": {"ok": True}, "claim_token": token},
        )
        assert resp.status_code == 403, resp.text
        assert not request_task.done(), "cross-session result wrongly resolved A's Future"
    finally:
        request_task.cancel()
        # Await the cancellation so no task is left pending; gather with
        # return_exceptions swallows the CancelledError (and gives the await an
        # observable result, unlike a bare `await` the linter flags as no-op).
        await asyncio.gather(request_task, return_exceptions=True)


async def test_second_result_after_done_is_noop(client: httpx.AsyncClient) -> None:
    """
    A second result after the Future is already resolved → ``{resolved:
    false}`` and no exception (the ``future.done()`` guard).
    """
    agent = await create_test_agent(client, "test-browser-double-result")
    session_id = await _create_session(client, agent["id"])
    request_task, action_id = await _park_action_request(client, session_id)

    claim = await client.post(f"/v1/sessions/{session_id}/browser/action_claim/{action_id}")
    token = claim.json()["claim_token"]

    first = await client.post(
        f"/v1/sessions/{session_id}/browser/action_result/{action_id}",
        json={"result": {"n": 1}, "claim_token": token},
    )
    assert first.json() == {"resolved": True}
    # The request POST has now returned and the registry entry is gone.
    resp = await request_task
    assert resp.status_code == 200
    assert resp.json() == {"n": 1}

    # A late duplicate must not raise; the action is no longer registered
    # (owner entry cleaned up), so it's rejected by the owner guard (403)
    # rather than resolving anything a second time.
    second = await client.post(
        f"/v1/sessions/{session_id}/browser/action_result/{action_id}",
        json={"result": {"n": 2}, "claim_token": token},
    )
    assert second.status_code == 403, second.text


# ── timeout / cleanup ────────────────────────────────────────────


async def test_timeout_returns_clean_json_and_cleans_registry(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no renderer result, the await elapses and the route returns the
    clean timeout JSON — and the registry, owner, and claim entries are
    all removed in the ``finally`` (no leak).
    """
    # Shrink the await so the test doesn't wait 30s.
    monkeypatch.setattr(sessions_routes, "_BROWSER_ACTION_AWAIT_S", 0.2)

    agent = await create_test_agent(client, "test-browser-timeout")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/browser/action_request",
        json={"action": "snapshot", "args": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "timed out" in body["error"]
    assert "Omnigent desktop app" in body["error"]

    # Registry fully cleaned — no leaked Future / owner / claim.
    assert sessions_routes._browser_action_registry == {}
    assert sessions_routes._browser_action_owners == {}
    assert sessions_routes._browser_action_claims == {}


async def test_action_request_rejects_empty_action(client: httpx.AsyncClient) -> None:
    """A request with a missing / empty ``action`` is rejected (400)."""
    agent = await create_test_agent(client, "test-browser-empty-action")
    session_id = await _create_session(client, agent["id"])
    resp = await client.post(
        f"/v1/sessions/{session_id}/browser/action_request",
        json={"action": "", "args": {}},
    )
    assert resp.status_code == 400, resp.text


# ── cross-user gate ──────────────────────────────────────────────


async def test_action_request_cross_user_forbidden(auth_client: httpx.AsyncClient) -> None:
    """
    A non-owner cannot reach the action_request route when auth is active
    — the ``LEVEL_EDIT`` gate fences the bridge just like elicitation.
    """
    agent = await create_test_agent(auth_client, user="alice@example.com")
    session_id = await _create_session(auth_client, agent["id"], user="alice@example.com")

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/browser/action_request",
        json={"action": "navigate", "args": {"url": "https://x"}},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert resp.status_code in (403, 404), resp.text
