"""Unit test for personal-label stripping in child-session summaries.

``_child_session_summary_from_conversation`` builds a ``ChildSessionSummary``
for a sub-agent row. Personal sidebar labels must not leak through this path.
"""

from __future__ import annotations

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
)
from omnigent.stores.conversation_store import completed_label_key, pinned_label_key


def _child(labels: dict[str, str]) -> Conversation:
    """A minimal sub-agent conversation carrying the given labels."""
    return Conversation(
        id="conv_child",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_parent",
        title="tool:child-task",
        agent_id="ag_test",
        labels=labels,
    )


def test_child_summary_strips_per_user_sidebar_labels() -> None:
    conv = _child(
        {
            pinned_label_key("alice@example.com"): "1721760000000",
            pinned_label_key("bob@example.com"): "1721760001000",
            completed_label_key("alice@example.com"): "1721760002000",
            "omnigent.pinned": "legacy",
            "omnigent.completed": "legacy",
            "omni_project": "Moonshot",
        }
    )
    summary = _child_session_summary_from_conversation(conv, "conv_parent", None)
    # No personal sidebar key survives.
    assert not any(k.startswith("omnigent.pinned") for k in summary.labels)
    assert not any(k.startswith("omnigent.completed") for k in summary.labels)
    # Unrelated labels are preserved.
    assert summary.labels.get("omni_project") == "Moonshot"
