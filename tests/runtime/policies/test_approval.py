"""
Tests for :func:`_await_elicitation` and the verdict parser.

Ports these omnigent ``test_labels_and_policies.py`` cases:

- ``test_label_policy_ask_approve`` — accept round-trip
  applies set_labels
- ``test_label_policy_ask_handler_receives_tool_args`` —
  elicitation request carries the message + preview (our
  shape is :class:`ElicitationRequest` rather than raw
  tool_args)
- ``test_label_policy_ask_deny`` — decline leaves no writes
- ``test_ask_timeout`` — timeout → decline path
- ``test_ask_user_denies_not_timeout_message`` — decline reason
  distinguishable from timeout
- ``test_no_handler_denies`` — missing verdict row → DENY

Plus refactor-specific coverage:

- Strict verdict parsing (only ``action == "accept"`` returns
  True; everything else — ``decline`` / ``cancel`` / malformed /
  missing field — returns False per POLICIES.md §13).
- Per-policy ask_timeout override via
  ``result.deciding_policy`` lookup.
- Labels apply on accept, NOT on decline / cancel / timeout /
  malformed (load-bearing §7.2 invariant).
- Emitted SSE event shape matches MCP elicitation spec
  (``response.elicitation_request`` with ``method =
  "elicitation/create"`` + ``params`` block carrying mode /
  message / requestedSchema + producer extras).
- Content preview truncated to 1024 chars.
- Cancel-during-elicitation semantics (via park returning None
  with cancelled status).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import ElicitationRequest, PolicyResult
from omnigent.runtime.policies.approval import (
    ELICITATION_PENDING_TOOL_NAME,
    _await_elicitation,
    _parse_verdict,
    _truncate,
)
from omnigent.runtime.policies.approval import (
    build_elicitation_params_json as _params_json,
)
from omnigent.runtime.policies.approval import (
    build_elicitation_request_event as _elicitation_request_event,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.runtime.policies.conftest import make_fixed_policy

# ── Fixtures / helpers ────────────────────────────────


def _engine_with_policies(
    store: SqlAlchemyConversationStore,
    policies: list,
    ask_timeout: int = 30,
) -> PolicyEngine:
    """Build engine for tests that need `spec_for` to resolve."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=ask_timeout,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=store,
    )


def _ask_policy(
    name: str,
    *,
    ask_timeout: int | None = None,
    set_labels: dict[str, str] | None = None,
) -> FunctionPolicy:
    """Build an ASKing FunctionPolicy — the typical ASK source."""
    return make_fixed_policy(
        name=name,
        on=[PhaseSelector(phase=Phase.REQUEST)],
        ask_timeout=ask_timeout,
        action=PolicyAction.ASK,
        reason="review needed",
        set_labels=set_labels,
    )


def _composed_ask(
    *,
    deciding_policy: str,
    reason: str = "please approve",
    set_labels: dict[str, str] | None = None,
) -> PolicyResult:
    """Fabricate an engine-composed ASK result."""
    return PolicyResult(
        action=PolicyAction.ASK,
        reason=reason,
        set_labels=set_labels,
        deciding_policies=[deciding_policy],
    )


class _Recorder:
    """
    Test recorder for the register / emit callbacks.

    Makes it trivial to assert on what the elicitation helper
    published without touching a real SSE stream or store.
    """

    def __init__(self) -> None:
        self.registered: list[tuple[str, str, str]] = []
        self.emitted: list[dict[str, Any]] = []

    def register(self, elicitation_id: str, task_id: str, params_json: str) -> None:
        """Record one register() seam invocation.

        :param elicitation_id: Helper-generated id (``elicit_...``
            prefix).
        :param task_id: The parked workflow's id.
        :param params_json: JSON-encoded MCP params block.
        """
        self.registered.append((elicitation_id, task_id, params_json))

    def emit(self, event: dict[str, Any]) -> None:
        """Record one emit() seam invocation.

        :param event: The SSE event dict the helper would publish.
        """
        self.emitted.append(event)


def _accepting_park(verdict: str) -> Any:
    """Park callback that instantly returns the given verdict string.

    :param verdict: Pre-canned JSON-encoded :class:`ElicitationResult`
        body, e.g. ``'{"action": "accept"}'``.
    :returns: An async park-shaped callable.
    """

    async def _park(elicitation_id: str, timeout_s: int) -> str:
        return verdict

    return _park


