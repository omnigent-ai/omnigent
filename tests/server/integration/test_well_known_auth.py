"""The manifest's ``auth`` block on a real ``create_app`` per sign-in mode.

``auth.mode == "oidc"`` promises the native loopback sign-in, so the
desktop can rely on ``POST /auth/native-token`` existing without a probe.
``auth.native_redirect_uris`` tells the iOS app its redirect is accepted.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio


def _oidc_config(redirect_uri: str) -> OIDCConfig:
    return OIDCConfig(
        issuer="https://github.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri=redirect_uri,
        cookie_secret=bytes.fromhex("bb" * 32),
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


def _app(
    db_uri: str,
    tmp_path: Path,
    auth_provider: AuthProvider,
    account_store: SqlAlchemyAccountStore | None = None,
) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=auth_provider,
        account_store=account_store,
    )


@pytest.mark.parametrize(
    ("redirect_uri", "cookie"),
    [
        ("https://omni.example/auth/callback", "__Host-ap_session"),
        ("http://localhost:8000/auth/callback", "ap_session"),
    ],
)
async def test_oidc_manifest_names_mode_and_cookie(
    runtime_init: None, db_uri: str, tmp_path: Path, redirect_uri: str, cookie: str
) -> None:
    app = _app(
        db_uri,
        tmp_path,
        UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config(redirect_uri)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        manifest = await client.get("/.well-known/omnigent.json", headers={"Cookie": ""})
        exchange = await client.post("/auth/native-token", data={})

    assert manifest.status_code == 200
    assert manifest.json()["auth"] == {
        "mode": "oidc",
        "session_cookie": cookie,
        "native_redirect_uris": ["ai.omnigent.ios:/oauth/callback"],
    }
    # The promised endpoint is mounted (a bad request, not a 404).
    assert exchange.status_code == 400
    assert exchange.json() == {"error": "invalid_request"}


@pytest.mark.parametrize("mode", ["accounts", "header"])
async def test_non_oidc_manifest_lists_no_native_redirects(
    runtime_init: None, db_uri: str, tmp_path: Path, mode: str
) -> None:
    """Only OIDC runs the native sign-in, so other modes advertise no app redirect."""
    if mode == "accounts":
        accounts_config = AccountsConfig(
            cookie_secret=bytes.fromhex("cc" * 32),
            session_ttl_hours=8,
            base_url="https://omni.example",
            init_admin_password=None,
            invite_ttl_seconds=3600,
            magic_ttl_seconds=300,
        )
        app = _app(
            db_uri,
            tmp_path,
            UnifiedAuthProvider(source="accounts", accounts_config=accounts_config),
            SqlAlchemyAccountStore(db_uri),
        )
    else:
        app = _app(db_uri, tmp_path, UnifiedAuthProvider(source="header", local_single_user=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        manifest = await client.get("/.well-known/omnigent.json", headers={"Cookie": ""})

    assert manifest.status_code == 200
    auth = manifest.json()["auth"]
    assert auth["mode"] == mode
    assert auth["native_redirect_uris"] is None
