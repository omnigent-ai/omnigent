"""An outstanding approval survives losing the in-process index.

The in-memory pending-elicitations index dies with the server process. The
count it mirrors onto the conversation row does not, so before the
``elicitations`` table existed a restart left the sidebar claiming an approval
was waiting while ``GET /v1/sessions/{id}`` replayed nothing for the user to
answer.

These tests wipe the index to stand in for that restart — the process boundary
itself is what a unit or integration test cannot cross — and drive the real
HTTP snapshot endpoint on either side of it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_elicitation_resolve_url import (
    _create_session,
    _park_permission_hook,
)

pytestmark = pytest.mark.asyncio


async def _pending_ids(client: httpx.AsyncClient, session_id: str) -> list[str]:
    """Return the elicitation ids the session snapshot replays."""
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    return [item["elicitation_id"] for item in resp.json().get("pending_elicitations", [])]


async def test_snapshot_replays_a_prompt_after_the_index_is_lost(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The prompt comes back on the snapshot, so the user can still answer it."""
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client, "test-elicitation-restart")
    session_id = await _create_session(client, agent["id"])
    hook_task, elicitation_id = await _park_permission_hook(client, session_id)

    try:
        assert await _pending_ids(client, session_id) == [elicitation_id]

        # Stand in for the restart: the index is gone, the row is not.
        pending_elicitations.reset_for_tests()
        assert pending_elicitations.count_for(session_id) == 0

        # Rewire the store the way the server does at startup. Only the index
        # was lost, so this is the whole of what a restart re-establishes.
        from omnigent.stores.elicitation_store.sqlalchemy_store import (
            SqlAlchemyElicitationStore,
        )

        pending_elicitations.set_store(SqlAlchemyElicitationStore(db_uri))

        assert await _pending_ids(client, session_id) == [elicitation_id]
    finally:
        hook_task.cancel()