def _timing_out_park() -> Any:
    """Park callback that always raises TimeoutError."""

    async def _park(elicitation_id: str, timeout_s: int) -> str:
        raise TimeoutError(f"no verdict within {timeout_s}s")

    return _park


def _returns_none_park() -> Any:
    """Park callback that returns None — cancelled or missing row."""

    async def _park(elicitation_id: str, timeout_s: int) -> str | None:
        return None

    return _park


# ── _parse_verdict ─────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"action": "accept"}', True),
        ('{"action": "decline"}', False),
        ('{"action": "cancel"}', False),
        ('{"action": "ACCEPT"}', False),  # strict — case matters
        ('{"action": "approved"}', False),  # not a valid action
        ('{"action": true}', False),  # wrong type
        ('{"action": null}', False),
        ("{}", False),  # missing action
        ('{"approved": true}', False),  # legacy shape — must NOT pass
        ("not json", False),
        ("", False),
        (None, False),
        ('[{"action": "accept"}]', False),  # non-dict root
        # ``content`` is allowed but ignored for binary verdicts —
        # ``action`` is the sole truth.
        ('{"action": "accept", "content": null}', True),
        ('{"action": "accept", "content": {"approved": true}}', True),
        ('{"action": "decline", "content": {"approved": true}}', False),
    ],
)
def test_parse_verdict_strict(raw: str | None, expected: bool) -> None:
    """Strict verdict parser: only ``action == "accept"`` returns
    True. Everything else (decline, cancel, malformed, missing
    field, legacy ``{"approved": ...}`` shape) returns False —
    fail-closed per POLICIES.md §13.

    The ``approved`` case exists specifically to guard against
    silently accepting the legacy shape if a stale client (or a
    misconfigured middleware) sends one. Under the new contract
    only ``action`` is meaningful; an ``approved`` field would
    miss the ``action`` requirement and correctly fail-closed."""
    assert _parse_verdict(raw) is expected


# ── _truncate ──────────────────────────────────────────


def test_truncate_short_passes() -> None:
    """Under-limit text returns unchanged."""
    assert _truncate("hi", limit=10) == "hi"


def test_truncate_long_clips_with_marker() -> None:
    """Over-limit text is clipped with an explicit marker
    so viewers can see truncation happened."""
    clipped = _truncate("x" * 100, limit=20)
    # First 20 chars of x, then the marker.
    assert clipped == "x" * 20 + " [truncated]"


# ── ElicitationRequest serialization ──────────────────


def test_params_json_serializes_all_fields() -> None:
    """Every field round-trips through JSON in the
    canonical MCP-shape ``params`` block (mode/message/
    requestedSchema + extras). The renderer reads
    these from both the SSE event ``params`` block AND
    the persisted ``pending_tool_calls.arguments`` column —
    they must agree."""
    req = ElicitationRequest(
        message="needs review",
        phase="tool_call",
        policy_name="confirm_shell",
        content_preview="ls -la",
    )
    data = json.loads(_params_json(req))
    # Top-level shape matches MCP ElicitRequestFormParams +
    # MCP-allowed extras (extra="allow" on the params model).
    assert data == {
        "mode": "form",
        "message": "needs review",
        # Empty {} for binary approve/reject — verdict lives
        # in the consumer's MCP ``action`` field.
        "requestedSchema": {},
        "phase": "tool_call",
        "policy_name": "confirm_shell",
        "content_preview": "ls -la",
    }


def test_elicitation_request_event_shape() -> None:
    """The SSE event payload has the canonical envelope
    (type/elicitation_id/method) and the MCP-shape params
    block. Locks the wire contract — drift here would
    silently break MCP-compatible consumers."""
    req = ElicitationRequest(
        message="needs review",
        phase="tool_call",
        policy_name="confirm_shell",
        content_preview="ls -la",
    )
    event = _elicitation_request_event("elicit_xyz", req)
    assert event["type"] == "response.elicitation_request"
    assert event["elicitation_id"] == "elicit_xyz"
    # Method name is the MCP JSON-RPC method, surfaced
    # verbatim so MCP-aware consumers can route on it.
    assert event["method"] == "elicitation/create"
    params = event["params"]
    # Field names must match MCP spec exactly: requestedSchema
    # (camelCase, not snake_case).
    assert params["mode"] == "form"
    assert params["message"] == "needs review"
    assert params["requestedSchema"] == {}
    # Extras (allowed by MCP's extra="allow") carry the
    # producer's policy context for the renderer.
    assert params["phase"] == "tool_call"
    assert params["policy_name"] == "confirm_shell"
    assert params["content_preview"] == "ls -la"


