"""The manifest's ``auth`` block on a real OIDC ``create_app``.

``auth.mode == "oidc"`` promises the native loopback sign-in, so the
desktop can rely on ``POST /auth/native-token`` existing without a probe.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
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
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config(redirect_uri)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        manifest = await client.get("/.well-known/omnigent.json", headers={"Cookie": ""})
        exchange = await client.post("/auth/native-token", data={})

    assert manifest.status_code == 200
    assert manifest.json()["auth"] == {"mode": "oidc", "session_cookie": cookie}
    # The promised endpoint is mounted (a bad request, not a 404).
    assert exchange.status_code == 400
    assert exchange.json() == {"error": "invalid_request"}
