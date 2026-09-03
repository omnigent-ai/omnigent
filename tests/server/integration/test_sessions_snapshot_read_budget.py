"""
Read-budget guard for ``GET /v1/sessions/{session_id}``.

The session snapshot is the widest-reach read in the product: session start
hits it several times per launch, the REPL polls it as a turn backstop, and
both the web UI and the SDK re-read it routinely. Layers of it used to do
their own reads of state the request already held — the authorization pass
fetched the conversation, then the subtree-usage load fetched the same row
again to find its tree root, and the harness resolver re-read the agent row
the snapshot had just loaded.

Behavioural assertions cannot see that: every variant returns the same body.
So this module pins counters instead —

- the route's total SQL statement count,
- that no statement is issued twice in one snapshot build,
- one single-row ``conversations`` fetch and one ``agents`` fetch,

and pins the response body's field set, because a snapshot that silently
loses ``runner_online`` / ``labels`` / ``runner_id`` / ``harness`` is a far
worse outcome than a slow one.
"""

from __future__ import annotations

import json as _json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

pytestmark = pytest.mark.asyncio

_BUDGET_USER = "budget@example.com"

# One authorized ``GET /v1/sessions/{id}`` on a session with zero items,
# measured with warm spec / ACL caches. The layer-by-layer tally:
#
#   3  authorization's ``get_conversation`` (row + metadata + labels)
#   1  the committed-items page
#   2  the bound agent row + its spawn-tree session lookup
#   2  the runner/host liveness lookup (metadata + labels)
#   3  the spawn-tree page for subtree usage (rows + labels + metadata)
#
# Raising this needs a reason in the diff: every caller of this endpoint pays
# it, several times per session launch.
_SNAPSHOT_ROUTE_SQL_BUDGET = 11

# Every field the snapshot contract carries for a freshly created session.
# Asserted as a subset so adding a field is fine and dropping one is not.
_SNAPSHOT_FIELDS = frozenset(
    {
        "active_response_id",
        "agent_id",
        "agent_name",
        "archived",
        "background_task_count",
        "background_tasks",
        "context_window",
        "cost_control_mode_override",
        "created_at",
        "external_session_id",
        "git_branch",
        "harness",
        "host_id",
        "host_online",
        "host_resumable",
        "id",
        "items",
        "kind",
        "labels",
        "last_task_error",
        "last_total_tokens",
        "llm_model",
        "mcp_startup",
        "model_options",
        "model_override",
        "parent_session_id",
        "pending_elicitations",
        "pending_inputs",
        "permission_level",
        "project_id",
        "reasoning_effort",
        "root_conversation_id",
        "runner_id",
        "runner_online",
        "sandbox_status",
        "skills",
        "status",
        "sub_agent_name",
        "subagent_routing_override",
        "terminal_launch_args",
        "terminal_pending",
        "title",
        "todos",
        "total_cost_usd",
        "updated_at",
        "usage_by_model",
        "workspace",
    }
)


@pytest.fixture()
def budget_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with permissions + header auth, so the route runs its real auth pass."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def budget_client(budget_app: FastAPI, mock_llm: Any, tmp_path: Path):
    """Async client against the auth-enabled app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    transport = httpx.ASGITransport(app=budget_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


async def _seed_session(client: httpx.AsyncClient, db_uri: str) -> str:
    """Create a real agent-bound session owned by the budget user.

    Goes through the real create path with a genuine uploaded bundle: a
    fabricated agent row cannot be loaded from the artifact store, and the
    snapshot's agent / harness resolvers would bail before doing the reads
    this budget measures.

    :param client: The authenticated async client.
    :param db_uri: Database URI, for granting the session.
    :returns: The created session id.
    """
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from tests.server.helpers import build_agent_bundle

    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(_BUDGET_USER)
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({"title": "snapshot budget"})},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="budget-agent"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": _BUDGET_USER},
    )
    assert resp.status_code == 201, resp.text
    session_id = str(resp.json()["session_id"])
    perms.grant(_BUDGET_USER, session_id, LEVEL_OWNER)
    return session_id


def _normalized(statement: str) -> str:
    """Collapse whitespace so the same query always compares equal."""
    return re.sub(r"\s+", " ", statement).strip()


def _is_conversation_row_fetch(statement: str) -> bool:
    """Whether *statement* is a single-row ``get_conversation`` entity load.

    The ORM entity select labels its columns (``... AS conversations_id``);
    the spawn-tree page listing selects them bare, so the two are
    distinguishable without matching on the WHERE clause.
    """
    return statement.startswith("SELECT conversations.workspace_id AS conversations_workspace_id")


def _is_agent_row_fetch(statement: str) -> bool:
    """Whether *statement* reads the bound agent's row."""
    return " FROM agents " in f" {statement} "


