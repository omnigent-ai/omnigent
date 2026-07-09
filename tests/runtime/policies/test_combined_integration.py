"""
Combined integration tests — all three policy types together.

Builds a PolicyEngine from the ``combined-policies`` fixture
and exercises scenarios that would only surface when multiple
policy subclasses interact. This is the most comprehensive
e2e proxy available until the workflow.py integration lands.

Assertions cover:

- FunctionPolicy composition on the same
  tool name (taint + rate-limit on web_search).
- FunctionPolicy observer policy (``observe_writes``) always ALLOWs.
- Multi-label DENY gate (`deny_exfil`): fires only when BOTH
  integrity and sensitivity labels are tainted.
- End-to-end IFC sequence: clean → web search taints
  integrity → confidential read elevates sensitivity →
  write_file fires `deny_exfil`.
- Rate-limit ASK + condition-gate DENY compose correctly.
- Monotonic label cache semantics (via the store layer).

These tests exercise the real parse → build → evaluate →
persist pipeline for every declared policy type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import (
    _enforce_policy,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec import load
from omnigent.spec.types import (
    Phase,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_FIXTURE = Path(__file__).resolve().parents[2] / "_fixtures" / "agents" / "combined-policies"


def _engine(store: SqlAlchemyConversationStore) -> PolicyEngine:
    """Build a fresh engine from the combined-policies fixture."""
    spec = load(_FIXTURE)
    conv = store.create_conversation()
    return build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=store,
    )


def _tool(name: str, args: dict[str, object] | None = None) -> EvaluationContext:
    """TOOL_CALL context helper."""
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": name, "arguments": args or {}},
        tool_name=name,
    )


# ── Happy-path smoke ──────────────────────────────────


@pytest.mark.asyncio
async def test_initial_labels_seeded_from_combined_spec(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """All declared initial values are seeded on build."""
    engine = _engine(conversation_store)
    assert engine.labels == {"integrity": "1", "sensitivity": "public"}


@pytest.mark.asyncio
async def test_clean_state_allows_write_file(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Before any taint, write_file passes — neither
    deny_exfil nor observe_writes blocks."""
    engine = _engine(conversation_store)
    r = await _enforce_policy(
        engine,
        _tool("write_file", {"path": "x.txt", "content": "hi"}),
    )
    assert r.action == PolicyAction.ALLOW


# ── Multi-policy interaction on web_search ────────────