def test_elicitation_request_event_url_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``_ELICITATION_MODE`` is ``"url"`` (the default) and a
    ``session_id`` is provided, the event carries ``mode: "url"`` and
    the standalone approval page path as ``params.url``.

    This is the primary contract the frontend relies on to render a
    link instead of inline buttons.
    """
    monkeypatch.setattr("omnigent.runtime.policies.approval._ELICITATION_MODE", "url")
    req = ElicitationRequest(
        message="approve shell?",
        phase="tool_call",
        policy_name="shell_gate",
        content_preview="rm -rf /",
    )
    event = _elicitation_request_event("elicit_abc", req, session_id="conv_123")
    params = event["params"]
    assert params["mode"] == "url"
    assert params["url"] == "/approve/conv_123/elicit_abc"
    # Message and extras are still present.
    assert params["message"] == "approve shell?"


def test_elicitation_request_event_form_mode_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``_ELICITATION_MODE`` is ``"form"``, the event stays in
    form mode and carries no ``url`` field even when session_id is
    provided.
    """
    monkeypatch.setattr("omnigent.runtime.policies.approval._ELICITATION_MODE", "form")
    req = ElicitationRequest(
        message="approve?",
        phase="tool_call",
        policy_name="gate",
        content_preview="ls",
    )
    event = _elicitation_request_event("elicit_abc", req, session_id="conv_123")
    params = event["params"]
    assert params["mode"] == "form"
    assert "url" not in params


def test_elicitation_request_event_no_session_id_stays_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``session_id`` (runner-side calls), the event always
    uses form mode regardless of config — the runner doesn't serve
    HTML pages.
    """
    monkeypatch.setattr("omnigent.runtime.policies.approval._ELICITATION_MODE", "url")
    req = ElicitationRequest(
        message="approve?",
        phase="tool_call",
        policy_name="gate",
        content_preview="",
    )
    event = _elicitation_request_event("elicit_abc", req)
    params = event["params"]
    assert params["mode"] == "form"
    assert "url" not in params


# ── _await_elicitation — happy paths ──────────────────


@pytest.mark.asyncio
async def test_accept_applies_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent
    ``test_label_policy_ask_approve``. On accept, the
    ASK-accumulated set_labels reach the store."""
    policy = _ask_policy("gate", set_labels={"integrity": "0"})
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(
        deciding_policy="gate",
        set_labels={"integrity": "0"},
    )

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "accept"}'),
    )
    assert accepted is True
    # Labels landed — both hot cache and persisted.
    assert engine.labels == {"integrity": "0"}
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"integrity": "0"}


@pytest.mark.asyncio
async def test_decline_does_not_apply_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_label_policy_ask_deny``.
    On explicit decline, labels are NOT applied — the
    load-bearing §7.2 invariant that a denied ASK leaves
    no side effects."""
    policy = _ask_policy("gate", set_labels={"integrity": "0"})
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(
        deciding_policy="gate",
        set_labels={"integrity": "0"},
    )

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "decline"}'),
    )
    assert accepted is False
    # No labels landed — hot cache empty, store empty.
    assert engine.labels == {}
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {}


@pytest.mark.asyncio
async def test_cancel_does_not_apply_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``cancel`` (user dismissed without an explicit
    decision) is treated identically to ``decline`` for
    label-write semantics — no side effects."""
    policy = _ask_policy("gate", set_labels={"integrity": "0"})
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(
        deciding_policy="gate",
        set_labels={"integrity": "0"},
    )

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "cancel"}'),
    )
    assert accepted is False
    assert engine.labels == {}


