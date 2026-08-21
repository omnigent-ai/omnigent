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
