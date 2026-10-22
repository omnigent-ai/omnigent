"""Regression guard: ``GET /v1/sessions`` must not pay an
O(all-accessible-conversations) cost per request.

The bug: the ACL filter in ``list_conversations`` pre-fetches **every**
conversation id the caller can access into Python and re-binds them all as an
``IN (...)`` clause on the conversations query, so a ``limit=1`` page request
marshals N ids out of the database and N back in (an ~88 KB statement for a
2303-session user in the report) before a single requested row is considered.

These tests drive the real user journey — an authenticated user requesting one
page of their session list over HTTP — against the full app (header auth +
permission store, both stores on one database, the same-bind mode where the
ACL pushdown is valid), and assert on the SQL the request actually executes:

- no statement may bind O(all-accessible) parameters, and
- the per-request SQL cost must not grow with the caller's total
  accessible-session count.

Both fail on the prefetch + ``IN (...)`` implementation and pass once the ACL
is pushed into the conversations query (e.g. a correlated ``EXISTS``), however
the fix is shaped. Split-database deployments (separate Omnigent / AP engines)
keep the prefetch as a fallback — that mode is covered by
``tests/stores/test_conversation_store_split_db.py``, not here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event

from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio

# Accessible-session counts for the two seeded users. Large enough that an
# O(all-accessible) bind blast is unmistakable against the thresholds below,
# small enough to seed in well under a second.
_N_MANY = 250
_N_FEW = 20

# A fixed statement never legitimately needs this many bind parameters to
# return one page: the page query binds the workspace + a handful of filter
# values, and follow-up batch queries (labels, metadata) bind at most the
# page's own ids. The buggy path binds all N accessible ids (~252 for the
# many-user) and trips this immediately.
_MAX_SANE_BINDS = 50

_USER_MANY = "acl-cost-many@example.com"
_USER_FEW = "acl-cost-few@example.com"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def acl_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """App with header auth + permission store — the multi-user posture
    where ``GET /v1/sessions`` applies the per-user ACL filter.

    Both stores share ``db_uri`` (one engine), i.e. the same-bind mode in
    which the ACL pushdown is valid and the Python-side prefetch is pure
    overhead.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    :param monkeypatch: Pytest env patcher, reverted per test.
    """
    # Clear every auth-related ambient variable the provider reads at request
    # time, so an environment that exports one (e.g. a custom identity header
    # name or single-user mode) can't change which header authenticates the
    # seeded users below.
    for var in (
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_AUTH_HEADER",
        "OMNIGENT_AUTH_HEADER_STRIP_PREFIX",
        "OMNIGENT_LOCAL_SINGLE_USER",
    ):
        monkeypatch.delenv(var, raising=False)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
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
        permission_store=SqlAlchemyPermissionStore(db_uri),
        # Strict header mode (the deployed multi-user posture), constructed
        # directly so ambient OMNIGENT_* env vars can't flip the mode.
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def acl_client(
    acl_app: FastAPI,
    mock_llm: ControllableMockClient,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled app.

    No harness process manager: these tests only list sessions, they never
    run a turn.
    """
    transport = httpx.ASGITransport(app=acl_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()


@pytest.fixture()
def seeded_users(db_uri: str) -> dict[str, int]:
    """Seed two users' sessions through the app's own create path.

    ``create_conversation`` + an owner grant is exactly what
    ``POST /v1/sessions`` persists per created session; driving the store
    APIs directly just skips the (irrelevant, slow) bundle upload.

    :param db_uri: Test database URI.
    :returns: ``{user_id: accessible_count}`` for the seeded users.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    agent_id = uuid.uuid4().hex  # hex uuid, as the agent store issues
    counts = {_USER_MANY: _N_MANY, _USER_FEW: _N_FEW}
    for user, n in counts.items():
        for i in range(n):
            conv = conv_store.create_conversation(
                title=f"{user.split('@')[0]}-{i}",
                agent_id=agent_id,
            )
            perm_store.grant(user, conv.id, LEVEL_OWNER)
    return counts


class _SqlCapture:
    """Record every statement (and its bind count) an engine executes."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, int]] = []

    def __call__(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        n_binds = len(parameters) if isinstance(parameters, (tuple, list, dict)) else 0
        self.statements.append((statement, n_binds))

    def relevant(self) -> list[tuple[str, int]]:
        """Statements touching the tables on the session-list path.

        Filters out PRAGMAs and unrelated background queries so the
        assertions can't flake on incidental engine traffic.
        """
        keep = ("conversations", "session_permissions", "conversation_labels")
        return [(stmt, n) for stmt, n in self.statements if any(table in stmt for table in keep)]

    def max_binds(self) -> int:
        return max((n for _, n in self.relevant()), default=0)


@pytest.fixture()
def sql_capture(db_uri: str) -> Iterator[_SqlCapture]:
    """Attach a bind-count capture to the app's (cached) engine."""
    engine = get_or_create_engine(db_uri)
    capture = _SqlCapture()
    event.listen(engine, "before_cursor_execute", capture)
    yield capture
    event.remove(engine, "before_cursor_execute", capture)


async def _list_one(
    client: httpx.AsyncClient,
    user: str,
    capture: _SqlCapture,
) -> None:
    """Request one page of the user's session list, capturing its SQL."""
    capture.statements.clear()
    resp = await client.get(
        "/v1/sessions",
        params={"limit": 1},
        headers={"X-Forwarded-Email": user},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1


# ── Tests ─────────────────────────────────────────────────────────────


async def test_sessions_list_page_binds_are_bounded(
    acl_client: httpx.AsyncClient,
    seeded_users: dict[str, int],
    sql_capture: _SqlCapture,
) -> None:
    """A ``limit=1`` list request must not bind O(all-accessible) params.

    Journey: a user who has accumulated many sessions opens the app, which
    fetches a page of their session list. The request's SQL must be bounded
    by the page, not by the user's total accessible-session count — the bug
    re-binds all N accessible ids into an ``IN (...)`` clause (N+2 binds,
    an ~88 KB statement at the report's 2303 sessions) to return one row.
    """
    await _list_one(acl_client, _USER_MANY, sql_capture)

    offenders = [(stmt, n) for stmt, n in sql_capture.relevant() if n > _MAX_SANE_BINDS]
    assert not offenders, (
        f"GET /v1/sessions?limit=1 for a user with {_N_MANY} accessible "
        f"sessions executed statement(s) binding O(all-accessible) "
        f"parameters — the ACL prefetch is being re-bound as IN (...) "
        f"instead of pushed into the query: "
        + "; ".join(f"binds={n}: {' '.join(s.split())[:120]}" for s, n in offenders)
    )


async def test_sessions_list_cost_does_not_scale_with_accessible_total(
    acl_client: httpx.AsyncClient,
    seeded_users: dict[str, int],
    sql_capture: _SqlCapture,
) -> None:
    """Per-request SQL cost must be flat across accessible-session counts.

    The same ``limit=1`` request is made by a user with ``_N_FEW`` accessible
    sessions and one with ``_N_MANY``. Under the bug the many-user's page
    query binds ~N_MANY parameters while the few-user's binds ~N_FEW — the
    O(all-accessible) signature. After the pushdown both requests bind the
    same small, fixed number of values, so the many-user's maximum bind
    count may not exceed the few-user's by more than a fixed slack.
    """
    await _list_one(acl_client, _USER_FEW, sql_capture)
    few_binds = sql_capture.max_binds()

    await _list_one(acl_client, _USER_MANY, sql_capture)
    many_binds = sql_capture.max_binds()

    # Fixed slack, NOT proportional to N: a compliant implementation's bind
    # count is invariant in the accessible total (few extra filter values at
    # most). The buggy path shows ~(_N_MANY - _N_FEW) ≈ 230 extra binds.
    assert many_binds <= few_binds + 10, (
        f"session-list SQL cost scales with the caller's accessible total: "
        f"max binds {few_binds} at {_N_FEW} accessible sessions vs "
        f"{many_binds} at {_N_MANY} — page cost must be O(limit), "
        f"independent of how many sessions the user can access"
    )