@pytest.mark.asyncio
async def test_timeout_does_not_apply_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_ask_timeout``. Park raises
    TimeoutError → helper returns False without applying
    labels."""
    policy = _ask_policy("gate", set_labels={"integrity": "0"})
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(
        deciding_policy="gate",
        set_labels={"integrity": "0"},
    )

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_timing_out_park(),
    )
    assert accepted is False
    # Labels not applied on timeout.
    assert engine.labels == {}


@pytest.mark.asyncio
async def test_missing_verdict_row_denies(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_no_handler_denies``. Park
    returns None (cancelled / missing row) → helper returns
    False. Covers the cancel-during-elicitation path where
    the pending row was advanced to ``cancelled`` by the
    cancel handler (POLICIES.md §12)."""
    policy = _ask_policy("gate", set_labels={"integrity": "0"})
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="gate", set_labels={"integrity": "0"})

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_returns_none_park(),
    )
    assert accepted is False
    assert engine.labels == {}


@pytest.mark.asyncio
async def test_malformed_verdict_denies(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A verdict row with garbage ``output`` → helper returns
    False. The route stays a dumb pipe; verdict parsing
    fail-closes here (POLICIES.md §13 malformed-verdict
    rule)."""
    policy = _ask_policy("gate")
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="gate")

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="hello",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        # Garbage JSON → strict parser returns False.
        park=_accepting_park("banana garbage"),
    )
    assert accepted is False


# ── Register + emit payloads ──────────────────────────


@pytest.mark.asyncio
async def test_registers_pending_row(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The register callback receives the generated
    elicitation_id, the task_id, and the params JSON. These
    three fields are what the approval dispatcher uses to
    route a verdict back to the parked workflow."""
    policy = _ask_policy("gate")
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="gate", reason="please")

    await _await_elicitation(
        task_id="task_abc",
        root_task_id="task_abc",
        result=result,
        phase=Phase.TOOL_CALL,
        content_preview="ls -la",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "accept"}'),
    )
    # Exactly one row registered.
    assert len(recorder.registered) == 1
    elicitation_id, task_id, params_json = recorder.registered[0]
    # Elicitation_id has the expected prefix matching the
    # MCP-style id naming.
    assert elicitation_id.startswith("elicit_")
    # Task_id matches the parked workflow.
    assert task_id == "task_abc"
    # Params carry the MCP shape + producer extras.
    args = json.loads(params_json)
    assert args["mode"] == "form"
    # Combined ASK reason becomes the elicitation message.
    assert args["message"] == "please"
    assert args["phase"] == "tool_call"
    assert args["policy_name"] == "gate"
    assert args["content_preview"] == "ls -la"
    # Empty schema for binary approve/reject — verdict lives
    # in the consumer's MCP action.
    assert args["requestedSchema"] == {}


@pytest.mark.asyncio
async def test_emits_elicitation_request_event(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The emit callback receives a
    ``response.elicitation_request`` SSE event with MCP-shape
    params. This is what the SDK's _parse_event branch
    dispatches on, surfacing it as an ``ElicitationRequest``
    event upstream."""
    policy = _ask_policy("gate")
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="gate", reason="please review")

    await _await_elicitation(
        task_id="task_abc",
        root_task_id="task_abc",
        result=result,
        phase=Phase.REQUEST,
        content_preview="x",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "accept"}'),
    )
    assert len(recorder.emitted) == 1
    event = recorder.emitted[0]
    # Top-level envelope.
    assert event["type"] == "response.elicitation_request"
    assert event["method"] == "elicitation/create"
    # elicitation_id is consistent between register and emit.
    registered_id = recorder.registered[0][0]
    assert event["elicitation_id"] == registered_id
    # Params block is byte-compatible with MCP
    # ElicitRequestFormParams shape.
    params = event["params"]
    assert params["mode"] == "form"
    assert params["message"] == "please review"
    assert params["requestedSchema"] == {}
    # Producer extras ride along as per MCP extra="allow".
    assert params["phase"] == "request"
    assert params["policy_name"] == "gate"
    assert params["content_preview"] == "x"


# ── Per-policy ask_timeout override ───────────────────


@pytest.mark.asyncio
async def test_per_policy_ask_timeout_override_wins(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """When the deciding policy has its own ask_timeout,
    that value is passed to the park callback — not the
    engine's default. Enables long-review policies (e.g.
    50 KB documents) without bumping the global default."""
    captured: dict[str, int] = {}

    async def _capturing_park(elicitation_id: str, timeout_s: int) -> str:
        captured["timeout_s"] = timeout_s
        return '{"action": "accept"}'

    # Policy declares its own 300s timeout.
    policy = _ask_policy("long_review", ask_timeout=300)
    engine = _engine_with_policies(
        conversation_store,
        [policy],
        ask_timeout=30,
    )
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="long_review")

    await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="x",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_capturing_park,
    )
    # Per-policy 300 beat engine's 30.
    assert captured["timeout_s"] == 300


@pytest.mark.asyncio
async def test_engine_ask_timeout_default_when_no_override(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Without a per-policy override, the engine's spec-level
    default applies."""
    captured: dict[str, int] = {}

    async def _capturing_park(elicitation_id: str, timeout_s: int) -> str:
        captured["timeout_s"] = timeout_s
        return '{"action": "accept"}'

    policy = _ask_policy("gate", ask_timeout=None)
    engine = _engine_with_policies(
        conversation_store,
        [policy],
        ask_timeout=45,
    )
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="gate")

    await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="x",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_capturing_park,
    )
    # Engine's 45 used because policy didn't override.
    assert captured["timeout_s"] == 45


