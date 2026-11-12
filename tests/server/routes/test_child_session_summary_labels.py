"""Unit test for pin-key stripping in child-session summaries.

``_child_session_summary_from_conversation`` builds a ``ChildSessionSummary``
for a sub-agent row. Every other serialization path collapses per-user pin keys
via ``_labels_for_viewer``; this one must at least strip them so a shared
child's summary can never expose another viewer's ``omnigent.pinned.<user>``
key. Child sessions aren't pinnable, so there's nothing to surface — just the
strip.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.entities import Agent, Conversation
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


@pytest.mark.parametrize(
    "source,attribution,expected",
    [
        ("claude-wrapper", None, None),
        ("claude-override", None, None),
        ("claude-custom", None, None),
        ("claude-custom", 0.0, 0.0),
        ("claude-custom", 1.0, 1.0),
        ("codex", None, 3.0),
        ("claude-sdk", None, 3.0),
    ],
)
def test_own_cost_provenance_agrees_across_projections(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    attribution: float | None,
    expected: float | None,
) -> None:
    from omnigent.native.native_coding_agents import CLAUDE_NATIVE_CODING_AGENT
    from omnigent.server.routes._sessions import helpers, orchestration
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    conv = _child({})
    conv.parent_conversation_id = "conv_parent"
    conv.session_usage = {"total_cost_usd": 3.0}
    if attribution is not None:
        conv.session_usage["attributed_cost_usd"] = attribution
    if source == "claude-wrapper":
        conv.labels[helpers._CLAUDE_NATIVE_WRAPPER_LABEL_KEY] = (
            CLAUDE_NATIVE_CODING_AGENT.wrapper_label
        )
    if source == "claude-override":
        conv.harness_override = "claude-native"
    harness = (
        "claude-native" if source.startswith("claude-") and source != "claude-sdk" else source
    )
    agent = Agent(id="ag_test", name="custom", created_at=1, bundle_location="bundle")
    spec = AgentSpec(
        spec_version=1, name="custom", executor=ExecutorSpec(config={"harness": harness})
    )
    reads: list[list[str]] = []

    def get_many(ids: list[str]) -> dict[str, Agent]:
        reads.append(ids)
        return {agent.id: agent}

    monkeypatch.setattr(
        "omnigent.runtime._globals._agent_store", SimpleNamespace(get_many=get_many)
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_agent_cache",
        lambda: SimpleNamespace(
            load=lambda *args, **kwargs: SimpleNamespace(spec=spec),
        ),
    )
    resolved = helpers._cost_display_harnesses([conv, conv])
    assert reads == ([[agent.id]] if attribution is None else [])
    summary = _child_session_summary_from_conversation(
        conv,
        "conv_parent",
        None,
        harness=resolved.get(conv.id),
    )
    item = orchestration._build_session_list_item(
        conv,
        agent_names_by_id={},
        grants=[],
        user_id=None,
        user_is_admin=False,
        permissions_enabled=False,
        pending_count=0,
        child_session_ids=[],
        comments_fingerprint=None,
        harness=resolved.get(conv.id),
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(orchestration, "load_session_tree", lambda *args: [conv])
    monkeypatch.setattr(
        orchestration,
        "session_stream",
        SimpleNamespace(
            publish=lambda destination, event: events.append((destination, event)),
        ),
    )
    orchestration._publish_subtree_cost_to_ancestors(SimpleNamespace(), conv.id)
    assert summary.own_cost_usd == item.own_cost_usd == expected
    assert events[0] == (
        "conv_parent",
        {
            "type": "session.child_session.updated",
            "sequence_number": None,
            "conversation_id": "conv_parent",
            "child_session_id": conv.id,
            "child": {"own_cost_usd": expected},
        },
    )
    assert conv.session_usage["total_cost_usd"] == 3.0
