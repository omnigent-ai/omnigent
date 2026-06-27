"""Reload-mode app factory for ``omnigent server --reload``.

When uvicorn runs with ``reload=True`` it needs the app specified as an
import string (``"module:factory"``), not a live object — the reloader
forks fresh workers that re-import the module on each code change. This
module exposes :func:`create_app` as that factory.

All resolved configuration (DB URI, artifact location, agent dirs, etc.)
is passed from the CLI via the ``_OMNIGENT_RELOAD_CONFIG`` environment
variable as a JSON blob, so the factory can reconstruct the full app
without re-parsing CLI flags.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the ASGI application from reload config stored in env.

    Called by uvicorn when ``factory=True`` is set, i.e.::

        uvicorn.run(
            "omnigent.server._reload_entry:create_app",
            factory=True,
            reload=True,
            ...
        )

    :returns: A fully wired :class:`FastAPI` application.
    :raises RuntimeError: If ``_OMNIGENT_RELOAD_CONFIG`` is not set.
    """
    raw = os.environ.get("_OMNIGENT_RELOAD_CONFIG")
    if raw is None:
        raise RuntimeError(
            "_OMNIGENT_RELOAD_CONFIG env var is not set. "
            "This module is intended to be used only via `omnigent server --reload`."
        )

    cfg_blob: dict[str, Any] = json.loads(raw)

    db_uri: str = cfg_blob["db_uri"]
    art_loc: str = cfg_blob["artifact_location"]
    execution_timeout: int = cfg_blob["execution_timeout"]
    agent_dirs: list[str] = cfg_blob.get("agent_dirs", [])
    runner_tunnel_token: str | None = cfg_blob.get("runner_tunnel_token")
    config: dict[str, Any] = cfg_blob.get("config", {})

    # ------------------------------------------------------------------
    # Store wiring (mirrors cli.py server command, lines ~2999-3006)
    # ------------------------------------------------------------------
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    comment_store = SqlAlchemyCommentStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    permission_store = SqlAlchemyPermissionStore(db_uri)

    # Artifact store (local or Databricks Volumes).
    from omnigent.cli import _create_artifact_store

    artifact_store = _create_artifact_store(art_loc)

    # ------------------------------------------------------------------
    # Runtime init (mirrors cli.py lines ~3009-3037)
    # ------------------------------------------------------------------
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.spec import parse_default_policies, parse_server_llm

    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=Path(art_loc) / ".cache",
    )

    caps = RuntimeCaps(
        execution_timeout=execution_timeout,
        default_policies=parse_default_policies(config.get("policies")),
        llm=parse_server_llm(config.get("llm")),
    )
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
        policy_store=policy_store,
        caps=caps,
    )

    # OpenTelemetry (no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset).
    from omnigent.runtime import telemetry

    telemetry.init()

    # Runner tunnel token.
    runner_tunnel_tokens: frozenset[str] | None = (
        frozenset({runner_tunnel_token}) if runner_tunnel_token else None
    )

    # Pre-register agents from --agent directories.
    from omnigent.cli import _preregister_agent

    for agent_dir in agent_dirs:
        _preregister_agent(
            Path(agent_dir),
            agent_store,
            artifact_store,
            agent_cache,
        )

    # Host store.
    from omnigent.stores.host_store import HostStore

    host_store = HostStore(db_uri)

    # Sandbox config.
    from omnigent.server.managed_hosts import parse_sandbox_config

    sandbox_config = parse_sandbox_config(config.get("sandbox"))

    # Auth provider.
    from omnigent.server.auth import create_auth_provider

    auth_provider = create_auth_provider()

    # Accounts store (accounts mode only).
    account_store = None
    from omnigent.server.auth import UnifiedAuthProvider as _UAP

    if isinstance(auth_provider, _UAP) and auth_provider._source == "accounts":
        from omnigent.server.accounts_store import SqlAlchemyAccountStore

        account_store = SqlAlchemyAccountStore(db_uri)

    # ------------------------------------------------------------------
    # Build the FastAPI app (mirrors cli.py lines ~3120-3137)
    # ------------------------------------------------------------------
    from omnigent.server.app import create_app as _create_app
    from omnigent.server.server_config import config_str_list

    app = _create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        comment_store=comment_store,
        policy_store=policy_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        runner_tunnel_tokens=runner_tunnel_tokens,
        permission_store=permission_store,
        auth_provider=auth_provider,
        host_store=host_store,
        account_store=account_store,
        policy_modules=config.get("policy_modules"),
        admins=config_str_list(config.get("admins")),
        allowed_domains=config_str_list(config.get("allowed_domains")),
        sandbox_config=sandbox_config,
    )

    return app
