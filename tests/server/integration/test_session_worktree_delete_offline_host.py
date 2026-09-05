"""
Repro for ``DELETE /v1/sessions/{id}`` with ``?delete_branch=true``
while the session's host is offline.

Reported journey: a worktree-backed session's runner goes offline
(e.g. an IP ACL blocks the host tunnel), the user deletes the session
with "delete branch" checked, and the operation fails with a
misleading error while the worktree survives. On a single-replica
server the delete instead *silently succeeds*: the server returns 200,
skips the worktree/branch cleanup entirely (the host is unreachable),
and deletes the row — leaving an orphaned worktree with no indication
to the user that their checked box did nothing.

Expected behavior (Option B from the issue): a clear conflict error
naming the offline runner, with the session retained, so the user can
retry once the runner reconnects — or delete without the flag.

Drives the full app exactly like
``tests/server/integration/test_session_worktree_delete.py``, but with
the host row present while its connection is absent from
``app.state.host_registry`` (host offline).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "b74c8e9f5724ba6a57da245494419bd8"

_WORKTREE_PATH = "/Users/alice/myrepo-worktrees/feature-login"


def _make_offline_host_worktree_conversation(db_uri: str) -> str:
    """Create a worktree-backed session whose host is offline.

    The host row exists (it connected in the past, so the FK resolves)
    but is deliberately NOT registered in ``app.state.host_registry``,
    which is exactly what an offline host looks like to the server.

    :param db_uri: DB URI for the host and conversation stores.
    :returns: The new conversation id.
    """
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host-offline", RESERVED_USER_LOCAL)
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace=_WORKTREE_PATH,
        git_branch="feature/login",
    )
    return conv.id


async def test_delete_branch_with_offline_host_errors_clearly(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` with an offline host must not silently
    drop the requested cleanup.

    The bug: the server returns 200, deletes the session row, and
    skips the worktree/branch removal with only a server-side log
    line — the user's "delete branch" checkbox silently does nothing
    and the worktree is orphaned. The fix contract is a 409 conflict
    (runner offline, cleanup impossible) with the session retained so
    the user can retry or delete without the flag.
    """
    conv_id = _make_offline_host_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")

    assert resp.status_code == 409, (
        "delete_branch=true with an offline host must fail with a clear "
        f"conflict, got {resp.status_code}: a 200 here means the server "
        "silently skipped the worktree/branch cleanup the user asked for "
        "and orphaned the worktree."
    )
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(conv_id)
    assert conv is not None, (
        "the session must be retained when its requested worktree cleanup "
        "cannot run, so the user can retry once the runner reconnects"
    )


async def test_delete_without_flag_with_offline_host_still_succeeds(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The documented workaround must keep working: deleting WITHOUT the
    flag succeeds even when the host is offline (no cleanup is being
    requested, so nothing needs the runner).

    Guards the fix against overreach — an offline host may only block
    the delete when ``delete_branch=true`` asked for host-side work.
    """
    conv_id = _make_offline_host_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}")

    assert resp.status_code == 200, (
        f"plain delete must stay available with an offline host, got {resp.status_code}"
    )
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(conv_id)
    assert conv is None, "the session row must be gone after a plain delete"
