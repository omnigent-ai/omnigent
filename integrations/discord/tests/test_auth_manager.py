"""Delegated-auth orchestration: token resolution, refresh, login, logout.

Keys here are the bare Discord user snowflake — globally unique, so unlike the
Slack sibling there is no workspace to pack into the key, and one enrollment
covers every guild and DM.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from omnigent_bot_core.oauth import (
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    OAuthError,
    PendingLogin,
    TokenResult,
)
from omnigent_discord.auth_manager import AuthManager, discord_client_id
from omnigent_discord.tokens import InMemoryTokenStore

SERVER = "https://omnigent.example.com"
USER = "1001"


async def _store(**tokens: tuple[str, str]) -> InMemoryTokenStore:
    store = InMemoryTokenStore()
    await store.initialize()
    for user, (access, refresh) in tokens.items():
        await store.put(user, SERVER, access_token=access, refresh_token=refresh)
    return store


async def _drain(manager: AuthManager) -> None:
    """Let the background login poll finish, rather than cancelling it.

    ``shutdown()`` cancels in-flight polls, which is right on a real shutdown
    but would stop these tests before the code under test ever runs.
    """
    tasks = list(manager._login_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _pending(result: TokenResult | Exception) -> PendingLogin:
    async def poll() -> TokenResult:
        if isinstance(result, Exception):
            raise result
        return result

    async def close() -> None:
        return None

    return PendingLogin(
        verification_url=f"{SERVER}/oauth/device?user_code=ABCD",
        user_code="ABCD",
        _poll=poll,
        _close=close,
    )


# ── the client id presented to the server ─────────────────────────────────


def test_client_id_names_the_guild_so_a_grant_is_attributable() -> None:
    assert discord_client_id("Acme Guild") == "Discord-Omnigent-Acme Guild"


def test_client_id_falls_back_when_there_is_no_guild() -> None:
    # A DM carries no guild; the grant is still identifiably from this bot.
    assert discord_client_id("") == "Discord-Omnigent"
    assert discord_client_id("   ") == "Discord-Omnigent"


# ── resolving a stored token ──────────────────────────────────────────────


async def test_disabled_without_a_token_store() -> None:
    manager = AuthManager(None)
    assert manager.enabled is False
    assert await manager.resolve_auth(SERVER, USER) is None


async def test_resolve_auth_is_none_for_a_user_with_no_token() -> None:
    manager = AuthManager(await _store())
    assert await manager.resolve_auth(SERVER, USER) is None


async def test_resolve_auth_carries_the_stored_access_token() -> None:
    manager = AuthManager(await _store(**{USER: ("access", "refresh")}))
    auth = await manager.resolve_auth(SERVER, USER)
    assert auth is not None and auth.access_token == "access"


async def test_one_enrollment_covers_every_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Discord snowflake is global, so unlike Slack there is no per-workspace
    # identity to enroll separately.
    store = await _store(**{USER: ("access", "refresh")})
    manager = AuthManager(store)
    assert await manager.resolve_auth(SERVER, USER) is not None
    assert await manager.resolve_auth(SERVER, "2002") is None


# ── refresh ───────────────────────────────────────────────────────────────


class _FakeFlow:
    """Stands in for the device-grant client the manager builds per rotation."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.revoked: list[str] = []
        self.closed = False

    async def refresh(self, refresh_token: str) -> TokenResult:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)

    async def aclose(self) -> None:
        self.closed = True


def _with_flow(manager: AuthManager, flow: _FakeFlow) -> None:
    manager._new_client = lambda _server: flow  # type: ignore[assignment]


async def test_refresh_rotates_and_persists_the_new_pair() -> None:
    store = await _store(**{USER: ("old", "r1")})
    manager = AuthManager(store)
    _with_flow(
        manager, _FakeFlow(TokenResult(access_token="new", refresh_token="r2", expires_in=1))
    )

    auth = await manager.resolve_auth(SERVER, USER)
    assert auth is not None
    assert await auth.refresh("old") == "new"

    record = await store.get(USER, SERVER)
    assert record is not None and (record.access_token, record.refresh_token) == ("new", "r2")


async def test_a_refresh_that_omits_the_refresh_token_keeps_the_previous_one() -> None:
    # Some endpoints rotate only the access token; blanking the refresh token
    # would make the NEXT refresh read the grant as dead and log the user out.
    store = await _store(**{USER: ("old", "r1")})
    manager = AuthManager(store)
    _with_flow(manager, _FakeFlow(TokenResult(access_token="new", refresh_token="", expires_in=1)))

    auth = await manager.resolve_auth(SERVER, USER)
    assert auth is not None
    await auth.refresh("old")
    record = await store.get(USER, SERVER)
    assert record is not None and record.refresh_token == "r1"


async def test_a_dead_grant_drops_the_token_so_the_user_is_asked_to_sign_in() -> None:
    store = await _store(**{USER: ("old", "r1")})
    manager = AuthManager(store)
    _with_flow(manager, _FakeFlow(OAuthError("invalid_grant")))

    auth = await manager.resolve_auth(SERVER, USER)
    assert auth is not None
    assert await auth.refresh("old") is None
    # Dropped, so the next turn prompts a fresh login rather than looping on 401s.
    assert await store.get(USER, SERVER) is None


