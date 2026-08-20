from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
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


async def test_enrollment_waits_for_fresh_token_over_stale(tmp_path: Path) -> None:
    # A stale token from a prior sign-in already exists. The enrollment poll must
    # NOT fire on_success against it — that would advance the modal before the new
    # token lands, 401 on validate, and hang the modal. It fires only once the
    # stored access token CHANGES (the fresh enrollment write).
    mgr, store = await _manager(tmp_path)
    await store.put("T1", "U1", _BASE, access_token="stale", refresh_token="")

    succeeded = asyncio.Event()
    failed: list[str] = []

    async def on_success() -> None:
        succeeded.set()

    async def on_failure(reason: str) -> None:
        failed.append(reason)

    mgr.await_enrollment_in_background(
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
        poll_interval_seconds=0.01,
    )

    # The stale token is present but unchanged — on_success must not fire yet.
    await asyncio.sleep(0.1)
    assert not succeeded.is_set()
    assert failed == []

    # The user finishes: the enrollment web server writes the FRESH token.
    await store.put("T1", "U1", _BASE, access_token="fresh", refresh_token="")

    await asyncio.wait_for(succeeded.wait(), timeout=1.0)
    assert failed == []


async def test_enrollment_fires_on_first_token_when_none_existed(tmp_path: Path) -> None:
    # The common case: no prior token. The first stored token is the fresh one,
    # so on_success fires as soon as it lands.
    mgr, store = await _manager(tmp_path)

    succeeded = asyncio.Event()

    async def on_success() -> None:
        succeeded.set()

    async def on_failure(reason: str) -> None:
        return None

    mgr.await_enrollment_in_background(
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
        poll_interval_seconds=0.01,
    )
    await asyncio.sleep(0.05)
    assert not succeeded.is_set()

    await store.put("T1", "U1", _BASE, access_token="first", refresh_token="")
    await asyncio.wait_for(succeeded.wait(), timeout=1.0)


class _FakeRotator:
    """Stands in for DatabricksOAuthClient as the AuthManager's rotator."""

    def __init__(self, result: object) -> None:
        self._result = result
        self.revoked: list[str] = []

    async def refresh(self, refresh_token: str):  # type: ignore[no-untyped-def]
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)


class _Pair:
    def __init__(self, access_token: str, refresh_token: str) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token


async def test_rotator_refresh_rotates_via_external_client(tmp_path: Path) -> None:
    # In databricks mode refresh goes to the workspace OAuth app (the rotator),
    # NOT the Omnigent server's /oauth/* — and a fresh pair is persisted.
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    rotator = _FakeRotator(_Pair("new-access", "new-refresh"))
    mgr = AuthManager(store, rotator=rotator)  # type: ignore[arg-type]
    await store.put("T1", "U1", _BASE, access_token="old", refresh_token="old-refresh")

    auth = await mgr.resolve_auth(_BASE, "T1:U1")
    assert auth is not None
    assert await auth.refresh(auth.access_token) == "new-access"
    rec = await store.get("T1", "U1", _BASE)
    assert rec is not None and rec.refresh_token == "new-refresh"


class _GrantExpired(RuntimeError):
    grant_expired = True


async def test_rotator_dead_grant_drops_token(tmp_path: Path) -> None:
    # A permanently-rejected grant (grant_expired marker) drops the token so the
    # user is re-prompted to sign in.
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    rotator = _FakeRotator(_GrantExpired("refresh token revoked"))
    mgr = AuthManager(store, rotator=rotator)  # type: ignore[arg-type]
    await store.put("T1", "U1", _BASE, access_token="old", refresh_token="dead")

    auth = await mgr.resolve_auth(_BASE, "T1:U1")
    assert auth is not None
    assert await auth.refresh(auth.access_token) is None
    assert await store.get("T1", "U1", _BASE) is None


async def test_rotator_refresh_omitted_retains_previous_refresh_token(tmp_path: Path) -> None:
    # A rotation that returns no refresh_token (endpoint keeps it implicitly)
    # must NOT overwrite the stored one with "" — else the next refresh treats
    # the grant as dead and logs the user out.
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    rotator = _FakeRotator(_Pair("new-access", ""))
    mgr = AuthManager(store, rotator=rotator)  # type: ignore[arg-type]
    await store.put("T1", "U1", _BASE, access_token="old", refresh_token="keep-me")

    auth = await mgr.resolve_auth(_BASE, "T1:U1")
    assert auth is not None
    assert await auth.refresh(auth.access_token) == "new-access"
    rec = await store.get("T1", "U1", _BASE)
    assert rec is not None and rec.refresh_token == "keep-me"


