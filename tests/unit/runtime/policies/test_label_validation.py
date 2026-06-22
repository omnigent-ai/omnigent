"""
Tests for :meth:`PolicyEngine.apply_label_writes` schema
validation (POLICIES.md §10 / §13).

Silent-drop semantics:

- Key not in ``LabelDef.values`` → dropped.
- Key violates ``monotonic`` direction → dropped.
- Unknown key (no LabelDef) → set freely.
- Valid write → persisted via the store.

The drop path is silent by design (matches omnigent) —
a runtime validation failure does NOT raise. The surviving
writes still land atomically.

Ports omnigent ``test_labels_and_policies.py``:
- test_engine_enforces_root_label_schema_monotonicity
- test_invalid_initial_label_value_rejected_by_schema
  (handled at spec-load in parser tests — this file covers
  the runtime post-seed write path)
"""

from __future__ import annotations

from omnigent.runtime.policies.engine import (
    PolicyEngine,
    _merge_monotonic_writes,
    _monotonic_ok,
)
from omnigent.spec.types import LabelDef
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# ── _monotonic_ok unit tests ──────────────────────────


def test_monotonic_ok_unset_label_allows_any_value() -> None:
    """Seeding from None passes — nothing to compare yet."""
    ldef = LabelDef(values=["0", "1"], monotonic="increasing")
    assert _monotonic_ok(ldef, None, "0") is True
    assert _monotonic_ok(ldef, None, "1") is True


def test_monotonic_ok_increasing_accepts_equal_or_greater() -> None:
    """Increasing: new index >= current index."""
    ldef = LabelDef(values=["0", "1", "2"], monotonic="increasing")
    # 0 → 1 ok
    assert _monotonic_ok(ldef, "0", "1") is True
    # 0 → 0 ok (equal)
    assert _monotonic_ok(ldef, "0", "0") is True
    # 2 → 0 rejected (decrease)
    assert _monotonic_ok(ldef, "2", "0") is False
    # 1 → 0 rejected
    assert _monotonic_ok(ldef, "1", "0") is False


def test_monotonic_ok_decreasing_accepts_equal_or_less() -> None:
    """Decreasing: new index <= current index."""
    ldef = LabelDef(values=["0", "1"], monotonic="decreasing")
    # 1 → 0 ok
    assert _monotonic_ok(ldef, "1", "0") is True
    # 1 → 1 ok
    assert _monotonic_ok(ldef, "1", "1") is True
    # 0 → 1 rejected
    assert _monotonic_ok(ldef, "0", "1") is False


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


def test_apply_label_writes_drops_monotonic_violation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Violating monotonic direction → silent drop. The
    taint-clearing safety property for IFC: once integrity
    drops to "0" (decreasing monotonic), attempts to set it
    back to "1" are rejected.

    If this regresses, a malicious or broken policy could
    silently untaint the session — defeating the entire
    IFC design."""
    engine = _build_engine_with_defs(
        conversation_store,
        {"integrity": LabelDef(values=["0", "1"], monotonic="decreasing")},
        initial_labels={"integrity": "1"},
    )
    # Legal: 1 → 0 (decreasing allowed).
    engine.apply_label_writes({"integrity": "0"})
    assert engine.labels["integrity"] == "0"

    # Illegal: 0 → 1 (attempted INCREASE on decreasing
    # monotonic). Dropped.
    engine.apply_label_writes({"integrity": "1"})
    # Still "0" — the "1" attempt was rejected.
    assert engine.labels["integrity"] == "0"

    # Persisted state reflects the drop.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels["integrity"] == "0"


def test_apply_label_writes_drops_increasing_violation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Symmetric case: increasing monotonic rejects
    decreases."""
    engine = _build_engine_with_defs(
        conversation_store,
        {
            "sensitivity": LabelDef(
                values=["public", "internal", "confidential"],
                monotonic="increasing",
            ),
        },
        initial_labels={"sensitivity": "internal"},
    )
    # internal → confidential: increase → allowed.
    engine.apply_label_writes({"sensitivity": "confidential"})
    assert engine.labels["sensitivity"] == "confidential"

    # confidential → public: decrease → dropped.
    engine.apply_label_writes({"sensitivity": "public"})
    # Still confidential.
    assert engine.labels["sensitivity"] == "confidential"