async def _measured_get(
    client: httpx.AsyncClient,
    db_uri: str,
    session_id: str,
) -> tuple[list[str], dict[str, Any]]:
    """Issue one authorized snapshot GET, recording every SQL statement.

    :param client: The authenticated async client.
    :param db_uri: Database URI, used to find the engine to listen on.
    :param session_id: The session to read.
    :returns: The normalized statements, and the response body.
    """
    from sqlalchemy import event as sa_event

    from omnigent.db.utils import _engine_cache

    headers = {"X-Forwarded-Email": _BUDGET_USER}
    # Warm the spec / ACL / status caches so the count is the steady-state one
    # a poll pays, not a cold-process one-off.
    for _ in range(3):
        warm = await client.get(f"/v1/sessions/{session_id}", headers=headers)
        assert warm.status_code == 200, warm.text

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith("PRAGMA"):
            statements.append(_normalized(statement))

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        resp = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)

    assert resp.status_code == 200, resp.text
    return statements, resp.json()


async def test_snapshot_route_sql_budget(
    budget_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Pin the statement count for one snapshot GET on an empty session.

    This is the oracle for threading the request's own state forward:
    dropping the ``root_conversation_id=`` hint on the subtree-usage load, or
    the ``agent=`` hand-off to the harness resolver, makes those layers read
    state the request already held and moves this count. No behavioural
    assertion can see it — every variant returns the same body.
    """
    session_id = await _seed_session(budget_client, db_uri)
    statements, body = await _measured_get(budget_client, db_uri, session_id)

    assert body["items"] == [], "fixture must be an empty session for this budget"
    assert len(statements) == _SNAPSHOT_ROUTE_SQL_BUDGET, [s[:90] for s in statements]


async def test_snapshot_route_issues_no_statement_twice(
    budget_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """No query runs twice in one snapshot build.

    Each layer of the snapshot answers a different question, so a repeated
    query means two layers observed identical state independently — the shape
    of redundancy this endpoint kept regressing into.
    """
    session_id = await _seed_session(budget_client, db_uri)
    statements, _ = await _measured_get(budget_client, db_uri, session_id)

    repeated = {s[:110]: n for s, n in Counter(statements).items() if n > 1}
    assert repeated == {}, repeated


async def test_snapshot_route_reads_conversation_and_agent_once(
    budget_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The session's own row and its agent row are each read exactly once.

    The authorization pass is the source of truth for both the conversation
    row and the caller's level, so it keeps its read; every later layer that
    needs the same row is handed it. The spawn-tree page listing is NOT this
    read — it observes the session's descendants, which the single row cannot
    answer — so it is deliberately still there.
    """
    session_id = await _seed_session(budget_client, db_uri)
    statements, _ = await _measured_get(budget_client, db_uri, session_id)

    row_fetches = [s for s in statements if _is_conversation_row_fetch(s)]
    assert len(row_fetches) == 1, [s[:90] for s in row_fetches]
    agent_fetches = [s for s in statements if _is_agent_row_fetch(s)]
    assert len(agent_fetches) == 1, [s[:90] for s in agent_fetches]


async def test_snapshot_body_keeps_every_field(
    budget_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The snapshot still carries its whole contract after the read dedupe.

    Losing a field is far worse than a slow response: session start, the
    REPL, the web UI and the SDK each pull a different one of these out of
    the same document, so a dropped ``harness`` or ``runner_online`` breaks a
    caller that no read-budget number would notice.
    """
    session_id = await _seed_session(budget_client, db_uri)
    _, body = await _measured_get(budget_client, db_uri, session_id)

    assert set(body) >= _SNAPSHOT_FIELDS, sorted(_SNAPSHOT_FIELDS - set(body))
    # The fields fed by the reads this module dedupes, spelled out: ``harness``
    # and ``llm_model`` now come from the agent row the snapshot already read,
    # and the cost fields from a tree loaded off the in-hand row's root.
    assert body["id"] == session_id
    assert body["harness"] is not None
    assert body["agent_name"] == "budget-agent"
    assert body["agent_id"] is not None
    assert body["status"] == "idle"
    assert body["permission_level"] is not None
    assert body["labels"] == {}
    assert body["runner_id"] is None
    # ``runner_online`` must carry a verdict, not ``None``: ``None`` means the
    # liveness lookup was not wired through, and the CLI treats it as
    # "optimistically online". Its VALUE is deliberately a superset of
    # ``GET /v1/runners/{id}/status`` (it stays true through the liveness TTL
    # after an ungraceful death), which is pinned elsewhere — not here.
    assert body["runner_online"] is not None
    assert body["host_id"] is None
    assert body["host_resumable"] is False