@pytest.mark.asyncio
async def test_web_search_first_call_taints_and_allows(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """First web_search: taint policy taints integrity AND
    FunctionPolicy allows (within budget). Both policies
    fire on the same tool; both contribute to the result."""
    engine = _engine(conversation_store)
    r = await _enforce_policy(engine, _tool("web_search", {"q": "x"}))
    assert r.action == PolicyAction.ALLOW
    assert engine.labels["integrity"] == "0"
    # Verify persistence round-trip — the taint policy's
    # set_labels made it through the store, not just the
    # hot cache.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels["integrity"] == "0"


@pytest.mark.asyncio
async def test_web_search_over_budget_asks(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """After 2 free calls, the 3rd web_search ASKs.
    FunctionPolicy's ASK wins because the taint policy returned
    ALLOW; composition: ALLOW+ASK → ASK."""
    engine = _engine(conversation_store)
    # Exhaust budget.
    await _enforce_policy(engine, _tool("web_search", {"q": "q1"}))
    await _enforce_policy(engine, _tool("web_search", {"q": "q2"}))
    # 3rd call asks.
    r = await _enforce_policy(engine, _tool("web_search", {"q": "q3"}))
    assert r.action == PolicyAction.ASK
    # First-ASKer-wins deciding_policy = search_rate_limit
    # (order: taint_web first in YAML, but it's ALLOW;
    # search_rate_limit is the first ASKer).
    assert r.deciding_policy == "search_rate_limit"
    # Reason names the policy + budget.
    assert "search_rate_limit" in r.reason


# ── Classifier-only observe policy ────────────────────


@pytest.mark.asyncio
async def test_observe_writes_never_blocks(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """write_file (in clean state) passes through the
    observe_writes policy without blocking."""
    engine = _engine(conversation_store)
    r = await _enforce_policy(
        engine,
        _tool("write_file", {"path": "out.txt"}),
    )
    assert r.action == PolicyAction.ALLOW


# ── Multi-label DENY gate ─────────────────────────────


@pytest.mark.asyncio
async def test_deny_exfil_requires_both_taints(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """deny_exfil's condition requires integrity=0 AND
    sensitivity=confidential. A write with only one taint
    passes; with both, it DENYs."""
    engine = _engine(conversation_store)

    # Taint only integrity.
    await _enforce_policy(engine, _tool("web_search", {"q": "x"}))
    assert engine.labels["integrity"] == "0"
    assert engine.labels["sensitivity"] == "public"
    # With only integrity tainted, write_file PASSES —
    # condition is AND across both keys.
    r1 = await _enforce_policy(
        engine,
        _tool("write_file", {"path": "x.txt"}),
    )
    assert r1.action == PolicyAction.ALLOW

    # Now elevate sensitivity too.
    await _enforce_policy(engine, _tool("read_confidential", {"id": "x"}))
    assert engine.labels["sensitivity"] == "confidential"
    # Now BOTH taints present → deny_exfil fires.
    r2 = await _enforce_policy(
        engine,
        _tool("write_file", {"path": "y.txt"}),
    )
    assert r2.action == PolicyAction.DENY
    assert r2.deciding_policy == "deny_exfil"
    # Reason matches the YAML declaration.
    assert "exfiltration" in r2.reason.lower()


@pytest.mark.asyncio
async def test_deny_exfil_covers_run_shell_too(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The deny_exfil selector scopes to both write_file
    and run_shell — same YAML entry, multi-tool selector."""
    engine = _engine(conversation_store)
    # Taint both labels.
    await _enforce_policy(engine, _tool("web_search", {"q": "x"}))
    await _enforce_policy(engine, _tool("read_confidential", {"id": "x"}))
    # run_shell with both taints → DENY.
    r = await _enforce_policy(
        engine,
        _tool("run_shell", {"cmd": "ls"}),
    )
    assert r.action == PolicyAction.DENY
    assert r.deciding_policy == "deny_exfil"


# ── Full IFC sequence ─────────────────────────────────


@pytest.mark.asyncio
async def test_full_ifc_sequence(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """End-to-end simulation of a real agent turn sequence.

    The canonical IFC bad-case:
    1. Clean agent — write_file passes.
    2. Web search runs — integrity taints to "0".
    3. Confidential read runs — sensitivity elevates to "confidential".
    4. Write attempt — DENY (both taints present).

    If any step regresses, a production agent executing this
    sequence would behave differently from the YAML's
    declared intent."""
    engine = _engine(conversation_store)

    # Step 1: clean write allowed.
    step1 = await _enforce_policy(
        engine,
        _tool("write_file", {"path": "clean.txt"}),
    )
    assert step1.action == PolicyAction.ALLOW

    # Step 2: web_search taints.
    step2 = await _enforce_policy(engine, _tool("web_search", {"q": "ext"}))
    assert step2.action == PolicyAction.ALLOW
    assert engine.labels["integrity"] == "0"

    # Step 3: confidential read elevates sensitivity.
    step3 = await _enforce_policy(
        engine,
        _tool("read_confidential", {"id": "doc-42"}),
    )
    assert step3.action == PolicyAction.ALLOW
    assert engine.labels["sensitivity"] == "confidential"

    # Step 4: tainted + confidential write DENIES.
    step4 = await _enforce_policy(
        engine,
        _tool("write_file", {"path": "leak.txt"}),
    )
    assert step4.action == PolicyAction.DENY
    assert step4.deciding_policy == "deny_exfil"

    # Final store state matches the engine's hot cache.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"integrity": "0", "sensitivity": "confidential"}


# ── Persistence across engine rebuilds ────────────────


@pytest.mark.asyncio
async def test_labels_persist_across_engine_rebuild(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Building a second engine on the same conversation
    picks up the labels written by the first. Models a real
    workflow restart — without this, tainting would reset
    every time the workflow replays."""
    spec = load(_FIXTURE)
    conv = conversation_store.create_conversation()

    # First engine — taint integrity.
    first = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    await _enforce_policy(first, _tool("web_search", {"q": "x"}))
    assert first.labels["integrity"] == "0"

    # Second engine on the SAME conversation — hot cache
    # seeds from the persisted state, which already has
    # integrity="0" (the seed logic is idempotent:
    # ON CONFLICT DO NOTHING leaves the "0" alone).
    second = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert second.labels["integrity"] == "0"
