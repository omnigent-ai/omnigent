"""E2E: a delete between page fetches must not silently truncate enumeration.

``_apply_cursor`` resolves an ``after`` cursor's sort key with a scalar
subquery keyed on the cursor row's id. When that row is deleted between two
page fetches the subquery yields NULL, every comparison in the WHERE clause
evaluates to NULL, and the next page returns ``data=[]`` with
``has_more=False`` — indistinguishable from a completed enumeration. Every
standard ``while has_more: after = last_id`` loop then stops early and
reports success on a partial result.

Covers the two reported consequences:

* the sessions API journey — a client paging ``GET /v1/sessions`` while the
  user deletes the cursor session silently never sees the remaining rows;
* the policy/display spend seed — ``load_session_usage`` builds its subtree
  total from the same enumeration loop, so a concurrent delete under-counts
  the surviving tree's spend.

Uses the shared ``client`` fixture from ``tests/server/conftest.py`` (real
route → store pipeline) and a real ``SqlAlchemyConversationStore`` on the
same per-test database, matching the suite's conventions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runtime.policies.builder import (
    _SUBTREE_USAGE_PAGE_SIZE,
    load_session_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

_PAGE_LIMIT = 2


@pytest.mark.asyncio
async def test_session_enumeration_not_silently_truncated_by_deleted_cursor(
    client: httpx.AsyncClient,
) -> None:
    """Deleting the cursor session mid-enumeration must not drop the rest.

    Drives the standard client enumeration loop over ``GET /v1/sessions``
    and lands the user's ``DELETE /v1/sessions/{id}`` on the page-1 cursor
    between the two page fetches (the concurrent-delete race, made
    deterministic). The loop must either surface every surviving session
    or receive a distinguishable non-success outcome — never a silent
    ``data=[] / has_more=False`` "fully enumerated" report while surviving
    sessions were never returned.
    """
    agent = await create_test_agent(client, name="deleted-cursor-agent")
    created: list[str] = []
    for i in range(5):
        resp = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "title": f"cursor-s{i}"},
        )
        assert resp.status_code in (200, 201), resp.text
        created.append(resp.json()["id"])

    enumerated: list[str] = []
    deleted_id: str | None = None
    after: str | None = None
    while True:
        params: dict[str, Any] = {
            "limit": _PAGE_LIMIT,
            "order": "asc",
            "agent_id": agent["id"],
        }
        if after is not None:
            params["after"] = after
        resp = await client.get("/v1/sessions", params=params)
        if deleted_id is not None and resp.status_code != 200:
            # A non-200 after the cursor row died is a *distinguishable*
            # outcome (the "signal the dead cursor" fix direction): the
            # caller can restart. This test guards only against the silent
            # truncation, so a signaled dead cursor passes.
            return
        assert resp.status_code == 200, resp.text
        page = resp.json()
        enumerated.extend(s["id"] for s in page["data"])
        if not page["has_more"] or not page["data"]:
            break
        after = page["data"][-1]["id"]
        if deleted_id is None:
            # The user deletes the session that happens to be the page-1
            # cursor, between the two page fetches.
            deleted_id = after
            del_resp = await client.delete(f"/v1/sessions/{deleted_id}")
            assert del_resp.status_code == 200, del_resp.text

    assert deleted_id is not None, "test setup: first page never had more rows"
    survivors = [cid for cid in created if cid != deleted_id]
    # Ground the "rows remained" claim: every survivor is still readable.
    for cid in survivors:
        live = await client.get(f"/v1/sessions/{cid}")
        assert live.status_code == 200, f"survivor {cid} unexpectedly gone"
    missing = [cid for cid in survivors if cid not in enumerated]
    assert not missing, (
        "enumeration reported completion (has_more=False) while surviving "
        f"sessions were never returned: missing={missing} "
        f"enumerated={enumerated} deleted_cursor={deleted_id}"
    )


class _DeleteCursorBetweenPages:
    """Store proxy landing a delete of the page-1 cursor row between pages.

    Delegates every call to the real store; the first paged
    ``list_conversations`` read triggers a real ``delete_conversation`` of
    that page's cursor row *after* the page is read and *before* the caller
    requests the next one — the concurrent user delete, made deterministic.
    """

    def __init__(self, inner: SqlAlchemyConversationStore) -> None:
        self._inner = inner
        self.deleted_id: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def list_conversations(self, *args: Any, **kwargs: Any) -> Any:
        page = self._inner.list_conversations(*args, **kwargs)
        if self.deleted_id is None and page.has_more and page.last_id is not None:
            self.deleted_id = page.last_id
            assert asyncio.run(self._inner.delete_conversation(self.deleted_id))
        return page


def test_policy_usage_seed_not_silently_truncated_by_concurrent_delete(
    db_uri: str,
) -> None:
    """A concurrent delete must not silently under-count the spend seed.

    Builds a spawn tree larger than ``_SUBTREE_USAGE_PAGE_SIZE`` where every
    conversation recorded $1.00 of spend, then loads the subtree usage via
    the public ``load_session_usage`` entry point (the same paged tree read
    that seeds ``PolicyEngine(initial_usage=...)``) while a delete of the
    page-1 cursor row lands between the two page fetches. The surviving
    tree's spend must not be silently under-reported — a truncated total
    moves budget gates and can allow an over-budget tool call.
    """
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(title="usage-root")
    store.set_session_usage(root.id, {"total_cost_usd": 1.0})
    n_children = _SUBTREE_USAGE_PAGE_SIZE + 4
    for i in range(n_children):
        child = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=root.id,
            sub_agent_name=f"worker-{i}",
        )
        store.set_session_usage(child.id, {"total_cost_usd": 1.0})

    proxy = _DeleteCursorBetweenPages(store)
    try:
        usage = load_session_usage(root.id, proxy)
    except Exception:
        # An error is a *distinguishable* outcome (the "signal the dead
        # cursor" fix direction); this test guards only against the silent
        # under-count.
        return

    assert proxy.deleted_id is not None, "test setup: tree never paged"
    # 1 root + n_children rows at $1 each, minus the one deleted mid-read.
    surviving_total = float(1 + n_children - 1)
    got = float(usage.get("total_cost_usd", 0.0))
    assert got >= surviving_total - 1e-6, (
        f"spend seed under-counted after a concurrent delete: got ${got}, "
        f"but the surviving tree still holds ${surviving_total} "
        f"(deleted mid-enumeration: {proxy.deleted_id})"
    )
