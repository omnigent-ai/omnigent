"""Shared fixtures for control-plane tests.

These exercise the real control-plane components against a real upstream
``create_app`` backed by an on-disk SQLite DB (the same path production
uses, via ``get_or_create_engine``). No stubs — the tests verify the
actual wiring, the enforcement middleware, and the stores end to end.

Identity is injected per request via the ``X-Forwarded-Email`` header
(Databricks Apps header mode). Group membership is injected via the
control plane's static-override hook (:func:`control_plane.identity.set_group_overrides`)
so no Databricks SCIM call is made.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import control_plane.identity as cp_identity
from control_plane.config import ControlPlaneConfig
from control_plane.wiring import attach_control_plane

# Force strict multi-user header posture for the whole module — a missing
# X-Forwarded-Email must 401, not resolve to the "local" sentinel.
os.environ.setdefault("OMNIGENT_AUTH_PROVIDER", "header")


@pytest.fixture
def cp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the three-tier role policy for tests.

    admin user: ``admin@db.com``; admin group: ``platform-admins``;
    contributor group: ``gtm-contributors``. Groups are resolved via the
    static override hook (set per-test), so ``groups_enabled`` is on.
    """
    monkeypatch.setenv("OMNIGENT_CP_ADMIN_USERS", "admin@db.com")
    monkeypatch.setenv("OMNIGENT_CP_ADMIN_GROUPS", "platform-admins")
    monkeypatch.setenv("OMNIGENT_CP_CONTRIBUTOR_GROUPS", "gtm-contributors")
    # Pin the use-only consumer-upload policy so the default-deny tests assert
    # against a known policy, not the developer's ambient shell env.
    monkeypatch.setenv("OMNIGENT_CP_CONSUMER_UPLOAD", "deny")


@pytest.fixture
def group_map() -> Iterator[dict[str, list[str]]]:
    """Mutable email→groups map installed into the identity resolver.

    Tests mutate the dict then call :func:`apply_groups` (or the fixture
    re-applies on teardown reset). Cleared after each test.
    """
    mapping: dict[str, list[str]] = {}
    cp_identity.set_group_overrides(mapping)
    yield mapping
    cp_identity.set_group_overrides({})
    cp_identity.clear_cache()


@pytest.fixture
def cp_app(
    cp_env: None,
    group_map: dict[str, list[str]],
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """Build the upstream app + attach the control plane, on a temp DB.

    Mirrors the production wiring in ``deploy/databricks/src/app.py``:
    ``create_app(...)`` then ``attach_control_plane(...)``.

    :param cp_env: Role/group env policy.
    :param group_map: The (initially empty) group-override map.
    :param db_uri: Per-test SQLite URI (root conftest fixture).
    :param tmp_path: Per-test temp dir.
    :returns: The wired FastAPI app.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    pytest.importorskip("omnigent.stores.agent_store.sqlalchemy_store")
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    permission_store = SqlAlchemyPermissionStore(db_uri)
    auth_provider = UnifiedAuthProvider(source="header", local_single_user=False)

    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=permission_store,
        auth_provider=auth_provider,
    )
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    attach_control_plane(
        app,
        db_uri=db_uri,
        agent_store=agent_store,
        conversation_store=conversation_store,
        permission_store=permission_store,
        auth_provider=auth_provider,
        config=ControlPlaneConfig.from_env(),
        agent_cache=agent_cache,
        artifact_store=artifact_store,
    )
    # Stash stores so tests can seed data.
    app.state.cp_agent_store = agent_store
    app.state.cp_conversation_store = conversation_store
    app.state.cp_permission_store = permission_store
    app.state.cp_artifact_store = artifact_store
    app.state.cp_db_uri = db_uri
    return app


@pytest.fixture
def cp_client(cp_app: FastAPI) -> Iterator[TestClient]:
    """A TestClient over the wired app (sync; control-plane routes are sync).

    Deliberately does NOT enter the ``with TestClient(...)`` lifespan: the
    upstream ``_lifespan`` starts the harness process manager and other
    runtime singletons that the control-plane routes / enforcement
    middleware don't touch, and that startup needs a fully initialized
    runtime (out of scope for these unit-level tests). Skipping lifespan
    keeps the tests fast and hermetic while still exercising the real
    routes, middleware, stores, and DB.
    """
    client = TestClient(cp_app, raise_server_exceptions=True)
    yield client
    client.close()


def auth_headers(email: str) -> dict[str, str]:
    """Build the identity header for a request as *email*."""
    return {"X-Forwarded-Email": email}


# Minimal valid agent spec for the connection-test happy path. Uses a
# non-``config.yaml`` archive name so it routes through the compat adapter
# (executor.config.harness shorthand), matching the e2e bundle helper.
_TEST_AGENT_YAML = """\
name: test-agent
prompt: You are a friendly assistant.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents
"""


def build_valid_bundle() -> bytes:
    """Build a gzipped-tar agent bundle that parses + validates."""
    import gzip
    import io
    import tarfile

    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = _TEST_AGENT_YAML.encode()
        info = tarfile.TarInfo(name="test_agent.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