async def test_rotator_transient_failure_keeps_token(tmp_path: Path) -> None:
    # A transient failure (network blip / 5xx — no grant_expired marker) must NOT
    # discard a still-valid refresh grant. It raises TokenRefreshTransientError
    # (so the caller keeps the current access token and skips a re-login prompt),
    # and the stored refresh token is untouched.
    from omnigent_slack.omnigent import TokenRefreshTransientError

    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    rotator = _FakeRotator(RuntimeError("connection reset"))
    mgr = AuthManager(store, rotator=rotator)  # type: ignore[arg-type]
    await store.put("T1", "U1", _BASE, access_token="old", refresh_token="still-good")

    auth = await mgr.resolve_auth(_BASE, "T1:U1")
    assert auth is not None
    with pytest.raises(TokenRefreshTransientError):
        await auth.refresh(auth.access_token)
    # Access token kept (not blanked), refresh token untouched.
    assert auth.access_token == "old"
    rec = await store.get("T1", "U1", _BASE)
    assert rec is not None and rec.refresh_token == "still-good"


async def test_rotator_used_for_logout_revoke(tmp_path: Path) -> None:
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    rotator = _FakeRotator(_Pair("a", "b"))
    mgr = AuthManager(store, rotator=rotator)  # type: ignore[arg-type]
    await store.put("T1", "U1", _BASE, access_token="at", refresh_token="rt")

    await mgr.logout("T1", "U1", _BASE)

    assert rotator.revoked == ["rt"]
    assert await store.get("T1", "U1", _BASE) is None


async def test_shutdown_cancels_pending_enrollment_polls(tmp_path: Path) -> None:
    # A login/enrollment poll can run for minutes; shutdown must cancel it so it
    # isn't abandoned ("Task was destroyed but it is pending") with a live client.
    mgr, _store = await _manager(tmp_path)

    async def on_success() -> None:
        return None

    async def on_failure(reason: str) -> None:
        return None

    mgr.await_enrollment_in_background(
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
        poll_interval_seconds=0.05,
    )
    # A poll task is live (no token will ever appear in this test).
    assert len(mgr._login_tasks) == 1  # type: ignore[attr-defined]
    task = next(iter(mgr._login_tasks))  # type: ignore[attr-defined]

    await mgr.shutdown()

    assert task.done()
    assert mgr._login_tasks == set()  # type: ignore[attr-defined]


async def test_second_enrollment_poll_supersedes_prior_for_same_key(tmp_path: Path) -> None:
    # A re-run of setup for the same (team, user, server) must cancel the prior
    # in-flight poll. Otherwise each /omnigent stacks another poll, several race,
    # and approving one leaves the modal bound to a different pending attempt
    # stuck on "waiting…". The newer poll replaces the old one in _login_polls.
    mgr, store = await _manager(tmp_path)

    succeeded = asyncio.Event()

    async def on_success() -> None:
        succeeded.set()

    async def on_failure(reason: str) -> None:
        return None

    kwargs = {
        "team_id": "T1",
        "user_id": "U1",
        "server_url": _BASE,
        "on_success": on_success,
        "on_failure": on_failure,
        "poll_interval_seconds": 0.01,
    }
    mgr.await_enrollment_in_background(**kwargs)  # type: ignore[arg-type]
    first = mgr._login_polls[("T1", "U1", _BASE)]  # type: ignore[attr-defined]

    # Second run for the same key supersedes the first.
    mgr.await_enrollment_in_background(**kwargs)  # type: ignore[arg-type]
    second = mgr._login_polls[("T1", "U1", _BASE)]  # type: ignore[attr-defined]

    assert second is not first
    # The prior poll is cancelled; only one poll remains tracked for the key.
    await asyncio.sleep(0.05)
    assert first.cancelled()
    assert len([t for t in mgr._login_tasks if not t.done()]) == 1  # type: ignore[attr-defined]

    # The surviving poll still resolves normally when the token lands.
    await store.put("T1", "U1", _BASE, access_token="fresh", refresh_token="")
    await asyncio.wait_for(succeeded.wait(), timeout=1.0)


async def test_enrollment_poll_clears_map_slot_on_completion(tmp_path: Path) -> None:
    # A completed poll removes itself from _login_polls so a later re-run for the
    # same key isn't mistaken for a live poll (and doesn't leak the map entry).
    mgr, store = await _manager(tmp_path)

    succeeded = asyncio.Event()

    async def on_success() -> None:
        succeeded.set()

    async def on_failure(reason: str) -> None:
        return None

    mgr.await_enrollment_in_background(
        team_id="T1",
        user_id="U1",
        server_url=_BASE,
        on_success=on_success,
        on_failure=on_failure,
        poll_interval_seconds=0.01,
    )
    # Let the poll baseline the (absent) token BEFORE writing the fresh one —
    # otherwise, if `store.put` races ahead of the poll's first read (likely
    # under a loaded `-n` runner), the poll baselines "fresh" and never sees a
    # change, so on_success never fires and this test times out. The sibling
    # ``test_enrollment_fires_on_first_token_when_none_existed`` uses the same
    # ordering barrier.
    await asyncio.sleep(0.05)
    assert not succeeded.is_set()
    await store.put("T1", "U1", _BASE, access_token="fresh", refresh_token="")
    await asyncio.wait_for(succeeded.wait(), timeout=1.0)
    # Let the done-callback run.
    await asyncio.sleep(0.05)
    assert ("T1", "U1", _BASE) not in mgr._login_polls  # type: ignore[attr-defined]
