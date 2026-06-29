"""Two-user integration tests for the per-user secret vault routes (#5).

Drives the real ``/v1/credentials`` router through the real
:class:`~omnigent.server.auth.UnifiedAuthProvider` (header mode),
:class:`~omnigent.stores.permission_store.sqlalchemy_store.SqlAlchemyPermissionStore`,
:class:`~omnigent.stores.user_credential_store.sqlalchemy_store.SqlAlchemyUserCredentialStore`,
and the Fernet vault, with two distinct ``X-Forwarded-Email`` identities. This
is the security heart of #5 in a *shared* multi-user server: each collaborator
stores, lists, resolves, and deletes only their own secrets; the same logical
name held by two users is namespaced per owner; listings never echo the secret;
values are encrypted at rest; and one user can never reach another's secret —
not over the wire and not through the runtime accessor that tool execution uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime import init as init_runtime
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.caps import RuntimeCaps
from omnigent.runtime.credentials import resolve_user_credential_env, resolve_user_secret
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.credentials import create_credentials_router
from omnigent.server.secret_vault import decrypt_secret, load_or_create_vault_key
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.user_credential_store.sqlalchemy_store import SqlAlchemyUserCredentialStore

pytestmark = pytest.mark.asyncio

# Two distinct collaborators sharing one server.
ALICE = "alice@example.com"
BOB = "bob@example.com"


def _hdr(email: str) -> dict[str, str]:
    """Build the trusted-proxy identity header for ``email``."""
    return {"X-Forwarded-Email": email}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def vault_key(tmp_path: Path) -> bytes:
    """A real, persisted Fernet vault key (same path shape as production)."""
    return load_or_create_vault_key(tmp_path / "secret_vault.key")


@pytest.fixture()
def cred_store(db_uri: str) -> SqlAlchemyUserCredentialStore:
    """The real per-user credential store on the per-test migrated DB."""
    return SqlAlchemyUserCredentialStore(db_uri)


@pytest.fixture()
def vault_app(
    db_uri: str,
    tmp_path: Path,
    vault_key: bytes,
    cred_store: SqlAlchemyUserCredentialStore,
) -> FastAPI:
    """Real multi-user app with the credentials router mounted, vault wired.

    Initializes the runtime with the vault key + credential store so the
    route's ``get_caps().vault_key`` and the ``resolve_user_secret`` accessor
    resolve exactly as they do under ``omnigent serve``.

    :param db_uri: Per-test SQLite URI (Alembic-migrated, so the
        ``user_credentials`` table exists).
    :param tmp_path: Pytest temp dir for artifacts / vault key / cache.
    :param vault_key: The server-held Fernet key.
    :param cred_store: The per-user credential store.
    :returns: A :class:`FastAPI` app in header-auth multi-user mode.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        user_credential_store=cred_store,
        caps=RuntimeCaps(vault_key=vault_key),
    )
    auth_provider = UnifiedAuthProvider(source="header", local_single_user=False)
    permission_store = SqlAlchemyPermissionStore(db_uri)
    return create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        permission_store=permission_store,
        auth_provider=auth_provider,
        extra_routers=[
            (
                create_credentials_router(cred_store, auth_provider, permission_store),
                "/v1",
                ["credentials"],
            ),
        ],
    )