def test_apply_label_writes_partial_batch_survives(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """One key in a multi-key batch violates the schema;
    OTHER keys still land. Silent-drop is per-key, not
    all-or-nothing."""
    engine = _build_engine_with_defs(
        conversation_store,
        {
            "integrity": LabelDef(values=["0", "1"], monotonic="decreasing"),
            "other": LabelDef(values=["a", "b"]),
        },
        initial_labels={"integrity": "0"},
    )
    # integrity 0→1 violates decreasing (drop); other
    # "a" is valid (land).
    engine.apply_label_writes({"integrity": "1", "other": "a"})
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


def test_apply_label_writes_values_only_no_monotonic(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`values` declared without `monotonic` → enum check
    only, transitions between declared values are free."""
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


# ── _merge_monotonic_writes unit tests ────────────────────


def test_merge_increasing_keeps_higher_index_when_existing_lower() -> None:
    """
    Two policies write the same monotonic-increasing key in
    one ``evaluate()`` call. The accumulator must end up with
    the higher-indexed value regardless of YAML order — the
    "labels only move upwards" invariant should not depend on
    which policy fires first.

    Without :func:`_merge_monotonic_writes`, ``dict.update``
    last-write-wins lets a smaller value silently overwrite
    a larger predecessor and the ``apply_label_writes``
    schema check sees only the smaller write.
    """
    label_defs = {
        "integrity": LabelDef(values=["0", "1", "2"], monotonic="increasing"),
    }
    accumulated: dict[str, str] = {}
    # Earlier write goes through unchallenged.
    _merge_monotonic_writes(accumulated, {"integrity": "2"}, label_defs)
    assert accumulated == {"integrity": "2"}
    # Later write of a SMALLER value MUST NOT win — that
    # would let a later policy silently lower the taint
    # level a prior policy raised.
    _merge_monotonic_writes(accumulated, {"integrity": "1"}, label_defs)
    assert accumulated == {"integrity": "2"}, (
        f"Expected '2' (higher) to win over '1' (lower) for "
        f"monotonic=increasing; got {accumulated!r}. The "
        f"merge regressed to last-write-wins, breaking "
        f"kasey_uhlenhuth bug #6's multi-policy invariant."
    )


def test_merge_increasing_accepts_higher_when_existing_lower() -> None:
    """
    Mirror of the above: an earlier policy wrote a low value,
    a later one writes a higher value. Higher wins because
    that's the constraint direction.
    """
    label_defs = {
        "integrity": LabelDef(values=["0", "1", "2"], monotonic="increasing"),
    }
    accumulated = {"integrity": "0"}
    _merge_monotonic_writes(accumulated, {"integrity": "2"}, label_defs)
    assert accumulated == {"integrity": "2"}


def test_merge_decreasing_keeps_lower_index() -> None:
    """
    Symmetric monotonic=decreasing: among multiple writes in
    one evaluation, the lower-indexed value wins. A later
    policy attempting to "undo" the decrease by writing a
    higher value must not silently overwrite the lower one.

    Exercises the FALSE branch of the decreasing comparison —
    new_idx (1) is NOT less than existing_idx (0), so existing
    is preserved. The mirror test below covers the TRUE branch.
    """
    label_defs = {
        "trust": LabelDef(values=["low", "med", "high"], monotonic="decreasing"),
    }
    accumulated = {"trust": "low"}
    # "med" is index 1, "low" is index 0 → "low" wins for decreasing.
    _merge_monotonic_writes(accumulated, {"trust": "med"}, label_defs)
    assert accumulated == {"trust": "low"}


def test_merge_decreasing_accepts_lower_when_existing_higher() -> None:
    """
    Mirror of the above: an earlier policy wrote a HIGHER
    value (e.g. "high"); a later policy writes a LOWER value
    (e.g. "low"). Lower wins for decreasing direction —
    that's the constraint's whole point ("once moved down,
    cannot move back up"). This test exercises the TRUE
    branch of the decreasing comparison; without it, a
    refactor that swaps the comparison sense would only fail
    half the cases.
    """
    label_defs = {
        "trust": LabelDef(values=["low", "med", "high"], monotonic="decreasing"),
    }
    accumulated = {"trust": "high"}
    # "low" is index 0, "high" is index 2 → "low" wins for decreasing.
    _merge_monotonic_writes(accumulated, {"trust": "low"}, label_defs)
    assert accumulated == {"trust": "low"}


def test_merge_schemaless_uses_last_write_wins() -> None:
    """
    A key with no ``LabelDef`` (schemaless / unconstrained)
    keeps the historical last-write-wins behaviour. There is
    no direction to honour, so the choice is arbitrary; we
    pick "later wins" for backwards-compat with callers that
    might depend on the override semantics.
    """
    accumulated = {"note": "first"}
    _merge_monotonic_writes(accumulated, {"note": "second"}, label_defs={})
    assert accumulated == {"note": "second"}


def test_merge_no_monotonic_with_values_uses_last_write_wins() -> None:
    """
    A key with ``values`` declared but no monotonic direction
    is unconstrained on transitions — last write wins. Only
    direction-aware schemas (``increasing``/``decreasing``)
    cause the merge to pick a specific side.
    """
    label_defs = {"role": LabelDef(values=["admin", "user", "guest"])}
    accumulated = {"role": "admin"}
    _merge_monotonic_writes(accumulated, {"role": "user"}, label_defs)
    assert accumulated == {"role": "user"}


def test_merge_inserts_new_key_unconditionally() -> None:
    """
    A key not yet in the accumulator is inserted regardless of
    schema. Only same-key conflicts go through the
    direction-aware path.
    """
    label_defs = {
        "integrity": LabelDef(values=["0", "1"], monotonic="increasing"),
    }
    accumulated: dict[str, str] = {}
    _merge_monotonic_writes(accumulated, {"integrity": "1"}, label_defs)
    assert accumulated == {"integrity": "1"}


def test_merge_is_order_independent_for_monotonic_keys() -> None:
    """
    Composing the same set of writes in opposite orders must
    produce the same accumulator state for monotonic keys —
    the merge is commutative on direction-aware keys. Pinned
    so a future rewrite that re-introduces order-dependent
    semantics fails this test loud.
    """
    label_defs = {
        "integrity": LabelDef(values=["0", "1", "2"], monotonic="increasing"),
    }

    forward: dict[str, str] = {}
    _merge_monotonic_writes(forward, {"integrity": "2"}, label_defs)
    _merge_monotonic_writes(forward, {"integrity": "1"}, label_defs)

    reverse: dict[str, str] = {}
    _merge_monotonic_writes(reverse, {"integrity": "1"}, label_defs)
    _merge_monotonic_writes(reverse, {"integrity": "2"}, label_defs)

    assert forward == reverse == {"integrity": "2"}, (
        f"Merge should be order-independent for monotonic "
        f"keys; got forward={forward!r}, reverse={reverse!r}. "
        f"Order dependence is the original kasey #6 symptom."
    )


def test_merge_out_of_enum_value_falls_back_to_last_write() -> None:
    """
    If either side of the merge is outside ``ldef.values``,
    the merge can't compare indices safely — fall through to
    last-write-wins. The schema check at apply time
    (``_filter_schema_valid``) drops the out-of-enum value,
    so the choice doesn't change end persistence; this branch
    just keeps the merge robust against malformed input.
    """
    label_defs = {
        "integrity": LabelDef(values=["0", "1"], monotonic="increasing"),
    }
    # Existing is in-enum, new is not — defer to schema filter.
    accumulated = {"integrity": "1"}
    _merge_monotonic_writes(accumulated, {"integrity": "rogue"}, label_defs)
    assert accumulated == {"integrity": "rogue"}
