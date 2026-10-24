"""Spawn-tree usage scenario shared by the single- and split-DB store tests."""

from __future__ import annotations

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def assert_tree_usage_matches_full_listing(store: SqlAlchemyConversationStore) -> None:
    """
    ``list_tree_usage`` is a narrow projection of the full tree listing.

    Cost gating sums it, so it must cover exactly the rows the full
    ``list_conversations(kind=None, include_archived=True)`` walk covered:
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

    nodes = store.list_tree_usage(root.id)

    full = store.list_conversations(
        limit=100, kind=None, root_conversation_id=root.id, include_archived=True
    ).data
    assert {(n.id, n.parent_conversation_id) for n in nodes} == {
        (c.id, c.parent_conversation_id) for c in full
    }
    assert {n.id: n.session_usage for n in nodes} == {
        root.id: {"total_cost_usd": 1.0, "by_model": {"m": {"total_cost_usd": 1.0}}},
        child.id: {},
        nested.id: {"policy_cost_usd": 0.5},
    }
    assert store.list_tree_usage("5eca720dc2bc6cdc3a99028d7bd0f917") == []
