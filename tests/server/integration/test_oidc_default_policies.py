"""OIDC integration tests for the global (default) policies routes.

The default-policies router (``/v1/policies``) gates purely on the
mode-agnostic ``permission_store.is_admin`` — reads require auth, writes
require admin — so it works under OIDC exactly as under accounts. These
tests pin that end-to-end: a real ``create_app`` wired with an OIDC
provider + permission store + policy store, driven by presenting an OIDC
session JWT as a Bearer token (the same mechanism ``test_oidc_admin_users``
uses).

Unlike Members (read-only under OIDC — no password actions), Policies is
fully functional: an OIDC admin can list, create, toggle, and delete.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, mint_session_cookie
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

pytestmark = pytest.mark.asyncio

_TEST_SECRET = bytes.fromhex("aa" * 32)
_ADMIN = "admin@example.com"
_MEMBER = "member@example.com"


def _oidc_config() -> OIDCConfig:
    """Build a minimal GitHub-flavoured OIDCConfig for testing."""
    return OIDCConfig(
        issuer="https://github.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="read:user user:email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="github",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        jwks_uri=None,
        userinfo_endpoint="https://api.github.com/user",
        allow_invites=False,
    )


def _bearer(user_id: str) -> dict[str, str]:
    """Authorization header carrying an OIDC session JWT for *user_id*."""
    token = mint_session_cookie(
        user_id=user_id,
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="oidc",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def oidc_policy_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """A create_app instance with OIDC auth + permission store + policy store.

    Seeds an admin and a non-admin so the tests can drive both gating
    paths. Header/accounts routers aren't mounted (no login_url match
    needed) — the /v1/policies router only needs auth_provider +
    permission_store, which is exactly the OIDC wiring.
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(_ADMIN, is_admin=True)
    perm_store.ensure_user(_MEMBER)

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    auth_provider = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config())
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
        policy_store=SqlAlchemyPolicyStore(db_uri),
        permission_store=perm_store,
        auth_provider=auth_provider,
    )


@pytest_asyncio.fixture()
async def oidc_policy_client(
    oidc_policy_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the OIDC policy-enabled app."""
    transport = httpx.ASGITransport(app=oidc_policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _policy_payload(**overrides: object) -> dict:
    """Build a valid CreateDefaultPolicyRequest payload (URL type)."""
    base: dict = {
        "name": "oidc_url_policy",
        "type": "url",
        "handler": "https://example.com/policies/eval",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


# ── Admin: full CRUD works under OIDC ─────────────────────────────────


async def test_oidc_admin_can_crud_default_policies(
    oidc_policy_client: httpx.AsyncClient,
) -> None:
    """An OIDC admin can create, list, toggle, and delete global policies."""
    headers = _bearer(_ADMIN)

    # Create.
    create = await oidc_policy_client.post("/v1/policies", json=_policy_payload(), headers=headers)
    assert create.status_code == 200, create.text
    pid = create.json()["id"]
    assert pid.startswith("pol_")

    # List — the created policy is present.
    listing = await oidc_policy_client.get("/v1/policies", headers=headers)
    assert listing.status_code == 200
    assert pid in [p["id"] for p in listing.json()["data"]]

    # Toggle disabled.
    patch = await oidc_policy_client.patch(
        f"/v1/policies/{pid}", json={"enabled": False}, headers=headers
    )
    assert patch.status_code == 200
    assert patch.json()["enabled"] is False

    # Delete.
    delete = await oidc_policy_client.delete(f"/v1/policies/{pid}", headers=headers)
    assert delete.status_code == 200
    assert delete.json()["deleted"] is True


# ── Auth gating ───────────────────────────────────────────────────────


async def test_oidc_unauthenticated_cannot_read_policies(
    oidc_policy_client: httpx.AsyncClient,
) -> None:
    """A request with no session is rejected (401) — reads require auth."""
    resp = await oidc_policy_client.get("/v1/policies")
    assert resp.status_code == 401


async def test_oidc_non_admin_can_read_but_not_write(
    oidc_policy_client: httpx.AsyncClient,
) -> None:
    """A non-admin OIDC user can list policies but cannot create them (403)."""
    member = _bearer(_MEMBER)

    # Reads are allowed for any authenticated user.
    listing = await oidc_policy_client.get("/v1/policies", headers=member)
    assert listing.status_code == 200

    # Writes require admin — 403 for a member.
    create = await oidc_policy_client.post("/v1/policies", json=_policy_payload(), headers=member)
    assert create.status_code == 403


async def test_oidc_non_admin_cannot_delete(
    oidc_policy_client: httpx.AsyncClient,
) -> None:
    """A non-admin can't delete a policy an admin created (403)."""
    pid = (
        await oidc_policy_client.post(
            "/v1/policies", json=_policy_payload(), headers=_bearer(_ADMIN)
        )
    ).json()["id"]

    resp = await oidc_policy_client.delete(f"/v1/policies/{pid}", headers=_bearer(_MEMBER))
    assert resp.status_code == 403
