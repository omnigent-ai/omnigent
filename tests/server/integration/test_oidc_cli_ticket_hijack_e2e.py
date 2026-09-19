"""Regression test for the OIDC CLI-login ticket hijack.

Exercises the real ``/auth/*`` routes on a FastAPI app with OIDC auth,
mocking only the external IdP, in the style of
``tests/server/integration/test_oidc_auth_e2e.py``.

The vulnerability: nothing bound a CLI-login ticket to the client that
created it, and the browser callback fulfilled the ticket the instant
whoever signed in returned from the IdP. So an attacker could mint a
ticket, hand its ``/auth/login?ticket=T`` link to a victim, and — after
the victim signed in normally with no explicit approval step — poll
``/auth/cli-poll`` from a client that never authenticated and walk away
with the victim's session token (and refresh token, when a grant store
is wired).

The security invariant this guards: a passive victim sign-in must never
hand an unauthenticated third party the victim's credentials.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.routes.auth import create_auth_router
from tests.server.integration.oidc_fixtures import TEST_SIGNING_KEY, make_oidc_config

pytestmark = pytest.mark.asyncio

_VICTIM_EMAIL = "victim@example.com"


def _build_oidc_app(grant_store: DeviceGrantStore) -> httpx.ASGITransport:
    from fastapi import FastAPI

    config = make_oidc_config()
    auth_provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    admin_list = AdminList(Path("/tmp/nonexistent-admin-list.txt"))
    router = create_auth_router(
        auth_provider=auth_provider,
        permission_store=None,
        admin_list=admin_list,
        device_grant_store=grant_store,
    )
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    return httpx.ASGITransport(app=app)


def _mock_idp_for(email: str) -> AsyncMock:
    """Async-context-manager mock returning ``email`` from the GitHub IdP."""
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "gho_token", "token_type": "bearer"}
    token_resp.text = "{}"

    emails_resp = MagicMock()
    emails_resp.status_code = 200
    emails_resp.json.return_value = [{"email": email, "primary": True, "verified": True}]

    client = AsyncMock()
    client.post = AsyncMock(return_value=token_resp)
    client.get = AsyncMock(return_value=emails_resp)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_attacker_ticket_not_redeemable_after_passive_victim_signin(
    tmp_path: Path,
) -> None:
    """An unauthenticated poller must not receive a signed-in victim's tokens.

    Drives the full attack journey through the real routes; the single hard
    assertion is that the attacker's poll never yields the victim's
    credentials. Intermediate steps stay tolerant so a hardened build that
    refuses the unbound ticket earlier still satisfies the invariant.
    """
    grant_store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    transport = _build_oidc_app(grant_store)

    # The attacker's CLI and the victim's browser are independent clients
    # that share no cookies; the attacker never authenticates.
    async with (
        httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as attacker,
        httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as victim_browser,
    ):
        # 1. Attacker mints a ticket (no auth) and gets a genuine login link.
        create = await attacker.post("/auth/cli-login")
        if create.status_code != 200:
            # A hardened build refuses to mint an unbound ticket; the attack
            # is stopped before it starts.
            return
        ticket = create.json()["ticket"]
        login_url = create.json()["login_url"]

        # 2+3. Victim opens the emailed link; the state cookie carries the
        # ticket so the callback can associate it.
        redirect = await victim_browser.get(login_url)
        assert redirect.status_code == 302
        state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
        # The login redirect set ap_auth_state on the victim's client jar,
        # which is sent automatically on the callback below.
        assert victim_browser.cookies.get("ap_auth_state") is not None

        # 4. Victim returns from the IdP and signs in normally. There is no
        # explicit "approve this CLI login" step in this journey.
        with patch(
            "omnigent.server.routes.auth.httpx.AsyncClient",
            return_value=_mock_idp_for(_VICTIM_EMAIL),
        ):
            callback = await victim_browser.get(
                "/auth/callback",
                params={"code": "victim-auth-code", "state": state},
            )
        # Sign-in itself succeeds (success page or a redirect to consent);
        # the disposition of the ticket is what the poll below probes.
        assert callback.status_code < 400

        # 5. Attacker polls with only the ticket string, from a client that
        # never signed in.
        poll = await attacker.get(f"/auth/cli-poll?ticket={ticket}")

    if poll.status_code == 200:
        body = poll.json()
        stolen = jwt.decode(body["token"], TEST_SIGNING_KEY, algorithms=["HS256"])
        pytest.fail(
            "CLI-login ticket hijack: an unauthenticated poller redeemed the "
            "victim's ticket with no explicit consent step. cli-poll returned "
            f"200 with a session token for sub={stolen.get('sub')!r} "
            f"(refresh_token present: {'refresh_token' in body})."
        )

    assert poll.status_code != 200
