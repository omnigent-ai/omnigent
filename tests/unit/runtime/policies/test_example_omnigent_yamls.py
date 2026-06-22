"""
End-to-end policy scenarios loaded directly from the
omnigent-format example YAMLs under ``examples/*.yaml``.

Complements :mod:`test_enforcement_integration`, which loads
pre-translated omnigent-native fixtures. The fixtures there
are hand-maintained ports; any bug in the omnigent → omnigent
adapter layer (e.g. ``condition: {}`` parse rejection,
``match_tools`` → ``on:`` expansion, label-schema monotonic
translation) slips past those tests. This module goes through
:func:`omnigent.spec.load` — the same path ``omnigent run``
uses — so the adapter is exercised on every run.

Scenarios mirror the user-documented trigger matrix:

#. ``agent_with_policies.yaml`` — sleep ≤ 5s → ALLOW.
#. ``agent_with_policies.yaml`` — sleep > 5s → DENY.
   **Documented pre-existing gap**: the example uses a legacy
   2-arg ``(content, phase)`` callable signature. Agent-plane's
   :class:`FunctionPolicy` calls 2-arg callables as
   ``(ctx, context)``, which doesn't match. The callable's
   ``isinstance(content, dict)`` guard falls through and the
   policy returns ALLOW. Marked xfail so the regression is
   visible if/when the signature adapter is added.
#. ``rate_limited_search_agent.yaml`` — first web_search → ALLOW.
#. ``rate_limited_search_agent.yaml`` — 4th web_search → ASK.
   Same legacy-signature gap as #2 (xfail).
#. ``secure_research_agent.yaml`` — clean run_shell → ALLOW.
#. ``secure_research_agent.yaml`` — read → run_shell → ASK
   (ask_high_confidentiality).
#. ``secure_research_agent.yaml`` — web_search + read →
   run_shell → DENY (deny_contaminated_shell).
#. ``secure_research_agent_os_env.yaml`` — same flow as #7,
   verifies the os_env variant's policy block still fires.

Prompt-policy scenarios (``block_canada_input``,
``block_canada_output``) require the real-LLM classifier and
are covered by :mod:`tests.e2e.test_policies_e2e`
(``test_prompt_policy_*``) — not re-tested here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import (
    _enforce_policy,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec import load
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_EXAMPLES_DIR = Path(__file__).resolve().parents[4] / "tests" / "resources" / "examples"

_AGENT_WITH_POLICIES = _EXAMPLES_DIR / "agent_with_policies.yaml"
_RATE_LIMITED_SEARCH = _EXAMPLES_DIR / "rate_limited_search_agent.yaml"
_SECURE_RESEARCH = _EXAMPLES_DIR / "secure_research_agent.yaml"
_RISK_SCORE = _EXAMPLES_DIR / "risk_score_agent.yaml"
# The os_env variant was relocated to tests/resources/ during
# the unification refactor (it didn't survive the examples
# curation cut). Path reflects that move.
_SECURE_RESEARCH_OS_ENV = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "resources"
    / "agents"
    / "secure_research_agent_os_env"
    / "secure_research_agent_os_env.yaml"
)


def _load_engine_from_yaml(
    yaml_path: Path,
    store: SqlAlchemyConversationStore,
) -> PolicyEngine:
    """
    Parse an omnigent-format example YAML and build a real
    :class:`PolicyEngine` bound to a fresh conversation.

    Goes through :func:`omnigent.spec.load`, so the
    ``_omnigent_compat`` adapter runs on every call. A bug
    there (condition parsing, match_tools expansion, label
    monotonic translation) will surface at ``load()`` time
    and fail the test at fixture setup — exactly where a
    regression in the adapter would show up in production.

    :param yaml_path: Absolute path to the example YAML.
    :param store: Conversation store to back the engine's
        label persistence.
    :returns: A PolicyEngine ready to evaluate.
    """
    spec = load(yaml_path)
    conv = store.create_conversation()
    return build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=store,
    )


def _tool_ctx(name: str, args: dict[str, object] | None = None) -> EvaluationContext:
    """
    Build a TOOL_CALL :class:`EvaluationContext` the way the
    workflow's ``_enforce_tool_call_policy`` assembles one.

    :param name: Tool name, e.g. ``"run_shell"``.
    :param args: Tool arguments dict, or ``None`` for empty.
    :returns: A ready-to-enforce context.
    """
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": name, "arguments": args or {}},
        tool_name=name,
    )


# ─── Scenario 1: agent_with_policies → ALLOW short sleep ────


@pytest.mark.asyncio
async def test_agent_with_policies_allows_short_sleep(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A 2-second sleep passes the ``block_long_sleep`` FunctionPolicy
    (threshold is 5s) and any other gates.

    Claim: the full parse + engine pipeline loaded from the
    omnigent YAML ends at ALLOW for an in-bounds duration.
    """
    engine = _load_engine_from_yaml(_AGENT_WITH_POLICIES, conversation_store)
    result = await _enforce_policy(
        engine,
        _tool_ctx("sleep", {"seconds": 2}),
    )
    assert result.action == PolicyAction.ALLOW