@pytest_asyncio.fixture()
async def client(vault_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client wired to the real vault app."""
    transport = httpx.ASGITransport(app=vault_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Auth gate ─────────────────────────────────────────────────────────────────


async def test_anonymous_request_is_rejected(client: httpx.AsyncClient) -> None:
    """In multi-user mode the vault fails closed on a missing identity header."""
    resp = await client.get("/v1/credentials")
    assert resp.status_code == 401


# ── Store returns metadata only ───────────────────────────────────────────────


async def test_store_returns_metadata_never_the_secret(client: httpx.AsyncClient) -> None:
    """PUT echoes credential metadata and never the secret value back."""
    resp = await client.put(
        "/v1/credentials/github",
        json={"secret": "alice-PAT-xyz"},
        headers=_hdr(ALICE),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "credential"
    assert body["name"] == "github"
    assert body["id"].startswith("cred_")
    assert body["created_at"] > 0
    # The secret value must never appear in any response payload.
    assert "secret" not in body
    assert "alice-PAT-xyz" not in resp.text


# ── Two-user isolation ────────────────────────────────────────────────────────


async def test_same_name_is_namespaced_per_user(client: httpx.AsyncClient) -> None:
    """Alice and Bob each hold their own ``github`` secret; listings don't cross.

    Both store a credential under the identical logical name; each sees exactly
    one entry (their own) and never the other's — the core shared-session
    isolation property.
    """
    await client.put("/v1/credentials/github", json={"secret": "alice-PAT"}, headers=_hdr(ALICE))
    await client.put("/v1/credentials/github", json={"secret": "bob-PAT"}, headers=_hdr(BOB))

    alice_list = (await client.get("/v1/credentials", headers=_hdr(ALICE))).json()["data"]
    bob_list = (await client.get("/v1/credentials", headers=_hdr(BOB))).json()["data"]

    assert [c["name"] for c in alice_list] == ["github"]
    assert [c["name"] for c in bob_list] == ["github"]
    # Distinct rows (distinct ids) despite the shared name — separate owners.
    assert alice_list[0]["id"] != bob_list[0]["id"]


async def test_resolve_user_secret_is_per_acting_user(client: httpx.AsyncClient) -> None:
    """The runtime accessor tools call resolves each acting user's own secret.

    This is the exact call a collaborator's tool action makes
    (``resolve_user_secret(ctx.user_id, name)``) — it must return *that* user's
    value, proving a shared-session tool runs under the actor's credentials.
    """
    await client.put("/v1/credentials/github", json={"secret": "alice-PAT"}, headers=_hdr(ALICE))
    await client.put("/v1/credentials/github", json={"secret": "bob-PAT"}, headers=_hdr(BOB))

    assert resolve_user_secret(ALICE, "github") == "alice-PAT"
    assert resolve_user_secret(BOB, "github") == "bob-PAT"
    # An identity with no such secret resolves to None (caller falls back to
    # ambient creds) — never to another user's value.
    assert resolve_user_secret("carol@example.com", "github") is None


async def test_resolve_credential_env_maps_per_acting_user(client: httpx.AsyncClient) -> None:
    """Server-side resolve+map (pushed to the runner) is per-user and mapped.

    This is what the turn dispatch attaches as ``credential_env`` for the
    actor: Alice's github token becomes GITHUB_TOKEN/GH_TOKEN; Bob's aws keys
    become AWS_*; neither leaks into the other.
    """
    await client.put("/v1/credentials/github", json={"secret": "ghp_alice"}, headers=_hdr(ALICE))
    await client.put(
        "/v1/credentials/aws_access_key_id", json={"secret": "AKIA_bob"}, headers=_hdr(BOB)
    )
    await client.put(
        "/v1/credentials/aws_secret_access_key", json={"secret": "bobsecret"}, headers=_hdr(BOB)
    )

    alice_env = resolve_user_credential_env(ALICE)
    bob_env = resolve_user_credential_env(BOB)

    assert alice_env == {"GITHUB_TOKEN": "ghp_alice", "GH_TOKEN": "ghp_alice"}
    assert bob_env == {"AWS_ACCESS_KEY_ID": "AKIA_bob", "AWS_SECRET_ACCESS_KEY": "bobsecret"}
    # No cross-contamination between collaborators.
    assert "GITHUB_TOKEN" not in bob_env
    assert "AWS_ACCESS_KEY_ID" not in alice_env


async def test_resolve_credential_env_empty_for_unknown_user(client: httpx.AsyncClient) -> None:
    """A user with nothing stored yields an empty overlay (the common case)."""
    assert resolve_user_credential_env("nobody@example.com") == {}


async def test_secret_is_encrypted_at_rest(
    client: httpx.AsyncClient,
    cred_store: SqlAlchemyUserCredentialStore,
    vault_key: bytes,
) -> None:
    """Stored secrets are Fernet ciphertext on disk, not plaintext."""
    await client.put("/v1/credentials/github", json={"secret": "alice-PAT"}, headers=_hdr(ALICE))
    encrypted = cred_store.get_encrypted(ALICE, "github")
    assert encrypted is not None
    assert encrypted != "alice-PAT"
    assert "alice-PAT" not in encrypted
    # And it decrypts back to the original under the server vault key.
    assert decrypt_secret(vault_key, encrypted) == "alice-PAT"


async def test_delete_is_scoped_to_the_acting_user(client: httpx.AsyncClient) -> None:
    """Alice deleting her secret leaves Bob's identically-named one intact."""
    await client.put("/v1/credentials/github", json={"secret": "alice-PAT"}, headers=_hdr(ALICE))
    await client.put("/v1/credentials/github", json={"secret": "bob-PAT"}, headers=_hdr(BOB))

    deleted = await client.delete("/v1/credentials/github", headers=_hdr(ALICE))
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    # Alice's vault is now empty; Bob is untouched and still resolvable.
    assert (await client.get("/v1/credentials", headers=_hdr(ALICE))).json()["data"] == []
    bob_list = (await client.get("/v1/credentials", headers=_hdr(BOB))).json()["data"]
    assert [c["name"] for c in bob_list] == ["github"]
    assert resolve_user_secret(BOB, "github") == "bob-PAT"


async def test_overwrite_replaces_only_the_callers_secret(client: httpx.AsyncClient) -> None:
    """A repeat PUT under the same name rotates the caller's secret in place."""
    await client.put("/v1/credentials/github", json={"secret": "alice-old"}, headers=_hdr(ALICE))
    await client.put("/v1/credentials/github", json={"secret": "bob-PAT"}, headers=_hdr(BOB))
    await client.put("/v1/credentials/github", json={"secret": "alice-new"}, headers=_hdr(ALICE))

    # Alice still has a single entry, now holding the rotated value; Bob's
    # secret is unaffected by Alice's rotation.
    alice_list = (await client.get("/v1/credentials", headers=_hdr(ALICE))).json()["data"]
    assert len(alice_list) == 1
    assert resolve_user_secret(ALICE, "github") == "alice-new"
    assert resolve_user_secret(BOB, "github") == "bob-PAT"