@pytest.mark.asyncio
async def test_unknown_deciding_policy_falls_back_to_engine_timeout(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """If deciding_policy is set to a name the engine
    doesn't know (shouldn't happen in production but
    defensive), fallback to the engine default."""
    captured: dict[str, int] = {}

    async def _capturing_park(elicitation_id: str, timeout_s: int) -> str:
        captured["timeout_s"] = timeout_s
        return '{"action": "accept"}'

    engine = _engine_with_policies(
        conversation_store,
        [],
        ask_timeout=60,
    )
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="nonexistent")

    await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="x",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_capturing_park,
    )
    # Unknown name → engine default.
    assert captured["timeout_s"] == 60


# ── Content preview truncation ────────────────────────


@pytest.mark.asyncio
async def test_content_preview_truncated_to_1024(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Long content previews are clipped so the UI is not
    swamped. 1024 is the chosen limit (POLICIES.md §7.2).
    The truncated value rides through both the persisted
    params JSON AND the SSE event params block — they
    derive from the same internal :class:`ElicitationRequest`
    so both stay in sync."""
    policy = _ask_policy("gate")
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    result = _composed_ask(deciding_policy="gate")

    await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="A" * 2000,
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "accept"}'),
    )
    args = json.loads(recorder.registered[0][2])
    preview = args["content_preview"]
    # Exactly 1024 chars + " [truncated]" suffix.
    assert preview.startswith("A" * 1024)
    assert preview.endswith(" [truncated]")
    # SSE event preview matches the persisted one.
    event_preview = recorder.emitted[0]["params"]["content_preview"]
    assert event_preview == preview


# ── No set_labels on result ───────────────────────────


@pytest.mark.asyncio
async def test_accept_with_no_set_labels_is_noop(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """An ASK result carrying no set_labels (empty/None) on
    accept does not touch the store — no pointless empty
    apply_label_writes call."""
    policy = _ask_policy("gate")
    engine = _engine_with_policies(conversation_store, [policy])
    recorder = _Recorder()
    # Result has no set_labels — a policy that just wants
    # approval without writing state.
    result = _composed_ask(deciding_policy="gate", set_labels=None)

    accepted = await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=Phase.REQUEST,
        content_preview="x",
        policy_engine=engine,
        register=recorder.register,
        emit=recorder.emit,
        park=_accepting_park('{"action": "accept"}'),
    )
    assert accepted is True
    # Store unchanged — no spurious empty writes.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {}


# ── Sentinel constant ─────────────────────────────────


def test_elicitation_pending_tool_name_is_internal_sentinel() -> None:
    """The pending row's ``tool_name`` column carries an
    internal sentinel (double-underscore prefix) — never an
    LLM-callable tool name. Locks the value so a rename
    breaks loud (the approval dispatcher's row-routing depends
    on this exact string)."""
    assert ELICITATION_PENDING_TOOL_NAME == "__elicitation__"
    # Sentinel must look internal so a tool_results PATCH
    # accidentally targeting an elicitation row would be
    # easy to spot in logs.
    assert ELICITATION_PENDING_TOOL_NAME.startswith("__")