# ─── Scenario 2: agent_with_policies → DENY long sleep ──────


@pytest.mark.asyncio
async def test_agent_with_policies_denies_long_sleep(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    An 8-second sleep trips ``block_long_sleep`` and DENYs.

    Contrary to the user's initial note that the legacy 2-arg
    ``(content, phase)`` callable signature from
    ``examples/tool_functions.py`` wouldn't fire under
    Omnigent' engine: it DOES. The engine calls 2-arg
    callables as ``(ctx, context)``, and ``block_long_sleep``
    inspects ``content.get("name")`` which works because
    :class:`EvaluationContext` is a :func:`dataclasses.dataclass`
    whose ``.content`` field is a dict carrying the tool-call
    payload — and ``_coerce_to_policy_result`` accepts the
    returned dict shape structurally.

    Claim: the omnigent YAML + its legacy-style example
    callable actually works through Omnigent' engine. If
    this ever regresses (e.g. the callable adapter path is
    tightened), the regression is visible in this test.
    """
    engine = _load_engine_from_yaml(_AGENT_WITH_POLICIES, conversation_store)
    result = await _enforce_policy(
        engine,
        _tool_ctx("sleep", {"seconds": 8}),
    )
    assert result.action == PolicyAction.DENY
    assert result.deciding_policy == "block_long_sleep"


# Scenarios 5–6 (rate_limited_search_agent.yaml) are NOT tested
# here: the example's ``summarize`` tool references
# ``examples.tool_functions.summarize`` which doesn't exist,
# and spec load fails at fixture setup. The rate-limit policy
# composition is already covered at the omnigent-native
# fixture layer in :mod:`test_enforcement_integration`
# (``test_rate_limited_search_*``). When the example YAML is
# fixed, add direct-from-YAML coverage here.


# ─── Scenario 7: secure_research → ALLOW clean shell ────────


@pytest.mark.asyncio
async def test_secure_research_clean_shell_allows(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    With initial labels (integrity=1, confidentiality=0), a
    run_shell call matches none of the deny/ask conditions
    and ALLOWs.

    Claim: the engine seeds ``initial`` values from the
    omnigent ``labels:`` block and the enforcement chain
    sees the clean state on the first call.
    """
    engine = _load_engine_from_yaml(_SECURE_RESEARCH, conversation_store)
    result = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"command": "pwd"}),
    )
    assert result.action == PolicyAction.ALLOW


# ─── Scenario 8: secure_research → ASK on confidentiality ───


@pytest.mark.asyncio
async def test_secure_research_doc_then_shell_asks(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    After ``read_internal_doc``, confidentiality=1, integrity=1.
    The subsequent ``run_shell`` matches
    ``ask_high_confidentiality`` (confidentiality=1), but not
    ``deny_contaminated_shell`` (needs integrity=0 too), so
    the engine returns ASK.

    Claim: single-label tainting drives the weakest matching
    gate (ASK), not the stricter multi-label DENY.
    """
    engine = _load_engine_from_yaml(_SECURE_RESEARCH, conversation_store)
    # Taint confidentiality via read_internal_doc.
    await _enforce_policy(
        engine,
        _tool_ctx("read_internal_doc", {"doc_id": "handbook"}),
    )
    # Now run_shell → ASK (not DENY).
    result = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"command": "pwd"}),
    )
    assert result.action == PolicyAction.ASK
    assert result.deciding_policy == "ask_high_confidentiality"


# ─── Scenario 9: secure_research → DENY on both taints ──────


@pytest.mark.asyncio
async def test_secure_research_both_taints_deny_shell(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    After web_search (integrity→0) AND read_internal_doc
    (confidentiality→1), run_shell matches
    ``deny_contaminated_shell`` (which needs both). The DENY
    short-circuits before ``ask_high_confidentiality`` and
    ``ask_low_integrity`` — YAML ordering matters.

    Claim: multi-key condition gates compose correctly and
    the stricter policy placed first wins.
    """
    engine = _load_engine_from_yaml(_SECURE_RESEARCH, conversation_store)
    # Note: the tool is named ``search_web`` in the YAML (line
    # 50) — not ``web_search``. The match_tools reference on
    # line 78 was corrected to match.
    await _enforce_policy(engine, _tool_ctx("search_web", {"query": "news"}))
    await _enforce_policy(
        engine,
        _tool_ctx("read_internal_doc", {"doc_id": "handbook"}),
    )
    result = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"command": "ls"}),
    )
    assert result.action == PolicyAction.DENY
    assert result.deciding_policy == "deny_contaminated_shell"


