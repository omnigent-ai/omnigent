from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import respx
from cryptography.fernet import Fernet
from omnigent_slack.auth_manager import AuthManager, slack_client_id
from omnigent_slack.tokens import EncryptedTokenStore, TokenStore

_BASE = "http://omnigent.test"


async def _manager(tmp_path: Path) -> tuple[AuthManager, TokenStore]:
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    return AuthManager(store), store


def test_slack_client_id_format() -> None:
    assert slack_client_id("Acme Corp") == "Slack-Omnigent-Acme Corp"
    # Missing/blank workspace name falls back to the bare label.
    assert slack_client_id("") == "Slack-Omnigent"
    assert slack_client_id("  ") == "Slack-Omnigent"


async def test_disabled_without_key() -> None:
    mgr = AuthManager(None)
    assert mgr.enabled is False
    assert await mgr.resolve_auth(_BASE, "T1:U1") is None


def _mock_authorize() -> None:
    # Device-grant path: /v1/me → accounts mode, then the device authorize.
    respx.get(_BASE + "/v1/me").mock(
        return_value=httpx.Response(401, json={"login_url": "/login"})
    )
    respx.post(_BASE + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _BASE + "/oauth/device",
                "verification_uri_complete": _BASE + "/oauth/device?user_code=ABCD-2345",
                "expires_in": 600,
                "interval": 0,
            },
        )
    )


