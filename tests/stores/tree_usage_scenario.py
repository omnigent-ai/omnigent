"""Spawn-tree usage scenario shared by the single- and split-DB store tests."""

from __future__ import annotations

from omnigent.stores.conversation_store import SpawnTreeUsage
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def read_tree_usage_in_pages(
    store: SqlAlchemyConversationStore, root_conversation_id: str, page_size: int
) -> list[SpawnTreeUsage]:
    """Walk ``list_spawn_tree_usage`` page by page with its ``after`` cursor."""
    rows: list[SpawnTreeUsage] = []
    after: str | None = None
    while True:
        page = store.list_spawn_tree_usage(root_conversation_id, limit=page_size, after=after)
        rows.extend(page.data)
        if not page.has_more:
            return rows
        after = page.last_id


def assert_paged_tree_usage_matches_full_listing(
    store: SqlAlchemyConversationStore, page_size: int
) -> None:
    """
    Paged ``list_spawn_tree_usage`` reads are a narrow projection of the full tree listing.

    Cost gating sums them, so they must cover exactly the rows the full
    ``list_conversations(kind=None, include_archived=True)`` walk covers:
    archived rows (an archived root or mid-tree node still holds spend) and
    nested descendants included, other trees excluded, and a row with no
    recorded usage present with an empty dict.
    """
    root = store.create_conversation()
    child = store.create_conversation(
        kind="sub_agent", title="coder:a", parent_conversation_id=root.id
    )
    nested = store.create_conversation(
        kind="sub_agent", title="reviewer:b", parent_conversation_id=child.id
    )
    other_tree = store.create_conversation()
    store.set_session_usage(
        root.id, {"total_cost_usd": 1.0, "by_model": {"m": {"total_cost_usd": 1.0}}}
    )
    store.set_session_usage(nested.id, {"policy_cost_usd": 0.5})
    store.set_session_usage(other_tree.id, {"total_cost_usd": 9.0})
    store.update_conversation(root.id, archived=True)
    store.update_conversation(child.id, archived=True)

    rows = read_tree_usage_in_pages(store, root.id, page_size)

    full = store.list_conversations(
        limit=100, kind=None, root_conversation_id=root.id, include_archived=True
    ).data
    assert [row.id for row in rows] == sorted(c.id for c in full)
    assert {(row.id, row.parent_conversation_id) for row in rows} == {
        (c.id, c.parent_conversation_id) for c in full
    }
    assert {row.id: row.session_usage for row in rows} == {
        root.id: {"total_cost_usd": 1.0, "by_model": {"m": {"total_cost_usd": 1.0}}},
        child.id: {},
        nested.id: {"policy_cost_usd": 0.5},
    }
    assert read_tree_usage_in_pages(store, "5eca720dc2bc6cdc3a99028d7bd0f917", page_size) == []
