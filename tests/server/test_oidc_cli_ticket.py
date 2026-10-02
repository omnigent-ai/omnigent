"""Security regression tests for the OIDC CLI login ticket flow."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi import FastAPI

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes.auth import create_auth_router
from tests.server.integration.oidc_fixtures import TEST_SIGNING_KEY, make_oidc_config
from tests.server.integration.test_oidc_auth_e2e import (
    _build_oidc_app,
    _mint_state_cookie,
    _mock_httpx_client_for_github,
    _pkce_pair,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _authenticated_origin_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)


def _build_app(
    store: DeviceGrantStore | None = None, *, base_path: str = "", redirect_uri: str | None = None
) -> httpx.ASGITransport:
    config = make_oidc_config()
    if redirect_uri is not None:
        config = replace(config, redirect_uri=redirect_uri)
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    router = create_auth_router(
        auth_provider=provider,
        permission_store=None,
        admin_list=AdminList(Path("/tmp/nonexistent-admin-list.txt")),
        device_grant_store=store,
    )
    app = FastAPI()
    app.state.base_path = base_path
    app.include_router(router, prefix=f"{base_path}/auth")
    return httpx.ASGITransport(app=app)


async def _create_ticket(client: httpx.AsyncClient, base_path: str = "") -> tuple[str, str]:
    verifier, challenge = _pkce_pair()
    response = await client.post(
        f"{base_path}/auth/cli-login",
        json={"code_challenge": challenge, "code_challenge_method": "S256"},
    )
    assert response.status_code == 200
    return response.json()["ticket"], verifier


async def _complete_callback(
    client: httpx.AsyncClient, ticket: str, *, state: str = "state", base_path: str = ""
) -> httpx.Response:
    state_cookie = _mint_state_cookie(state, ticket=ticket)
    cookie_name = "__Host-ap_auth_state" if client.base_url.scheme == "https" else "ap_auth_state"
    mock_cm = _mock_httpx_client_for_github()
    with patch("omnigent.server.routes.auth.httpx.AsyncClient", return_value=mock_cm):
        return await client.get(
            f"{base_path}/auth/callback",
            params={"code": "auth-code", "state": state},
            cookies={cookie_name: state_cookie},
        )


async def test_cli_ticket_requires_browser_consent_and_issues_grant(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    transport = _build_app(store)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        callback = await _complete_callback(client, ticket)
        assert callback.status_code == 302
        assert callback.headers["location"] == f"/auth/cli-consent?ticket={ticket}"
        assert "ap_session" in callback.cookies
        browser_claims = jwt.decode(
            callback.cookies["ap_session"], TEST_SIGNING_KEY, algorithms=["HS256"]
        )
        assert browser_claims["cli_login_ticket"] == ticket

        pending = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert pending.status_code == 202

        consent = await client.get(f"/auth/cli-consent?ticket={ticket}")
        assert consent.status_code == 200
        assert re.search(r"Code: [A-Z2-9]{4}-[A-Z2-9]{4}", consent.text)
        assert "alice@example.com" in consent.text
        assert "Authorize sign-in" in consent.text
        assert "For CLI or Slack sign-in" in consent.text

        approved = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert approved.status_code == 200
        assert "Approved" in approved.text

        fulfilled = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert fulfilled.status_code == 200
        body = fulfilled.json()
        payload = jwt.decode(body["token"], TEST_SIGNING_KEY, algorithms=["HS256"])
        assert payload["sub"] == "alice@example.com"
        assert "cli_login_ticket" not in payload
        assert body["user_id"] == "alice@example.com"
        assert body["refresh_token"]


async def test_cli_ticket_rejects_wrong_verifier_without_consuming() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        approved = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert approved.status_code == 200

        wrong = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={'b' * 64}")
        assert wrong.status_code == 403
        correct = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert correct.status_code == 200


async def test_cli_ticket_poll_requires_matching_verifier() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ticket, verifier = await _create_ticket(client)
        missing = await client.get(f"/auth/cli-poll?ticket={ticket}")
        assert missing.status_code == 400
        assert "Upgrade" in missing.json()["error"]
        wrong = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={'b' * 64}")
        assert wrong.status_code == 403
        assert "does not match" in wrong.json()["error"]
        assert (
            await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        ).status_code == 202


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("missing_ticket", 410),
        ("unknown_ticket", 410),
        ("upgrade", 410),
        ("expired", 410),
        ("missing_verifier", 400),
        ("malformed_verifier", 400),
        ("wrong_verifier", 403),
        ("pending", 202),
        ("approve", 200),
        ("deny", 410),
    ],
)
async def test_cli_poll_responses_cannot_be_cached(outcome: str, status: int) -> None:
    async with httpx.AsyncClient(
        transport=_build_app(), base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        params = {"ticket": ticket}
        headers = {"X-Omnigent-Code-Verifier": verifier}
        if outcome == "missing_ticket":
            params = {}
        elif outcome == "unknown_ticket":
            params["ticket"] = "unknown"
        elif outcome == "upgrade":
            params["ticket"] = (await client.post("/auth/cli-login")).json()["ticket"]
        elif outcome == "missing_verifier":
            headers = {}
        elif outcome == "malformed_verifier":
            headers["X-Omnigent-Code-Verifier"] = "invalid"
        elif outcome == "wrong_verifier":
            headers["X-Omnigent-Code-Verifier"] = "b" * 64
        elif outcome in ("approve", "deny"):
            await _complete_callback(client, ticket)
            await client.post(
                f"/auth/cli-{outcome}", data=params, headers={"Origin": "http://test"}
            )
        now = time.time() + (301 if outcome == "expired" else 0)
        with patch("omnigent.server.routes.auth.time.time", return_value=now):
            response = await client.get("/auth/cli-poll", params=params, headers=headers)
        assert response.status_code == status
        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("Pragma") == "no-cache"


async def test_cli_poll_shared_cache_cannot_replay_credentials(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    async with httpx.AsyncClient(
        transport=_build_app(store), base_url="http://test", follow_redirects=False
    ) as upstream:
        ticket, verifier = await _create_ticket(upstream)
        await _complete_callback(upstream, ticket)
        await upstream.post(
            "/auth/cli-approve", data={"ticket": ticket}, headers={"Origin": "http://test"}
        )
        cache: dict[str, httpx.Response] = {}

        async def caching_proxy(request: httpx.Request) -> httpx.Response:
            key = str(request.url)
            if key in cache:
                return cache[key]
            response = await upstream.send(request)
            if response.status_code == 200 and "no-store" not in response.headers.get(
                "Cache-Control", ""
            ):
                cache[key] = response
            return response

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(caching_proxy), base_url="http://test"
        ) as proxy:
            url = f"/auth/cli-poll?ticket={ticket}"
            redeemed = await proxy.get(url, headers={"X-Omnigent-Code-Verifier": verifier})
            assert redeemed.status_code == 200
            assert redeemed.json()["token"]
            assert redeemed.json()["refresh_token"]
            replay = await proxy.get(url)
            assert replay.status_code == 410
            assert "token" not in replay.json()
            assert "refresh_token" not in replay.json()


async def test_cli_ticket_deny_is_terminal() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        denied = await client.post(
            "/auth/cli-deny",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert denied.status_code == 200
        first = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        second = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert first.status_code == 410
        assert second.status_code == 410


async def test_cli_approve_requires_origin_and_session() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, _ = await _create_ticket(client)
        await _complete_callback(client, ticket)
        no_origin = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": ""},
        )
        assert no_origin.status_code == 403

        client.cookies.delete("ap_session")
        no_session = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert no_session.status_code == 401


@pytest.mark.parametrize("endpoint", ["cli-approve", "cli-deny"])
@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize(
    "origin", ["https://evil.example", "http://sub.test", "null", "omnigent://internal"]
)
async def test_cli_consent_rejects_foreign_origins(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, origin: str, allowlisted: bool
) -> None:
    if allowlisted:
        monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", origin)
    else:
        monkeypatch.delenv("OMNIGENT_WS_ALLOWED_ORIGINS", raising=False)
    async with httpx.AsyncClient(
        transport=_build_app(), base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        response = await client.post(
            f"/auth/{endpoint}", data={"ticket": ticket}, headers={"Origin": origin}
        )
        assert response.status_code == 403
        assert (
            await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        ).status_code == 202


async def test_cli_consent_accepts_configured_public_origin_behind_proxy() -> None:
    async with httpx.AsyncClient(
        transport=_build_app(), base_url="http://internal-proxy", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        response = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://localhost:8000"},
        )
        assert response.status_code == 200
        assert (
            await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        ).status_code == 200


@pytest.mark.parametrize("decision", ["approve", "deny"])
@pytest.mark.parametrize(
    ("redirect_uri", "origin"),
    [
        ("https://example.com:443/auth/callback", "https://example.com"),
        ("http://example.com:80/auth/callback", "http://example.com"),
        ("https://example.com:8443/auth/callback", "https://example.com:8443"),
        ("https://EXAMPLE.com/auth/callback", "https://example.com"),
    ],
)
async def test_cli_consent_canonicalizes_public_origin_behind_proxy(
    decision: str, redirect_uri: str, origin: str
) -> None:
    async with httpx.AsyncClient(
        transport=_build_app(redirect_uri=redirect_uri),
        base_url=f"{urlsplit(redirect_uri).scheme}://internal-proxy",
        follow_redirects=False,
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        response = await client.post(
            f"/auth/cli-{decision}", data={"ticket": ticket}, headers={"Origin": origin}
        )
        assert response.status_code == 200
        poll = await client.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
            headers={"X-Omnigent-Code-Verifier": verifier},
        )
        assert poll.status_code == (200 if decision == "approve" else 410)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://example.com",
        "https://example.com:444",
        "https://example.com/path",
        "https://user@example.com",
        "https://example.com:99999",
    ],
)
async def test_cli_consent_origin_normalization_remains_strict(origin: str) -> None:
    async with httpx.AsyncClient(
        transport=_build_app(redirect_uri="https://example.com:443/auth/callback"),
        base_url="https://internal-proxy",
        follow_redirects=False,
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        response = await client.post(
            "/auth/cli-approve", data={"ticket": ticket}, headers={"Origin": origin}
        )
        assert response.status_code == 403
        pending = await client.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
            headers={"X-Omnigent-Code-Verifier": verifier},
        )
        assert pending.status_code == 202


async def test_cli_approve_rejects_stale_session() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        with patch("omnigent.server.oidc.time.time", return_value=time.time() - 10):
            stale = mint_session_cookie(
                "alice@example.com", TEST_SIGNING_KEY, 8, "github", cli_login_ticket=ticket
            )
        client.cookies.set("ap_session", stale)
        approved = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert approved.status_code == 200
        assert "Sign in again to approve this login" in approved.text
        assert (
            await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        ).status_code == 202


@pytest.mark.parametrize("credential_kind", ["runner", "session"])
async def test_cli_approve_requires_ticket_login_provenance(
    credential_kind: str, tmp_path: Path
) -> None:
    config = make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    async with httpx.AsyncClient(
        transport=_build_app(store), base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        if credential_kind == "runner":
            token = provider.mint_runner_token("alice@example.com", ttl_seconds=1800)
            assert token is not None
        else:
            token = mint_session_cookie("alice@example.com", TEST_SIGNING_KEY, 8, "github")
        client.cookies.set("ap_session", token)
        with patch("omnigent.server.routes.auth.issue_login_grant") as issue_grant:
            approval = await client.post(
                "/auth/cli-approve", data={"ticket": ticket}, headers={"Origin": "http://test"}
            )
            assert "<h1>Approved</h1>" not in approval.text
            pending = await client.get(
                "/auth/cli-poll", params={"ticket": ticket, "code_verifier": verifier}
            )
            assert pending.status_code == 202
            issue_grant.assert_not_called()
        consent = await client.get("/auth/cli-consent", params={"ticket": ticket})
        assert consent.status_code == 302
        assert "reauth=1" in consent.headers["location"]


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_cli_consent_and_decisions_disallow_framing(decision: str) -> None:
    async with httpx.AsyncClient(
        transport=_build_app(), base_url="http://test", follow_redirects=False
    ) as client:
        ticket, _ = await _create_ticket(client)
        await _complete_callback(client, ticket)
        consent = await client.get("/auth/cli-consent", params={"ticket": ticket})
        response = await client.post(
            f"/auth/cli-{decision}", data={"ticket": ticket}, headers={"Origin": "http://test"}
        )
        invalid = await client.get("/auth/cli-consent", params={"ticket": "missing"})
        for page in (consent, response, invalid):
            assert page.headers.get("content-security-policy") == "frame-ancestors 'none'"
            assert page.headers.get("x-frame-options") == "DENY"
            assert page.headers.get("cache-control") == "no-store"


async def test_cli_poll_accepts_header_proof_and_rejects_conflicting_query_proof() -> None:
    async with httpx.AsyncClient(
        transport=_build_app(), base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        await client.post(
            "/auth/cli-approve", data={"ticket": ticket}, headers={"Origin": "http://test"}
        )
        rejected = await client.get(
            "/auth/cli-poll",
            params={"ticket": ticket, "code_verifier": verifier},
            headers={"X-Omnigent-Code-Verifier": "b" * 64},
        )
        assert rejected.status_code == 403
        approved = await client.get(
            "/auth/cli-poll",
            params={"ticket": ticket},
            headers={"X-Omnigent-Code-Verifier": verifier},
        )
        assert approved.status_code == 200


async def test_cli_consent_unauthenticated_bounces_to_login() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, _ = await _create_ticket(client)
        response = await client.get(f"/auth/cli-consent?ticket={ticket}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/auth/login?")
    assert "reauth=1" in response.headers["location"]


async def test_cli_login_response_contains_consent_code() -> None:
    transport = _build_oidc_app()
    _, challenge = _pkce_pair()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/cli-login",
            json={"code_challenge": challenge, "code_challenge_method": "S256"},
        )
    body = response.json()
    assert "reauth=1" in body["login_url"]
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", body["user_code"])


async def test_legacy_login_opens_upgrade_instructions_and_stops_polling() -> None:
    """Old clients open login_url on 200 and stop polling on 410."""
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post("/auth/cli-login")
        assert response.status_code == 200
        body = response.json()
        page = await client.get(body["login_url"])
        assert page.status_code == 200
        assert "Update Omnigent to sign in" in page.text
        assert "omni upgrade" in page.text
        assert "iOS or Android" in page.text
        assert "Slack integration" in page.text
        assert "<form" not in page.text
        assert "set-cookie" not in page.headers
        assert page.headers["cache-control"] == "no-store"
        poll = await client.get("/auth/cli-poll", params={"ticket": body["ticket"]})
        assert poll.status_code == 410
        assert "omni upgrade" in poll.json()["error"]


async def test_legacy_upgrade_notice_cannot_issue_credentials(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    async with httpx.AsyncClient(
        transport=_build_app(store), base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post("/auth/cli-login")
        ticket = response.json()["ticket"]
        with patch("omnigent.server.routes.auth.issue_login_grant") as issue_grant:
            await _complete_callback(client, ticket)
            consent = await client.get("/auth/cli-consent", params={"ticket": ticket})
            assert "invalid or has expired" in consent.text
            approval = await client.post(
                "/auth/cli-approve", data={"ticket": ticket}, headers={"Origin": "http://test"}
            )
            assert "invalid or has expired" in approval.text
            poll = await client.get("/auth/cli-poll", params={"ticket": ticket})
            assert poll.status_code == 410
            assert "token" not in poll.json()
            assert "refresh_token" not in poll.json()
            issue_grant.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"null",
        b"[]",
        b"{}",
        b"[" * 1500 + b"]" * 1500,
        b'{"code_challenge": "short"}',
        b'{"code_challenge_method": "plain"}',
    ],
)
async def test_malformed_pkce_is_not_treated_as_a_legacy_request(body: bytes) -> None:
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post(
            "/auth/cli-login", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert "login_url" not in response.json()


@pytest.mark.parametrize("verifier", ["é" * 43, "", "a" * 42, "a" * 129, "!" * 43])
async def test_cli_poll_rejects_malformed_verifier_without_consuming(verifier: str) -> None:
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        ticket, valid_verifier = await _create_ticket(client)
        response = await client.get(
            "/auth/cli-poll", params={"ticket": ticket, "code_verifier": verifier}
        )
        assert response.status_code == 400
        pending = await client.get(
            "/auth/cli-poll", params={"ticket": ticket, "code_verifier": valid_verifier}
        )
        assert pending.status_code == 202


@pytest.mark.parametrize("challenge", ["a" * 42, "a" * 44, "a" * 128, "!" * 43])
async def test_cli_login_rejects_invalid_s256_challenge(challenge: str) -> None:
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post(
            "/auth/cli-login", json={"code_challenge": challenge, "code_challenge_method": "S256"}
        )
        assert response.status_code == 400
        assert "ticket" not in response.json()


async def test_cli_login_rejects_oversized_body() -> None:
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post("/auth/cli-login", content=b"x" * 4097)
        assert response.status_code == 413


async def test_cli_login_stops_reading_oversized_chunked_body() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 2048
        yield b"x" * 2049
        raise AssertionError("Login endpoint read past its body limit")

    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post("/auth/cli-login", content=chunks())
        assert response.status_code == 413


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_cli_consent_flow_under_base_path(decision: str) -> None:
    base_path = "/proxy/6767"
    async with httpx.AsyncClient(
        transport=_build_app(base_path=base_path), base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client, base_path)
        consent_url = f"{base_path}/auth/cli-consent?ticket={ticket}"
        unauthenticated = await client.get(consent_url)
        login_url = unauthenticated.headers["location"]
        assert urlsplit(login_url).path == f"{base_path}/auth/login"
        assert parse_qs(urlsplit(login_url).query)["return_to"] == [consent_url]

        callback = await _complete_callback(client, ticket, base_path=base_path)
        assert callback.headers["location"] == consent_url
        consent = await client.get(consent_url)
        action = f"{base_path}/auth/cli-{decision}"
        assert f'action="{action}"' in consent.text
        response = await client.post(
            action, data={"ticket": ticket}, headers={"Origin": "http://test"}
        )
        assert response.status_code == 200
        poll = await client.get(
            f"{base_path}/auth/cli-poll", params={"ticket": ticket, "code_verifier": verifier}
        )
        assert poll.status_code == (200 if decision == "approve" else 410)

        legacy = await client.post(f"{base_path}/auth/cli-login")
        upgrade = await client.get(base_path + legacy.json()["login_url"])
        assert upgrade.status_code == 200
        assert "Update Omnigent to sign in" in upgrade.text