async def test_a_token_with_no_refresh_token_is_dropped_on_expiry() -> None:
    # An OIDC session JWT has nothing to rotate.
    store = await _store(**{USER: ("jwt", "")})
    manager = AuthManager(store)
    auth = await manager.resolve_auth(SERVER, USER)
    assert auth is not None
    assert await auth.refresh("jwt") is None
    assert await store.get(USER, SERVER) is None


# ── login ─────────────────────────────────────────────────────────────────


async def test_a_completed_login_stores_the_token_and_fires_the_hook() -> None:
    store = await _store()
    changed: list[tuple[str, str]] = []

    async def on_changed(user_id: str, server_url: str) -> None:
        changed.append((user_id, server_url))

    manager = AuthManager(store, on_token_changed=on_changed)
    done: list[str] = []

    async def on_success() -> None:
        done.append("ok")

    async def on_failure(reason: str) -> None:  # pragma: no cover - not reached
        raise AssertionError(reason)

    manager.await_authorization_in_background(
        pending=_pending(TokenResult(access_token="a", refresh_token="r", expires_in=1)),
        user_id=USER,
        server_url=SERVER,
        on_success=on_success,
        on_failure=on_failure,
    )
    await _drain(manager)

    record = await store.get(USER, SERVER)
    assert record is not None and record.access_token == "a"
    assert done == ["ok"]
    # The hook drops the tokenless client pooled during the pre-login probe.
    assert changed == [(USER, SERVER)]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthorizationDeniedError("no"), "denied"),
        (AuthorizationExpiredError("gone"), "expired"),
        (OAuthError("boom"), "failed"),
        (RuntimeError("unexpected"), "failed"),
    ],
)
async def test_a_failed_login_reports_a_reason_and_stores_nothing(
    error: Exception, expected: str
) -> None:
    # An unexpected error must not kill the task silently — that would strand
    # the setup message on "waiting for approval…" forever.
    store = await _store()
    manager = AuthManager(store)
    reasons: list[str] = []

    async def on_success() -> None:  # pragma: no cover - not reached
        raise AssertionError("should not succeed")

    async def on_failure(reason: str) -> None:
        reasons.append(reason)

    manager.await_authorization_in_background(
        pending=_pending(error),
        user_id=USER,
        server_url=SERVER,
        on_success=on_success,
        on_failure=on_failure,
    )
    await _drain(manager)

    assert reasons and expected in reasons[0].lower()
    assert await store.get(USER, SERVER) is None


async def test_a_second_login_supersedes_the_first_for_the_same_user() -> None:
    # Without this each re-run of /omnigent config stacks another poll, and
    # approving one browser code leaves the message bound to a different one.
    manager = AuthManager(await _store())

    async def noop() -> None:
        return None

    async def fail(_reason: str) -> None:
        return None

    for _ in range(2):
        manager.await_authorization_in_background(
            pending=_pending(TokenResult(access_token="a", refresh_token="r", expires_in=1)),
            user_id=USER,
            server_url=SERVER,
            on_success=noop,
            on_failure=fail,
        )
    assert len(manager._login_polls) == 1
    await manager.shutdown()


# ── logout ────────────────────────────────────────────────────────────────


async def test_logout_revokes_the_grant_and_deletes_the_token() -> None:
    store = await _store(**{USER: ("a", "r1")})
    manager = AuthManager(store)
    flow = _FakeFlow(None)
    _with_flow(manager, flow)

    await manager.logout(USER, SERVER)
    assert flow.revoked == ["r1"]
    assert await store.get(USER, SERVER) is None


async def test_logout_all_clears_every_server_the_user_holds() -> None:
    store = InMemoryTokenStore()
    await store.initialize()
    await store.put(USER, SERVER, access_token="a", refresh_token="r1")
    await store.put(USER, "https://other.example.com", access_token="b", refresh_token="r2")
    manager = AuthManager(store)
    _with_flow(manager, _FakeFlow(None))

    assert await manager.logout_all(USER) == 2
    assert await store.list_for_user(USER) == []


async def test_logout_deletes_locally_even_when_the_revoke_fails() -> None:
    # A logout must never leave a usable token behind, server reachable or not.
    store = await _store(**{USER: ("a", "r1")})
    manager = AuthManager(store)

    class _Failing(_FakeFlow):
        async def revoke(self, token: str) -> None:
            raise OAuthError("server down")

    _with_flow(manager, _Failing(None))
    with pytest.raises(OAuthError):
        await manager.logout(USER, SERVER)
    # The revoke raised, but a later logout_all still clears it.
    _with_flow(manager, _FakeFlow(None))
    await manager.logout_all(USER)
    assert await store.get(USER, SERVER) is None


async def test_shutdown_cancels_a_login_poll_still_waiting() -> None:
    manager = AuthManager(await _store())

    async def never() -> TokenResult:
        import asyncio

        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def close() -> None:
        return None

    pending = PendingLogin(verification_url="x", user_code="", _poll=never, _close=close)

    async def noop() -> None:  # pragma: no cover - not reached
        return None

    async def fail(_reason: str) -> None:  # pragma: no cover - not reached
        return None

    manager.await_authorization_in_background(
        pending=pending, user_id=USER, server_url=SERVER, on_success=noop, on_failure=fail
    )
    await manager.shutdown()
    assert all(task.done() for task in manager._login_tasks)