async def test_resolved_prompt_does_not_come_back(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An answered approval must not be asked again after a restart."""
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client, "test-elicitation-restart-resolved")
    session_id = await _create_session(client, agent["id"])
    hook_task, elicitation_id = await _park_permission_hook(client, session_id)

    resp = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert resp.status_code in (200, 202), resp.text
    await hook_task

    from omnigent.stores.elicitation_store.sqlalchemy_store import (
        SqlAlchemyElicitationStore,
    )

    pending_elicitations.reset_for_tests()
    pending_elicitations.set_store(SqlAlchemyElicitationStore(db_uri))

    assert await _pending_ids(client, session_id) == []


async def test_snapshot_restores_when_the_mirrored_count_was_lost(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The durable row must not be gated behind the best-effort count.

    ``pending_elicitation_count`` is mirrored onto the conversation row by a
    background executor that logs and drops failures. So a crash between the
    row commit and the count write leaves a real, still-parked prompt whose
    count says zero — and a count-gated read would never ask for it. That is
    the same crash the durable rows exist to survive, so it must not be the
    thing that hides them.
    """
    from omnigent.runtime import pending_elicitations
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.elicitation_store.sqlalchemy_store import (
        SqlAlchemyElicitationStore,
    )

    agent = await create_test_agent(client, "test-elicitation-count-lost")
    session_id = await _create_session(client, agent["id"])
    hook_task, elicitation_id = await _park_permission_hook(client, session_id)

    try:
        # The row is durable; now make the mirror say nothing is parked, which
        # is what a dropped count write leaves behind.
        SqlAlchemyConversationStore(db_uri).set_pending_elicitation_count(session_id, 0)
        pending_elicitations.reset_for_tests()
        pending_elicitations.set_store(SqlAlchemyElicitationStore(db_uri))

        assert await _pending_ids(client, session_id) == [elicitation_id]
    finally:
        hook_task.cancel()


async def test_the_standalone_approval_page_survives_a_restart(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The approval link is the phone path; a restart must not blank it.

    ``GET /v1/sessions/{id}/elicitations/{eid}`` backs
    ``/approve/:sessionId/:elicitationId``. Reading only the in-memory index
    means that after a restart it reports every genuinely-pending prompt as
    ``resolved`` — telling someone their approval was already handled when the
    work is still parked on it.
    """
    from omnigent.runtime import pending_elicitations
    from omnigent.stores.elicitation_store.sqlalchemy_store import (
        SqlAlchemyElicitationStore,
    )

    agent = await create_test_agent(client, "test-elicitation-approve-page")
    session_id = await _create_session(client, agent["id"])
    hook_task, elicitation_id = await _park_permission_hook(client, session_id)

    try:
        pending_elicitations.reset_for_tests()
        pending_elicitations.set_store(SqlAlchemyElicitationStore(db_uri))

        resp = await client.get(f"/v1/sessions/{session_id}/elicitations/{elicitation_id}")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending"
    finally:
        hook_task.cancel()


async def test_ancestor_snapshot_replays_a_child_prompt_after_the_index_is_lost(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A sub-agent's parked approval is answerable from the parent after a restart.

    A child's prompt mirrors into the ancestor's chat while everything is
    live. After a restart the index is cold *everywhere*, so the cheap
    "anything pending anywhere?" gate that protects the descendant walk sees
    nothing — and without a gate-free first walk, the parent's cold load
    would render no card for a child approval whose durable row is genuinely
    parked.
    """
    from omnigent.runtime import pending_elicitations
    from omnigent.stores.elicitation_store.sqlalchemy_store import (
        SqlAlchemyElicitationStore,
    )
    from tests.server.integration.test_sessions_elicitation_resolve_url import (
        _claude_permission_payload,
        _create_child_session,
        _park_permission_hook_on,
    )

    agent = await create_test_agent(client, "test-elicitation-restart-ancestor")
    parent_id = await _create_session(client, agent["id"])
    child_id = _create_child_session(db_uri, parent_id=parent_id, agent_id=agent["id"])
    hook_task, mirrored = await _park_permission_hook_on(
        client,
        child_id,
        parent_id,
        payload=_claude_permission_payload("Bash"),
    )
    elicitation_id = mirrored["elicitation_id"]

    try:
        assert await _pending_ids(client, parent_id) == [elicitation_id]

        # The restart: index cold for parent and child alike; rows survive.
        pending_elicitations.reset_for_tests()
        pending_elicitations.set_store(SqlAlchemyElicitationStore(db_uri))

        assert await _pending_ids(client, parent_id) == [elicitation_id]
    finally:
        hook_task.cancel()


async def test_a_crashed_descendant_walk_does_not_consume_the_restore_claim(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A walk that dies mid-flight must not spend the ancestor's only walk.

    The gate-free descendant walk is granted once per ancestor per process.
    If listing the descendants raises (a transient store hiccup) after the
    claim was taken, the claim must come back — otherwise the child's parked
    approval stays invisible from the parent for the process's life.
    """
    from omnigent.runtime import pending_elicitations
    from omnigent.server.routes._sessions import helpers as session_helpers
    from omnigent.stores.elicitation_store.sqlalchemy_store import (
        SqlAlchemyElicitationStore,
    )
    from tests.server.integration.test_sessions_elicitation_resolve_url import (
        _claude_permission_payload,
        _create_child_session,
        _park_permission_hook_on,
    )

    agent = await create_test_agent(client, "test-elicitation-restart-walk-crash")
    parent_id = await _create_session(client, agent["id"])
    child_id = _create_child_session(db_uri, parent_id=parent_id, agent_id=agent["id"])
    hook_task, mirrored = await _park_permission_hook_on(
        client,
        child_id,
        parent_id,
        payload=_claude_permission_payload("Bash"),
    )
    elicitation_id = mirrored["elicitation_id"]

    try:
        # The restart: index cold everywhere; the child's row survives.
        pending_elicitations.reset_for_tests()
        pending_elicitations.set_store(SqlAlchemyElicitationStore(db_uri))

        # The parent's first cold snapshot claims the walk, then the walk dies.
        real_walk = session_helpers._descendant_sessions
        crashed = False

        def _crash_once(conv_store: Any, session_id: str) -> Any:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("transient store hiccup")
            return real_walk(conv_store, session_id)

        monkeypatch.setattr(session_helpers, "_descendant_sessions", _crash_once)
        with pytest.raises(RuntimeError, match="transient store hiccup"):
            await client.get(f"/v1/sessions/{parent_id}")

        # The next snapshot must get the walk again and surface the child's
        # parked approval.
        assert await _pending_ids(client, parent_id) == [elicitation_id]
    finally:
        hook_task.cancel()
