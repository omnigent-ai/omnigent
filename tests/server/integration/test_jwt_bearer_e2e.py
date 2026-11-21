"""End-to-end integration for the RFC 7523 JWT bearer grant.

Drives a production-shaped app in accounts mode over ``httpx.ASGITransport``.
The identity provider is in-process: assertions are signed with a local key
and the JWKS fetch is patched, so the test is network-free. Proves that an
exchanged token authenticates the delegated session APIs as its configured
principal, stays confined to the allowlist even after a cached success, and
that the grant is opt-in, vets its principals and refuses a bad configuration
at startup.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa

from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes.jwt_bearer import JWT_BEARER_GRANT_TYPE
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_client_credentials_e2e import _build_app, _login_grant_token

pytestmark = pytest.mark.asyncio

_ISSUER = "https://sso.example.com/realms/main"
_AUDIENCE = "omnigent-api"
_CLIENT = "ci-triage"
_PRINCIPAL = "ci-triage@ci.invalid"
_KID = "key-1"
_CONFINEMENT_PROBE = "/v1/me"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _assertion(key: rsa.RSAPrivateKey, **claims: object) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "azp": _CLIENT,
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": _KID})


def _configure(monkeypatch: pytest.MonkeyPatch, key: rsa.RSAPrivateKey) -> None:
    monkeypatch.setenv("OMNIGENT_JWT_BEARER_ISSUER", _ISSUER)
    monkeypatch.setenv("OMNIGENT_JWT_BEARER_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("OMNIGENT_JWT_BEARER_CLIENTS", f"{_CLIENT}={_PRINCIPAL}")
    monkeypatch.setenv("OMNIGENT_JWT_BEARER_JWKS_URI", f"{_ISSUER}/certs")
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    document = {"keys": [{**jwk, "kid": _KID, "use": "sig", "alg": "RS256"}]}
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: document)


async def _client_for(built: SimpleNamespace) -> AsyncIterator[SimpleNamespace]:
    from omnigent.db.utils import clear_engine_cache

    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(client=client, permission_store=built.permission_store)
    clear_engine_cache()


@pytest_asyncio.fixture
async def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> AsyncIterator[SimpleNamespace]:
    _configure(monkeypatch, signing_key)
    async for value in _client_for(_build_app(tmp_path, monkeypatch, machine_client=False)):
        yield value


@pytest_asyncio.fixture
async def unconfigured_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SimpleNamespace]:
    async for value in _client_for(_build_app(tmp_path, monkeypatch, machine_client=False)):
        yield value


async def _exchange(client: httpx.AsyncClient, assertion: str) -> httpx.Response:
    return await client.post(
        "/oauth/token", data={"grant_type": JWT_BEARER_GRANT_TYPE, "assertion": assertion}
    )


async def _token(env: SimpleNamespace, key: rsa.RSAPrivateKey) -> dict[str, str]:
    resp = await _exchange(env.client, _assertion(key))
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_default_deployment_does_not_serve_the_grant(
    unconfigured_env: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    resp = await _exchange(unconfigured_env.client, _assertion(signing_key))
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


async def test_exchanged_token_is_confined_to_the_delegated_apis(
    env: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    auth = await _token(env, signing_key)
    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200
    assert (await env.client.get("/v1/agents", headers=auth)).status_code == 200
    assert (await env.client.get(_CONFINEMENT_PROBE, headers=auth)).status_code in (401, 403)
    assert (await env.client.get("/auth/users", headers=auth)).status_code in (401, 403)
    # A success on an allowlisted path must not warm a cache entry that lets
    # the same token through elsewhere.
    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200
    assert (await env.client.get(_CONFINEMENT_PROBE, headers=auth)).status_code in (401, 403)

    login_auth = {"Authorization": f"Bearer {await _login_grant_token(env.client)}"}
    assert (await env.client.get(_CONFINEMENT_PROBE, headers=login_auth)).status_code == 200


async def test_raw_assertion_is_not_accepted_as_a_bearer(
    env: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    auth = {"Authorization": f"Bearer {_assertion(signing_key)}"}
    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 401


async def test_principal_owns_and_operates_its_own_session(
    env: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    auth = await _token(env, signing_key)
    created = await env.client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": ("agent.tar.gz", build_agent_bundle(name="jwt-agent"), "application/gzip")
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    grant = env.permission_store.get(_PRINCIPAL, session_id)
    assert grant is not None and grant.level >= LEVEL_OWNER

    posted = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "interrupt", "data": {}},
        headers=auth,
    )
    assert posted.status_code not in (401, 403) and posted.status_code < 500, posted.text
    deleted = await env.client.delete(f"/v1/sessions/{session_id}", headers=auth)
    assert deleted.status_code == 200, deleted.text


async def test_promoted_principal_stops_exchanging(
    env: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    await _token(env, signing_key)
    env.permission_store.ensure_user(_PRINCIPAL)
    env.permission_store.set_admin(_PRINCIPAL, True)
    resp = await _exchange(env.client, _assertion(signing_key))
    assert resp.status_code == 400
    assert resp.json()["error"] == "unauthorized_client"


async def test_unlisted_client_is_refused(
    env: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    resp = await _exchange(env.client, _assertion(signing_key, azp="someone-else"))
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_partial_configuration_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_JWT_BEARER_ISSUER", _ISSUER)
    with pytest.raises(RuntimeError, match="must all be set"):
        _build_app(tmp_path, monkeypatch, machine_client=False)