@respx.mock
async def test_authorize_returns_link_and_await_persists_on_approval(tmp_path: Path) -> None:
    _mock_authorize()
    respx.post(_BASE + "/oauth/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    mgr, store = await _manager(tmp_path)

    pending = await mgr.authorize(server_url=_BASE, client_id="Slack-Omnigent-Test")
    assert "ABCD-2345" in pending.verification_url

    succeeded: list[bool] = []

    async def on_success() -> None:
        succeeded.append(True)

    async def on_failure(reason: str) -> None:
        raise AssertionError(f"unexpected failure: {reason}")

    mgr.await_authorization_in_background(
        pending=pending,
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
    )
    for _ in range(50):
        if succeeded:
            break
        await asyncio.sleep(0.05)

    assert succeeded == [True]
    rec = await store.get("T1", "U1", _BASE)
    assert rec is not None and rec.access_token == "at"


@respx.mock
async def test_await_authorization_denied_calls_on_failure(tmp_path: Path) -> None:
    _mock_authorize()
    respx.post(_BASE + "/oauth/token").mock(
        return_value=httpx.Response(400, json={"error": "access_denied"})
    )
    mgr, store = await _manager(tmp_path)
    pending = await mgr.authorize(server_url=_BASE, client_id="Slack-Omnigent-Test")

    failures: list[str] = []

    async def on_success() -> None:
        raise AssertionError("should not succeed")

    async def on_failure(reason: str) -> None:
        failures.append(reason)

    mgr.await_authorization_in_background(
        pending=pending,
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
    )
    for _ in range(50):
        if failures:
            break
        await asyncio.sleep(0.05)

    assert failures and "denied" in failures[0].lower()
    assert await store.get("T1", "U1", _BASE) is None


@respx.mock
async def test_login_fires_token_changed_hook(tmp_path: Path) -> None:
    """On successful login the hook fires so the pool drops its stale client."""
    _mock_authorize()
    respx.post(_BASE + "/oauth/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    changed: list[tuple[str, str, str]] = []

    async def hook(team_id: str, user_id: str, server_url: str) -> None:
        changed.append((team_id, user_id, server_url))

    mgr = AuthManager(store, on_token_changed=hook)
    pending = await mgr.authorize(server_url=_BASE, client_id="Slack-Omnigent-Test")

    async def _noop() -> None:
        return None

    async def _noop_fail(reason: str) -> None:
        return None

    mgr.await_authorization_in_background(
        pending=pending,
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=_noop,
        on_failure=_noop_fail,
    )
    for _ in range(50):
        if changed:
            break
        await asyncio.sleep(0.05)
    assert changed == [("T1", "U1", _BASE)]


@respx.mock
async def test_logout_revokes_and_deletes(tmp_path: Path) -> None:
    revoked = respx.post(_BASE + "/oauth/revoke").mock(return_value=httpx.Response(200))
    mgr, store = await _manager(tmp_path)
    await store.put("T1", "U1", _BASE, access_token="at", refresh_token="rt")

    await mgr.logout("T1", "U1", _BASE)

    assert revoked.called
    assert await store.get("T1", "U1", _BASE) is None


@respx.mock
async def test_logout_all_revokes_every_server(tmp_path: Path) -> None:
    """logout_all revokes and deletes the user's token on every server."""
    other = "http://other.test"
    revoke_a = respx.post(_BASE + "/oauth/revoke").mock(return_value=httpx.Response(200))
    revoke_b = respx.post(other + "/oauth/revoke").mock(return_value=httpx.Response(200))
    mgr, store = await _manager(tmp_path)
    await store.put("T1", "U1", _BASE, access_token="a", refresh_token="ra")
    await store.put("T1", "U1", other, access_token="b", refresh_token="rb")
    # A different user's token must be left untouched.
    await store.put("T1", "U2", _BASE, access_token="c", refresh_token="rc")

    count = await mgr.logout_all("T1", "U1")

    assert count == 2
    assert revoke_a.called and revoke_b.called
    assert await store.get("T1", "U1", _BASE) is None
    assert await store.get("T1", "U1", other) is None
    assert await store.get("T1", "U2", _BASE) is not None


@respx.mock
async def test_logout_all_deletes_even_if_revoke_fails(tmp_path: Path) -> None:
    """A failed server revoke still clears the local token (no leftover)."""
    respx.post(_BASE + "/oauth/revoke").mock(return_value=httpx.Response(500))
    mgr, store = await _manager(tmp_path)
    await store.put("T1", "U1", _BASE, access_token="a", refresh_token="ra")

    count = await mgr.logout_all("T1", "U1")

    assert count == 1
    assert await store.get("T1", "U1", _BASE) is None


@respx.mock
async def test_resolve_auth_refresh_drops_dead_grant(tmp_path: Path) -> None:
    """A refresh that fails (revoked grant) clears the stored token."""
    respx.post(_BASE + "/oauth/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    mgr, store = await _manager(tmp_path)
    await store.put("T1", "U1", _BASE, access_token="at", refresh_token="rt")

    auth = await mgr.resolve_auth(_BASE, "T1:U1")
    assert auth is not None
    # Refresh fails → returns None and deletes the dead token.
    assert await auth.refresh(auth.access_token) is None
    assert await store.get("T1", "U1", _BASE) is None


@respx.mock
async def test_oidc_login_stores_session_jwt_no_refresh(tmp_path: Path) -> None:
    """OIDC mode uses the cli-ticket flow and stores a refreshless session JWT."""
    respx.get(_BASE + "/v1/me").mock(
        return_value=httpx.Response(401, json={"login_url": "/auth/login"})
    )
    respx.post(_BASE + "/auth/cli-login").mock(
        return_value=httpx.Response(
            200, json={"ticket": "T1", "login_url": "/auth/login?ticket=T1"}
        )
    )
    respx.get(_BASE + "/auth/cli-poll").mock(
        return_value=httpx.Response(
            200, json={"token": "sess", "user_id": "a@x", "expires_in": 60}
        )
    )
    mgr, store = await _manager(tmp_path)

    pending = await mgr.authorize(server_url=_BASE, client_id="Slack-Omnigent-Test")
    assert "ticket=T1" in pending.verification_url
    assert pending.user_code == ""  # no code in the OIDC flow

    done: list[bool] = []

    async def on_success() -> None:
        done.append(True)

    async def on_failure(reason: str) -> None:
        raise AssertionError(f"unexpected failure: {reason}")

    mgr.await_authorization_in_background(
        pending=pending,
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
    )
    for _ in range(50):
        if done:
            break
        await asyncio.sleep(0.05)

    assert done == [True]
    rec = await store.get("T1", "U1", _BASE)
    assert rec is not None
    assert rec.access_token == "sess"
    assert rec.refresh_token == ""  # session JWT — no refresh token


async def test_resolve_auth_no_refresh_token_drops_on_expiry(tmp_path: Path) -> None:
    """A stored session JWT with no refresh token can't refresh — it's dropped."""
    mgr, store = await _manager(tmp_path)
    await store.put("T1", "U1", _BASE, access_token="sess", refresh_token="")

    auth = await mgr.resolve_auth(_BASE, "T1:U1")
    assert auth is not None
    # No refresh token → refresh is a no-op returning None, and the dead
    # token is cleared so the next turn prompts a fresh login.
    assert await auth.refresh(auth.access_token) is None
    assert await store.get("T1", "U1", _BASE) is None
