"""
Tests for :meth:`PolicyEngine.apply_label_writes` schema
validation (POLICIES.md §10 / §13).

Silent-drop semantics:

- Key not in ``LabelDef.values`` → dropped.
- Unknown key (no LabelDef) → set freely.
- Valid write → persisted via the store.

The drop path is silent by design (matches omnigent) —
a runtime validation failure does NOT raise. The surviving
writes still land atomically.
"""

from __future__ import annotations

from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import LabelDef
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# ── Engine-level filtering ────────────────────────────


def _build_engine_with_defs(
    store: SqlAlchemyConversationStore,
    label_defs: dict[str, LabelDef],
    *,
    initial_labels: dict[str, str] | None = None,
) -> PolicyEngine:
    """Build an engine with specific label_defs."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=[],
        label_defs=label_defs,
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels=initial_labels or {},
        conversation_store=store,
    )


def test_apply_label_writes_drops_value_outside_enum(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A value not in ``LabelDef.values`` is silently
    dropped. Prevents a policy (or a prompt-policy
    classifier) from injecting an arbitrary string into an
    enumerated label."""
    engine = _build_engine_with_defs(
        conversation_store,
        {"integrity": LabelDef(values=["0", "1"])},
    )
    # "2" is not in values → dropped. "integrity": "1" is
    # valid → lands.
    engine.apply_label_writes({"integrity": "1", "other": "x"})
    # Hot cache has the valid write + the unknown-key
    # write (unknown keys pass through per POLICIES.md §10
    # schemaless-set-freely rule).
    assert engine.labels == {"integrity": "1", "other": "x"}

    # Now try to set an out-of-enum value.
    engine.apply_label_writes({"integrity": "2"})
    # Dropped — cache still shows "1".
    assert engine.labels["integrity"] == "1"


def test_apply_label_writes_partial_batch_survives(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """One key in a multi-key batch violates the schema;
    OTHER keys still land. Silent-drop is per-key, not
    all-or-nothing."""
    engine = _build_engine_with_defs(
        conversation_store,
        {
            "integrity": LabelDef(values=["0", "1"]),
            "other": LabelDef(values=["a", "b"]),
        },
        initial_labels={"integrity": "0"},
    )
    # integrity "2" is out-of-enum (drop); other "a" is valid (land).
    engine.apply_label_writes({"integrity": "2", "other": "a"})
    # Only `other` landed; integrity unchanged.
    assert engine.labels == {"integrity": "0", "other": "a"}


def test_apply_label_writes_schemaless_keys_pass_freely(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Keys with no LabelDef are set freely — the
    omnigent-parity behavior that lets policies write
    ad-hoc labels without declaring a schema first
    (POLICIES.md §10)."""
    engine = _build_engine_with_defs(
        conversation_store,
        {},  # no label_defs at all
    )
    engine.apply_label_writes({"any": "value", "anything": "123"})
    # Both landed — no schema to enforce.
    assert engine.labels == {"any": "value", "anything": "123"}


def test_apply_label_writes_values_only_free_transitions(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`values` declared — enum check only, transitions between
    declared values are free."""
    engine = _build_engine_with_defs(
        conversation_store,
        {"role": LabelDef(values=["admin", "user", "guest"])},
        initial_labels={"role": "user"},
    )
    # Free transitions within the enum.
    engine.apply_label_writes({"role": "admin"})
    assert engine.labels["role"] == "admin"
    engine.apply_label_writes({"role": "guest"})
    assert engine.labels["role"] == "guest"
    # Out-of-enum still rejected.
    engine.apply_label_writes({"role": "root"})
    assert engine.labels["role"] == "guest"