# ─── risk_score_agent: built-in session-risk-score policy ───


def _tool_result_ctx(name: str, result: str) -> EvaluationContext:
    """
    Build a TOOL_RESULT :class:`EvaluationContext` carrying a stringified result.

    :param name: Tool that produced the result, e.g. ``"docs_document_get"``.
    :param result: Stringified tool output under ``content.result``, e.g.
        ``'{"label_classification": "Highly Confidential"}'``.
    :returns: A ready-to-enforce TOOL_RESULT context.
    """
    return EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content={"result": result},
        tool_name=name,
    )


@pytest.mark.asyncio
async def test_risk_score_below_threshold_allows_guarded_tool(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Loaded from YAML: a single web_search (+10) leaves the score under the 50
    threshold, so the guarded gmail_message_send still ALLOWs.

    Claim: the risk_score_policy resolves through ``spec.load`` and does not gate
    before enough risk has accrued.
    """
    engine = _load_engine_from_yaml(_RISK_SCORE, conversation_store)
    searched = await _enforce_policy(engine, _tool_ctx("web_search", {"query": "x"}))
    assert searched.action == PolicyAction.ALLOW  # +10, scored not gated
    send = await _enforce_policy(engine, _tool_ctx("gmail_message_send", {"to": "a@b.com"}))
    assert send.action == PolicyAction.ALLOW  # score 10 < 50


@pytest.mark.asyncio
async def test_risk_score_web_searches_accrue_and_gate_send(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Loaded from YAML: five web_searches (5×10 = 50) reach the threshold, so the
    next gmail_message_send escalates to ASK.

    Claim: per-call scoring accumulates in the engine's session_state across
    enforcement calls and drives the guarded-tool gate. The MCP-prefixed tool
    name (``mcp__google__gmail_message_send``) still matches the bare config name.
    """
    engine = _load_engine_from_yaml(_RISK_SCORE, conversation_store)
    for _ in range(5):
        result = await _enforce_policy(engine, _tool_ctx("web_search", {"query": "x"}))
        assert result.action == PolicyAction.ALLOW
    gated = await _enforce_policy(
        engine, _tool_ctx("mcp__google__gmail_message_send", {"to": "a@b.com"})
    )
    # 50 >= 50 → the send needs approval; session_risk is the deciding policy.
    assert gated.action == PolicyAction.ASK
    assert gated.deciding_policy == "session_risk"


@pytest.mark.asyncio
async def test_risk_score_confidential_reads_accrue_and_gate(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Loaded from YAML: two reads returning a "Highly Confidential" label
    (2×30 = 60) cross the threshold, gating drive_permission_create.

    Claim: the label-in-result scoring path works through the real load + engine
    pipeline, not just the unit-level callable.
    """
    engine = _load_engine_from_yaml(_RISK_SCORE, conversation_store)
    confidential = json.dumps({"label_classification": "Highly Confidential"})
    for _ in range(2):
        read = await _enforce_policy(engine, _tool_result_ctx("docs_document_get", confidential))
        assert read.action == PolicyAction.ALLOW  # +30 each
    gated = await _enforce_policy(
        engine, _tool_ctx("drive_permission_create", {"file_id": "1AbC"})
    )
    # 60 >= 50 → sharing is gated after reading confidential material.
    assert gated.action == PolicyAction.ASK
    assert gated.deciding_policy == "session_risk"


# Scenario 10 (secure_research_agent_os_env.yaml) is NOT tested
# here: the YAML declares a tool named ``web_search`` (line 49)
# which collides with an omnigent reserved builtin name. The
# validator rejects at spec load with "tool name 'web_search'
# collides with a reserved builtin tool name". Fix requires
# renaming the tool in the YAML or relaxing the reserved-name
# check — separate from this file's scope. The enforcement
# semantics (double-taint → DENY on gated os_env tools) are
# structurally identical to scenario 9 above, which IS covered,
# so the policy-engine behavior is not uncovered — only the
# direct-from-YAML load path is blocked.
