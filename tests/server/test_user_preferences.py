"""User-scoped, cross-device preferences API tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
    validate_preferences_envelope,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


class _HeaderAuthProvider(AuthProvider):
    """Resolve a test identity from ``X-Test-User``."""

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


class _AccountsAuthProvider(UnifiedAuthProvider):
    """Expose test-header identity while exercising accounts-mode guards."""

    def __init__(self) -> None:
        super().__init__(source="header", local_single_user=False)

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def _preferences_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts-preferences"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache-preferences",
        ),
        auth_provider=_HeaderAuthProvider(),
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )


def _deleted_account_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts-deleted-account"))
    auth_provider = _AccountsAuthProvider()
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache-deleted-account",
        ),
        auth_provider=auth_provider,
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )
    # create_app only mounts accounts-only auth routes when accounts mode is
    # active at construction. Switch after construction so this focused test
    # exercises the preferences guard without bootstrapping a real account.
    auth_provider._source = "accounts"
    return app


def test_store_preserves_uninitialized_vs_initialized_defaults(db_uri: str) -> None:
    """NULL and an explicit empty settings envelope have different meaning."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    assert store.get("alice@example.com") is None

    empty = {"version": 1, "settings": {}}
    assert store.initialize("alice@example.com", empty) == empty
    assert store.get("alice@example.com") == empty

    # First-device migration is idempotent and cannot overwrite an already
    # initialized account with stale localStorage from another device.
    stale = {"version": 1, "settings": {"usage_context": {"visible": False}}}
    assert store.initialize("alice@example.com", stale) == empty


def test_store_merges_one_namespace_and_keeps_users_isolated(db_uri: str) -> None:
    store = SqlAlchemyUserPreferencesStore(db_uri)
    first = store.patch_namespace(
        "alice@example.com",
        "keyboard_shortcuts",
        {"enabled": True, "actions": {"archive": "Alt+W"}},
    )
    assert first["settings"]["keyboard_shortcuts"]["enabled"] is True

    merged = store.patch_namespace(
        "alice@example.com",
        "keyboard_shortcuts",
        {"enabled": False},
    )
    assert merged["settings"]["keyboard_shortcuts"] == {
        "enabled": False,
        "actions": {"archive": "Alt+W"},
    }
    assert store.get("bob@example.com") is None

    removed = store.patch_namespace("alice@example.com", "keyboard_shortcuts", None)
    assert removed == {"version": 1, "settings": {}}

    compact = store.patch_namespace("alice@example.com", "context_indicator", "compact")
    assert compact["settings"]["context_indicator"] == "compact"


