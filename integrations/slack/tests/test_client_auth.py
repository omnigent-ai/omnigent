from __future__ import annotations

import asyncio

import httpx
import respx
from omnigent_slack.omnigent import ClientAuth, OmnigentClient, OmnigentClientPool

_BASE = "http://omnigent.test"


@respx.mock
async def test_bearer_attached_to_requests() -> None:
    route = respx.get(_BASE + "/v1/agents").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    auth = ClientAuth("tok-1", _no_refresh)
    client = OmnigentClient(_BASE, auth=auth)
    try:
        await client.list_agents()
    finally:
        await client.aclose()
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_refresh_on_401_then_retry() -> None:
    calls: list[str | None] = []

    def _record(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Authorization"))
        # First call (stale token) → 401; retry with refreshed token → 200.
        if len(calls) == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"data": []})

    respx.get(_BASE + "/v1/agents").mock(side_effect=_record)

    async def _refresh() -> str | None:
        return "tok-2"

    auth = ClientAuth("tok-1", _refresh)
    client = OmnigentClient(_BASE, auth=auth)
    try:
        await client.list_agents()
    finally:
        await client.aclose()
    assert calls == ["Bearer tok-1", "Bearer tok-2"]
    assert auth.access_token == "tok-2"


async def test_concurrent_refresh_rotates_once() -> None:
    """Concurrent 401s on one ClientAuth trigger a single rotation.

    Rotating refresh tokens are single-use, so a second rotation would
    consume the just-minted token and revoke the grant. The single-flight
    guard makes the loser adopt the winner's token instead of re-rotating.
    """
    rotations = 0

    async def _refresh() -> str | None:
        nonlocal rotations
        rotations += 1
        await asyncio.sleep(0.01)  # let the second caller pile up on the lock
        return f"tok-{rotations + 1}"

    auth = ClientAuth("tok-1", _refresh)
    # Both callers observed the same stale token "tok-1" on their 401.
    results = await asyncio.gather(auth.refresh("tok-1"), auth.refresh("tok-1"))

    assert rotations == 1
    assert results == ["tok-2", "tok-2"]
    assert auth.access_token == "tok-2"


@respx.mock
async def test_pool_keys_by_server_and_user() -> None:
    async def resolver(server_url: str, user_id: str) -> ClientAuth | None:
        return ClientAuth(f"tok-{user_id}", _no_refresh)

    pool = OmnigentClientPool(auth_resolver=resolver)
    try:
        c1 = await pool.get(_BASE, "U1")
        c1_again = await pool.get(_BASE, "U1")
        c2 = await pool.get(_BASE, "U2")
    finally:
        await pool.aclose_all()
    assert c1 is c1_again
    assert c1 is not c2


@respx.mock
async def test_invalidate_rebuilds_client_with_new_token() -> None:
    """After login, the tokenless probe client is replaced by an authed one.

    Reproduces the "still asks me to log in after auth" bug: the pre-login
    probe caches an unauthenticated client; without invalidation the pool
    keeps returning it and every request 401s.
    """
    token: str | None = None

    async def resolver(server_url: str, user_id: str) -> ClientAuth | None:
        return ClientAuth(token, _no_refresh) if token else None

    pool = OmnigentClientPool(auth_resolver=resolver)
    try:
        # Pre-login probe: no token yet → unauthenticated client, cached.
        before = await pool.get(_BASE, "U1")
        assert before._auth is None

        # Login stores a token and invalidates the cached client.
        token = "tok-1"
        await pool.invalidate(_BASE, "U1")

        # Next get rebuilds with the fresh token.
        after = await pool.get(_BASE, "U1")
        assert after is not before
        assert after._auth is not None
        assert after._auth.access_token == "tok-1"
    finally:
        await pool.aclose_all()


async def _no_refresh() -> str | None:
    return None
