"""Tests for server-side policy evaluation (steps 5.5 and 5.6).

Verifies that ``POST /v1/sessions/{id}/events`` evaluates tool
calls (``function_call`` with ``evaluate_policy: true``) and
user input (``message`` with ``role: "user"``) against the agent
spec's guardrails and returns the correct verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.entities import Conversation, ConversationItem
from omnigent.entities.agent import Agent, LoadedAgent
from omnigent.entities.conversation import FunctionCallData
from omnigent.entities.policy import Policy as StoredPolicy
from omnigent.policies.types import PolicyAction, PolicyResult
from omnigent.server.routes.sessions import (
    _build_evaluation_context,
    _build_skill_slash_command_policy_body,
    _evaluate_input_policy,
    _evaluate_tool_call_policy,
    _persist_policy_deny_sentinel,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.spec import AgentSpec
from omnigent.spec.types import Phase, PolicySpec

# ── Stub stores ──────────────────────────────────────────────


@dataclass
class _FakeConversationStore:
    """Minimal conversation store for policy evaluation tests.

    :param labels: Pre-seeded labels returned by
        ``get_conversation_labels``.
    :param appended_items: Items captured by ``append`` calls.
    """

    labels: dict[str, str] = field(default_factory=dict)
    appended_items: list[Any] = field(default_factory=list)

    def get_conversation(self, conversation_id: str) -> Conversation:
        """Return a stub conversation.

        :param conversation_id: Session id, e.g. ``"sess_1"``.
        :returns: Stub conversation with agent_id set.
        """
        return Conversation(
            id=conversation_id,
            created_at=1,
            updated_at=1,
            root_conversation_id=conversation_id,
            agent_id="ag_test",
        )

    def get_conversation_labels(self, conversation_id: str) -> dict[str, str]:
        """Return pre-seeded labels.

        :param conversation_id: Session id.
        :returns: Label dict.
        """
        return dict(self.labels)

    def set_labels(self, conversation_id: str, labels: dict[str, str]) -> None:
        """Record label writes.

        :param conversation_id: Session id.
        :param labels: Labels to write.
        """
        self.labels.update(labels)

    def append(self, conversation_id: str, items: list[Any]) -> list[ConversationItem]:
        """Record appended items and return stubs.

        :param conversation_id: Session id.
        :param items: Items to persist.
        :returns: List of stub conversation items with generated ids.
        """
        result = []
        for i, item in enumerate(items):
            ci = ConversationItem(
                id=f"item_{i}",
                type=getattr(item, "type", "function_call"),
                response_id=getattr(item, "response_id", "turn_1"),
                data=getattr(
                    item,
                    "data",
                    FunctionCallData(
                        agent="test-agent",
                        name="sys_os_shell",
                        arguments="{}",
                        call_id="call_1",
                    ),
                ),
                created_at=1,
                status="completed",
            )
            result.append(ci)
            self.appended_items.append(item)
        return result


@dataclass
class _FakeAgentStore:
    """Minimal agent store that returns a stub agent.

    :param agent: The agent to return from ``get()``.
    """

    agent: Agent | None = None

    def get(self, agent_id: str) -> Agent | None:
        """Return the pre-configured agent.

        :param agent_id: Agent id.
        :returns: The stub agent or None.
        """
        return self.agent


@dataclass
class _FakeBody:
    """Minimal SessionEventInput stub.

    :param type: Event type, e.g. ``"function_call"``.
    :param data: Event data dict.
    """

    type: str
    data: dict[str, Any]


class _FakeRequest:
    """Minimal stand-in for a FastAPI ``Request``.

    ``_evaluate_input_policy`` only passes the request through to
    ``_hold_native_ask_gate`` (for upstream-disconnect detection while
    parked on an ASK). The ALLOW / DENY / skip tests never reach the
    gate, and the ASK tests stub the gate out, so the request is never
    actually introspected — this exists only to fill the positional
    parameter with a real object rather than ``None``.
    """

    async def is_disconnected(self) -> bool:
        """Report the client as connected.

        :returns: Always ``False`` (test client never disconnects).
        """
        return False


# ── Helpers ──────────────────────────────────────────────────


def _make_function_call_body(
    name: str = "sys_os_shell",
    arguments: str = '{"command": "ls"}',
    call_id: str = "call_1",
) -> _FakeBody:
    """Build a function_call event body with evaluate_policy.

    :param name: Tool name, e.g. ``"sys_os_shell"``.
    :param arguments: JSON-encoded arguments string.
    :param call_id: Call identifier.
    :returns: A fake body matching SessionEventInput shape.
    """
    return _FakeBody(
        type="function_call",
        data={
            "name": name,
            "arguments": arguments,
            "call_id": call_id,
            "model": "test-agent",
            "evaluate_policy": True,
        },
    )


def _make_agent(agent_id: str = "ag_test") -> Agent:
    """Build a stub Agent entity.

    :param agent_id: Agent identifier.
    :returns: Agent with minimal fields.
    """
    return Agent(
        id=agent_id,
        created_at=1,
        name="test-agent",
        bundle_location="ag_test/abc123",
    )


def _make_spec_no_guardrails() -> AgentSpec:
    """Build an AgentSpec with no guardrails.

    :returns: Minimal AgentSpec with guardrails=None.
    """
    return AgentSpec(spec_version=1, name="test-agent")


# ── Tests ────────────────────────────────────────────────────


_CACHE_PATCH = "omnigent.server.routes.sessions.get_agent_cache"
_ENGINE_PATCH = "omnigent.server.routes.sessions.build_policy_engine"
_HOLD_GATE_PATCH = "omnigent.server.routes.sessions._hold_native_ask_gate"
_STREAM_PATCH = "omnigent.server.routes.sessions.session_stream"
_TREE_PATCH = "omnigent.runtime.policies.builder.load_verified_session_tree"


@pytest.mark.asyncio
async def test_allow_verdict():
    """Policy evaluation returns allow when the engine ALLOWs."""
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_function_call_body()

    spec = _make_spec_no_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    allow_result = PolicyResult(action=PolicyAction.ALLOW)

    async def _eval(_ctx: Any) -> PolicyResult:
        return allow_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_tool_call_policy(
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result is None


@pytest.mark.asyncio
async def test_deny_verdict():
    """Policy evaluation returns deny with reason when the
    engine DENYs.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_function_call_body()

    spec = _make_spec_no_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    deny_result = PolicyResult(
        action=PolicyAction.DENY,
        reason="Tool blocked by policy",
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return deny_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_tool_call_policy(
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result["verdict"] == "deny"
    assert result["reason"] == "Tool blocked by policy"


@pytest.mark.asyncio
async def test_pending_verdict_registers_elicitation():
    """Policy evaluation returns pending and registers an
    elicitation when the engine returns ASK.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_function_call_body()

    spec = _make_spec_no_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    ask_result = PolicyResult(
        action=PolicyAction.ASK,
        reason="Requires user approval",
        deciding_policies=["approve_shell"],
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return ask_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
        patch(_STREAM_PATCH),
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        # No per-policy override → the spec-wide engine value applies.
        mock_engine.spec_for = lambda _name: None
        mock_engine.ask_timeout = 30

        result = await _evaluate_tool_call_policy(
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result["verdict"] == "pending"
    assert "elicitation_id" in result
    assert result["elicitation_id"].startswith("elicit_")
    # The pending verdict carries the spec-resolved approval window so
    # the runner's park honors it; without it the runner falls back to
    # its hard-coded 120s default regardless of the spec.
    assert result["ask_timeout"] == 30
    # Approval state lives on the runner (in-memory dict), not
    # the task store. The server just publishes the SSE event.
    # No pending_tool_call row is created.


@pytest.mark.asyncio
async def test_pending_verdict_carries_per_policy_ask_timeout():
    """The deciding policy's ``ask_timeout`` override rides the verdict.

    A spec that grants one expensive ASK a longer window (e.g. nessie's
    pi worker setting a day-long approval) must reach the runner's park;
    if the verdict carried the spec-wide value instead, the override
    would be silently ignored on every runner-dispatched tool call.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_function_call_body()

    spec = _make_spec_no_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    ask_result = PolicyResult(
        action=PolicyAction.ASK,
        reason="Requires user approval",
        deciding_policies=["approve_shell"],
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return ask_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
        patch(_STREAM_PATCH),
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        # Real PolicySpec: the resolver reads ``.ask_timeout`` off the
        # deciding policy's spec, overriding the engine-wide 30.
        mock_engine.spec_for = lambda _name: PolicySpec(
            name="approve_shell", on=None, ask_timeout=86400
        )
        mock_engine.ask_timeout = 30

        result = await _evaluate_tool_call_policy(
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result["verdict"] == "pending"
    # 86400 (per-policy) — not 30 (engine-wide): the override wins.
    assert result["ask_timeout"] == 86400


@pytest.mark.asyncio
async def test_no_agent_binding_skips_policy():
    """When the session has no agent_id, policy evaluation is
    skipped and the function_call is persisted with allow verdict.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=None)
    conv = Conversation(
        id="sess_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="sess_1",
        agent_id=None,
    )
    body = _make_function_call_body()

    result = await _evaluate_tool_call_policy(
        "sess_1",
        conv,
        body,
        conv_store,
        agent_store,
        None,
    )

    assert result is None


# ── INPUT policy tests (step 5.6) ───────────────────────────


def _make_user_message_body(
    text: str = "hello tell me about canada",
) -> _FakeBody:
    """Build a user message event body.

    :param text: User message text.
    :returns: A fake body matching SessionEventInput shape
        for a user message.
    """
    return _FakeBody(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def _make_spec_with_guardrails() -> AgentSpec:
    """Build an AgentSpec declaring one guardrail policy.

    A policy must actually be declared: with none, the input gate
    short-circuits via ``any_policies_apply`` and never builds an engine,
    so a test that stubs the engine would assert on a code path the
    request never reaches.

    :returns: AgentSpec with one function policy declared.
    """
    from omnigent.spec.types import FunctionPolicySpec, FunctionRef, GuardrailsSpec

    return AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="test_policy",
                    on=None,
                    function=FunctionRef(path="tests.fake.policy"),
                )
            ]
        ),
    )


@pytest.mark.asyncio
async def test_input_allow_verdict():
    """INPUT policy evaluation returns allow when the engine ALLOWs."""
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body()

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    allow_result = PolicyResult(action=PolicyAction.ALLOW)

    async def _eval(_ctx: Any) -> PolicyResult:
        return allow_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result is None


@pytest.mark.asyncio
async def test_input_deny_verdict():
    """INPUT policy evaluation returns deny when the engine DENYs."""
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body("hello tell me about canada")

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    deny_result = PolicyResult(
        action=PolicyAction.DENY,
        reason="Input mentions Canada",
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return deny_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result["verdict"] == "deny"
    assert result["reason"] == "Input mentions Canada"


@pytest.mark.asyncio
async def test_skill_slash_command_policy_body_uses_typed_command_text():
    """
    Skill slash-command input policy evaluates typed user text.

    The policy surface must be ``/<skill> <arguments>``, not the
    hidden meta message that contains the full skill instructions.
    Otherwise a bundled skill body could trip input guardrails before
    the user has made a request.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    slash_body = SessionEventInput(
        type="slash_command",
        data={
            "kind": "skill",
            "name": "grill-me",
            "arguments": "review Canada rollout",
        },
    )
    policy_body = _build_skill_slash_command_policy_body(slash_body)

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    seen_content: list[str] = []
    deny_result = PolicyResult(
        action=PolicyAction.DENY,
        reason="Input mentions Canada",
    )

    async def _eval(ctx: Any) -> PolicyResult:
        """
        Capture the content evaluated by the policy engine.

        :param ctx: Evaluation context built by the route helper.
        :returns: Deny verdict so the test can verify propagation.
        """
        seen_content.append(ctx.content)
        return deny_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            policy_body,
            conv_store,
            agent_store,
            None,
        )

    # REQUEST content is the structured dict ({"user_content", "attachments"})
    # so policies can reason about attachments per-file; a slash command carries
    # no attachments.
    assert seen_content == [{"user_content": "/grill-me review Canada rollout", "attachments": []}]
    assert result["verdict"] == "deny"
    assert result["reason"] == "Input mentions Canada"


@pytest.mark.asyncio
async def test_input_no_guardrails_skips_policy():
    """When the agent spec has no guardrails, INPUT policy
    is skipped and the message is persisted with allow verdict.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body()

    spec = _make_spec_no_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")

    with (
        patch(_CACHE_PATCH) as mock_cache,
    ):
        mock_cache.return_value.load.return_value = loaded
        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result is None


@dataclass
class _FakePolicyStore:
    """Minimal policy store for the input-gate fast-path tests.

    :param session_policies: Rows returned for ``list_for_session``.
    :param default_policies: Rows returned for ``list_defaults``.
    """

    session_policies: list[StoredPolicy] = field(default_factory=list)
    default_policies: list[StoredPolicy] = field(default_factory=list)

    def list_for_session(self, session_id: str) -> list[StoredPolicy]:
        """Return this session's stored policies.

        :param session_id: Session id, e.g. ``"sess_1"``.
        :returns: The configured session rows.
        """
        return list(self.session_policies)

    def list_defaults(self) -> list[StoredPolicy]:
        """Return server-wide default policies.

        :returns: The configured default rows.
        """
        return list(self.default_policies)


def _make_stored_policy(name: str, session_id: str | None) -> StoredPolicy:
    """Build an enabled python-handler stored policy row.

    :param name: Policy name, e.g. ``"deny_pii"``.
    :param session_id: Owning session, or ``None`` for a default policy.
    :returns: A :class:`Policy` entity the builder can convert to a spec.
    """
    return StoredPolicy(
        id=f"pol_{name}",
        name=name,
        session_id=session_id,
        scope="session" if session_id else "default",
        created_at=1,
        type="python",
        handler="tests.fake.policy",
    )


@pytest.fixture
def policy_store(monkeypatch: pytest.MonkeyPatch):
    """Install an empty policy store and reset the builder's spec caches.

    A store is always configured in a real deployment, so these tests must
    run with one bound; the module-level spec caches are process-wide, so
    they are cleared on entry and exit to keep the tests hermetic.

    :param monkeypatch: pytest patcher, used to bind the runtime global.
    :returns: The installed :class:`_FakePolicyStore`.
    """
    store = _FakePolicyStore()
    monkeypatch.setattr("omnigent.runtime._globals._policy_store", store)
    _clear_policy_spec_caches()
    yield store
    _clear_policy_spec_caches()


def _clear_policy_spec_caches() -> None:
    """Evict the builder's default/session policy-spec caches."""
    from omnigent.runtime.policies.builder import (
        invalidate_default_policy_specs_cache,
        invalidate_session_policy_specs_cache,
    )

    invalidate_default_policy_specs_cache()
    invalidate_session_policy_specs_cache("sess_1")


@pytest.mark.asyncio
async def test_input_no_policies_skips_engine_and_tree_scan(policy_store):
    """A configured-but-empty policy store must not build an engine.

    The gate used to skip only when ``get_policy_store() is None`` — never
    true in a real deployment — so every user message built an engine and
    paid its O(spawn-tree) scan even with zero policies anywhere.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body()

    loaded = LoadedAgent(spec=_make_spec_no_guardrails(), workdir="/tmp/fake")

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
        patch(_TREE_PATCH) as mock_tree,
    ):
        mock_cache.return_value.load.return_value = loaded
        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result is None
    mock_build.assert_not_called()
    mock_tree.assert_not_called()


@pytest.mark.asyncio
async def test_input_policy_added_mid_session_is_enforced(policy_store):
    """A policy created after the session is live still gates the next message.

    Guards the fast path against caching a stale "no policies" verdict: the
    first message legitimately skips, and once a session policy exists (with
    the store's cache invalidated, as the CRUD routes do) the very next
    message must reach the engine and be denied.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body()

    loaded = LoadedAgent(spec=_make_spec_no_guardrails(), workdir="/tmp/fake")
    deny_result = PolicyResult(action=PolicyAction.DENY, reason="Denied mid-session")

    async def _eval(_ctx: Any) -> PolicyResult:
        return deny_result

    async def _evaluate_once() -> tuple[dict[str, Any] | None, bool]:
        """Run the input gate once against the current store contents.

        :returns: ``(verdict, engine_was_built)`` — the verdict dict (or
            ``None`` on allow/skip) and whether an engine was constructed.
        """
        with (
            patch(_CACHE_PATCH) as mock_cache,
            patch(_ENGINE_PATCH) as mock_build,
        ):
            mock_cache.return_value.load.return_value = loaded
            mock_engine = mock_build.return_value
            mock_engine.evaluate = _eval
            mock_engine.apply_label_writes = lambda x: None
            verdict = await _evaluate_input_policy(
                _FakeRequest(),
                "sess_1",
                conv,
                body,
                conv_store,
                agent_store,
                None,
            )
            return verdict, mock_build.called

    # No policies yet: the message is allowed without an engine build.
    assert await _evaluate_once() == (None, False)

    # A policy is added mid-session, exactly as POST /sessions/{id}/policies does.
    policy_store.session_policies.append(_make_stored_policy("deny_all", "sess_1"))
    _clear_policy_spec_caches()

    verdict, built = await _evaluate_once()
    assert built is True
    assert verdict is not None
    assert verdict["verdict"] == "deny"
    assert verdict["reason"] == "Denied mid-session"


@pytest.mark.asyncio
async def test_input_empty_text_skips_policy():
    """When the user message has no text content, INPUT policy
    is skipped (nothing to evaluate).
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _FakeBody(
        type="message",
        data={"role": "user", "content": []},
    )

    result = await _evaluate_input_policy(
        _FakeRequest(),
        "sess_1",
        conv,
        body,
        conv_store,
        agent_store,
        None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_input_ask_approved_falls_through_to_allow():
    """A REQUEST-phase ASK the user APPROVES collapses to ALLOW.

    Regression guard for the request-phase approval round-trip. The
    REQUEST phase has no runner-side park (the message has not been
    forwarded yet), so the input path must hold the gate server-side
    via ``_hold_native_ask_gate`` and, on accept, return ``None`` so the
    /events handler forwards the message. Before the fix, an input ASK
    returned a ``pending`` verdict that the handler collapsed to
    ``[Denied by policy]`` — the approval card was published but nothing
    waited on it.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body("delete the file /tmp/policy-demo.txt")

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    ask_result = PolicyResult(
        action=PolicyAction.ASK,
        reason="Deleting files requires approval",
        deciding_policies=["llm_prompt_classifier_policy"],
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return ask_result

    held_phases: list[Phase] = []

    async def _fake_hold(
        _request: Any,
        *,
        session_id: str,
        phase: Phase,
        data: dict[str, Any],
        engine: Any,
        result: PolicyResult,
        conversation_store: Any,
        elicitation_id: str | None = None,
    ) -> bool:
        """Stand in for the server-side approval park; simulate approve.

        Records the phase so the test can assert the ASK was routed
        through the gate at the REQUEST phase (not the old pending path).

        :returns: ``True`` — the user clicked Approve.
        """
        held_phases.append(phase)
        return True

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
        patch(_HOLD_GATE_PATCH, new=_fake_hold),
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    # The ASK was routed through the server-side approval park, at the
    # REQUEST phase. If this is empty, the input path skipped the gate
    # (the regressed "pending"/silent-deny path); a non-REQUEST phase
    # would mean the wrong gate fired.
    assert held_phases == [Phase.REQUEST]
    # Approve -> None -> the /events handler forwards the message. A dict
    # here would mean the message was wrongly blocked despite approval.
    assert result is None


@pytest.mark.asyncio
async def test_input_ask_declined_denies():
    """A REQUEST-phase ASK the user DECLINES (or times out) collapses to DENY.

    The fail-closed half of the request-phase round-trip: when the
    server-side park returns ``False`` (decline / cancel / timeout),
    ``_evaluate_input_policy`` returns a deny verdict carrying the
    deciding policy's reason so the /events handler refuses to forward
    the message.
    """
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_user_message_body("delete the file /tmp/policy-demo.txt")

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    ask_result = PolicyResult(
        action=PolicyAction.ASK,
        reason="Deleting files requires approval",
        deciding_policies=["llm_prompt_classifier_policy"],
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return ask_result

    async def _fake_hold(
        _request: Any,
        *,
        session_id: str,
        phase: Phase,
        data: dict[str, Any],
        engine: Any,
        result: PolicyResult,
        conversation_store: Any,
        elicitation_id: str | None = None,
    ) -> bool:
        """Stand in for the server-side approval park; simulate decline.

        :returns: ``False`` — the user declined / the gate timed out.
        """
        return False

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
        patch(_HOLD_GATE_PATCH, new=_fake_hold),
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_input_policy(
            _FakeRequest(),
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    # Decline -> deny verdict carrying the deciding policy's reason. A
    # ``None`` here would mean a declined ASK silently let the message
    # through (the dangerous direction).
    assert result["verdict"] == "deny"
    assert result["reason"] == "Deleting files requires approval"


@pytest.mark.asyncio
async def test_input_policy_deny_sentinel_persists_as_assistant_history():
    """INPUT policy DENY stores the deny sentinel for later history reads."""
    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")

    await _persist_policy_deny_sentinel(
        "sess_1",
        conv,
        "Request contains BLOCK_THIS_TOKEN",
        conv_store,
        agent_store,
    )

    assert len(conv_store.appended_items) == 1
    item = conv_store.appended_items[0]
    assert item.type == "message"
    assert item.response_id.startswith("deny_")
    assert item.data.role == "assistant"
    assert item.data.agent == "test-agent"
    assert item.data.content == [
        {
            "type": "output_text",
            "text": "[Denied by policy: Request contains BLOCK_THIS_TOKEN]",
        }
    ]


# ── OUTPUT policy tests (step 5.7) ──────────────────────────


def _make_assistant_message_body(
    text: str = "Here is some information.",
) -> _FakeBody:
    """Build an assistant message event body.

    :param text: Assistant message text.
    :returns: A fake body matching SessionEventInput shape
        for an assistant message.
    """
    return _FakeBody(
        type="message",
        data={
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
            "model": "test-agent",
        },
    )


@pytest.mark.asyncio
async def test_output_allow_verdict():
    """OUTPUT policy evaluation returns allow when the engine
    ALLOWs the assistant response.
    """
    from omnigent.server.routes.sessions import _evaluate_output_policy

    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_assistant_message_body("This is a safe response.")

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    allow_result = PolicyResult(action=PolicyAction.ALLOW)

    async def _eval(_ctx: Any) -> PolicyResult:
        return allow_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_output_policy(
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result is None


@pytest.mark.asyncio
async def test_output_deny_replaces_text():
    """OUTPUT policy DENY replaces the assistant text with the
    deny sentinel in the persisted message.
    """
    from omnigent.server.routes.sessions import _evaluate_output_policy

    conv_store = _FakeConversationStore()
    agent_store = _FakeAgentStore(agent=_make_agent())
    conv = conv_store.get_conversation("sess_1")
    body = _make_assistant_message_body("Here is my secret API key: sk-1234")

    spec = _make_spec_with_guardrails()
    loaded = LoadedAgent(spec=spec, workdir="/tmp/fake")
    deny_result = PolicyResult(
        action=PolicyAction.DENY,
        reason="Response contains a secret",
    )

    async def _eval(_ctx: Any) -> PolicyResult:
        return deny_result

    with (
        patch(_CACHE_PATCH) as mock_cache,
        patch(_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value = loaded
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _eval
        mock_engine.apply_label_writes = lambda x: None

        result = await _evaluate_output_policy(
            "sess_1",
            conv,
            body,
            conv_store,
            agent_store,
            None,
        )

    assert result["verdict"] == "deny"
    assert result["reason"] == "Response contains a secret"
    # Verify the _denied_body has the deny sentinel, not the original text.
    denied_body = result["_denied_body"]
    denied_content = denied_body.data.get("content", [])
    denied_text = denied_content[0]["text"]
    assert "[Denied by policy: Response contains a secret]" in denied_text
    assert "sk-1234" not in denied_text


def test_build_evaluation_context_request_accepts_string_data() -> None:
    """REQUEST-phase ``data`` may be a bare string and must NOT raise.

    A bare string is accepted for compatibility with older or third-party
    callers that send the prompt text directly rather than wrapped in an
    object — OpenCode's own plugin used to be one of these, but now sends
    ``{"text": ...}`` like the native hooks (see the dict-data test below).
    The old code did ``data.get("text")`` unconditionally and ``AttributeError``ed
    on a string, 500ing the evaluate endpoint — which silently failed the
    request-phase gate OPEN (cost-over-budget terminal prompts sailed through).
    """
    ctx = _build_evaluation_context(Phase.REQUEST, "delete the prod database", {})
    # REQUEST content is the structured dict ({"user_content", "attachments"}); the
    # native / opencode path carries no attachments.
    assert ctx.content == {"user_content": "delete the prod database", "attachments": []}


def test_build_evaluation_context_request_dict_still_works() -> None:
    """The native-hook convention (dict with ``text``) still resolves."""
    ctx = _build_evaluation_context(Phase.REQUEST, {"text": "hello"}, {})
    assert ctx.content == {"user_content": "hello", "attachments": []}
    # ``content`` fallback also honored.
    ctx2 = _build_evaluation_context(Phase.REQUEST, {"content": "hi"}, {})
    assert ctx2.content == {"user_content": "hi", "attachments": []}