def test_store_can_refuse_to_recreate_a_deleted_account(db_uri: str) -> None:
    """Accounts-mode routes fail closed when a JWT outlives its user row."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    with pytest.raises(UserPreferencesUserNotFoundError):
        store.initialize(
            "deleted@example.com",
            {"version": 1, "settings": {}},
            create_if_missing=False,
        )
    with pytest.raises(UserPreferencesUserNotFoundError):
        store.patch_namespace(
            "deleted@example.com",
            "usage_context",
            {"version": 1},
            create_if_missing=False,
        )
    assert store.get("deleted@example.com") is None


def test_store_rejects_an_unpaired_unicode_surrogate_as_a_validation_error() -> None:
    with pytest.raises(UserPreferencesValidationError, match="valid UTF-8"):
        validate_preferences_envelope({"version": 1, "settings": {"usage_context": "\ud800"}})


@pytest.mark.asyncio
async def test_preferences_api_initializes_merges_and_returns_from_me(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "alice@example.com"}

        initial_me = await client.get("/v1/me", headers=headers)
        assert initial_me.status_code == 200
        assert initial_me.headers["cache-control"] == "private, no-store"
        assert initial_me.json() == {
            "user_id": "alice@example.com",
            "is_admin": False,
            "preferences": None,
        }

        initialized = await client.put(
            "/v1/me/preferences",
            headers=headers,
            json={
                "version": 1,
                "settings": {"session_navigation": {"activeHours": 12}},
            },
        )
        assert initialized.status_code == 200

        # Once initialized, PUT is idempotent and cannot replace the first
        # device's value with stale state from a second device.
        repeated = await client.put(
            "/v1/me/preferences",
            headers=headers,
            json={"version": 1, "settings": {}},
        )
        assert repeated.json() == initialized.json()

        patched = await client.patch(
            "/v1/me/preferences/session_navigation",
            headers=headers,
            json={"value": {"showMobileTitle": True}},
        )
        assert patched.status_code == 200
        assert patched.json()["settings"]["session_navigation"] == {
            "activeHours": 12,
            "showMobileTitle": True,
        }

        patched = await client.patch(
            "/v1/me/preferences/agent_badges",
            headers=headers,
            json={
                "value": {
                    "version": 1,
                    "enabled": False,
                    "entries": {
                        "agent-a": {
                            "label": "A",
                            "borderColor": "#123456",
                            "textColor": "#abcdef",
                        }
                    },
                }
            },
        )
        assert patched.status_code == 200
        assert patched.json()["settings"]["agent_badges"]["enabled"] is False
        assert "agent-a" in patched.json()["settings"]["agent_badges"]["entries"]

        synced_me = await client.get("/v1/me", headers=headers)
        assert synced_me.json()["preferences"] == patched.json()


@pytest.mark.asyncio
async def test_preferences_api_isolates_users_and_rejects_invalid_payloads(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        alice = {"x-test-user": "alice@example.com"}
        bob = {"x-test-user": "bob@example.com"}
        payload = {
            "version": 1,
            "settings": {"mobile_assistant": {"enabled": True}},
        }
        assert (
            await client.put("/v1/me/preferences", headers=alice, json=payload)
        ).status_code == 200

        bob_me = await client.get("/v1/me", headers=bob)
        assert bob_me.status_code == 200
        assert bob_me.json()["preferences"] is None

        unauthorized = await client.patch(
            "/v1/me/preferences/mobile_assistant",
            json={"value": {"enabled": False}},
        )
        assert unauthorized.status_code == 401

        unknown = await client.patch(
            "/v1/me/preferences/not_allowed",
            headers=alice,
            json={"value": {}},
        )
        assert unknown.status_code == 422

        oversized = await client.put(
            "/v1/me/preferences",
            headers=alice,
            json={
                "version": 1,
                "settings": {"usage_context": {"padding": "x" * (64 * 1024)}},
            },
        )
        assert oversized.status_code == 422

        raw_oversized = await client.put(
            "/v1/me/preferences",
            headers={**alice, "content-type": "application/json"},
            content=b'{"version":1,"settings":{"usage_context":{"padding":"'
            + (b"x" * (1024 * 1024))
            + b'"}}}',
        )
        assert raw_oversized.status_code == 413

        extra = await client.put(
            "/v1/me/preferences",
            headers=alice,
            json={"version": 1, "settings": {}, "unexpected": True},
        )
        assert extra.status_code == 422

        invalid_unicode = await client.patch(
            "/v1/me/preferences/usage_context",
            headers={**alice, "content-type": "application/json"},
            content=b'{"value":"\\ud800"}',
        )
        assert invalid_unicode.status_code == 422

        wrong_media_type = await client.put(
            "/v1/me/preferences",
            headers={**alice, "content-type": "text/plain"},
            content=b'{"version":1,"settings":{}}',
        )
        assert wrong_media_type.status_code == 415


@pytest.mark.asyncio
async def test_preferences_api_does_not_recreate_a_deleted_account(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _deleted_account_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/v1/me/preferences/usage_context",
            headers={"x-test-user": "deleted@example.com"},
            json={"value": {"visible": True}},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Account no longer exists"
    assert SqlAlchemyUserPreferencesStore(db_uri).get("deleted@example.com") is None


@pytest.mark.asyncio
async def test_preferences_api_rate_limits_writes_per_user(
    db_uri: str,
    runtime_init: None,
    tmp_path: Path,
) -> None:
    app = _preferences_app(db_uri, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-test-user": "rate-limited@example.com"}
        for index in range(120):
            response = await client.patch(
                "/v1/me/preferences/usage_context",
                headers=headers,
                json={"value": {"counter": index}},
            )
            assert response.status_code == 200

        limited = await client.patch(
            "/v1/me/preferences/usage_context",
            headers=headers,
            json={"value": {"counter": 120}},
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
