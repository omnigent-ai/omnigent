"""Unit test for pin-key stripping in child-session summaries.

``_child_session_summary_from_conversation`` builds a ``ChildSessionSummary``
for a sub-agent row. Every other serialization path collapses per-user pin keys
via ``_labels_for_viewer``; this one must at least strip them so a shared
child's summary can never expose another viewer's ``omnigent.pinned.<user>``
key. Child sessions aren't pinnable, so there's nothing to surface — just the
strip.
"""

from __future__ import annotations

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
)
from omnigent.stores.conversation_store import pinned_label_key


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


def test_child_summary_strips_per_user_pin_keys() -> None:
    """A per-user pin key on a child row must not leak into its summary."""
    conv = _child(
        {
            pinned_label_key("alice@example.com"): "1721760000000",
            pinned_label_key("bob@example.com"): "1721760001000",
            "omni_project": "Moonshot",
        }
    )
    summary = _child_session_summary_from_conversation(conv, "conv_parent", None)
    # No pin key of any kind survives — not the canonical one, not a per-user one.
    assert not any(k.startswith("omnigent.pinned") for k in summary.labels)
    # Unrelated labels are preserved.
    assert summary.labels.get("omni_project") == "Moonshot"
