"""
Runtime evaluation contracts for the policy system.

These are the shapes that cross the ``Policy.evaluate()``
boundary and the engine-to-approval-helper boundary. They are
NOT spec types — they do not appear in any config.yaml the user
writes. Spec types (what the parser consumes and emits) live in
:mod:`omnigent.spec.types`; runtime evaluation types live
here.

Three types live in this module:

- :class:`EvaluationContext` — what the caller hands to the
  engine on each enforcement call (phase + content +
  resolved tool_name).
- :class:`PolicyResult` — what a single policy returns and what
  the engine composes across policies.
- :class:`ElicitationRequest` — the internal contract for an
  ASK that's about to be surfaced upstream as an MCP-style
  elicitation. Carries the human-readable message plus the
  policy-context fields the renderer needs (phase,
  policy_name, content_preview).

Agent-author Python callables import :class:`EvaluationContext`
and :class:`PolicyResult` from here (or from the
:mod:`omnigent.policies` package entry point).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnigent.spec.types import Phase, PolicyAction, StateUpdate

if TYPE_CHECKING:
    from omnigent.entities import ConversationItem

_log = logging.getLogger(__name__)


# Proto-style phase wire strings (the ``type`` field on events that
# cross the harness↔runner boundary) for which an unavailable policy
# evaluation must fail CLOSED (default ``POLICY_ACTION_DENY``).
#
# ``PHASE_TOOL_CALL``: for connector-native MCP tools the in-band verdict is
# the only enforcement point — the call is never re-checked server-side — so
# an unevaluable policy must not let the call through.
#
# ``PHASE_REQUEST``: the request gate runs before the LLM turn and is the
# sole pre-turn enforcement point for native sessions (UserPromptSubmit). A
# server hiccup must not let an over-budget or otherwise-blocked request
# proceed, so this phase also fails CLOSED.
#
# ``PHASE_TOOL_RESULT`` is intentionally NOT here: by the time the result
# phase runs the tool has already executed, so failing it closed would only
# block an already-incurred side effect; it fails OPEN like the advisory LLM
# phases.
#
# Defined once here so the enforcement sites
# (``omnigent.runner.app`` and ``omnigent.runtime.harnesses._scaffold``)
# can't drift if the set of fail-closed phases changes.
FAIL_CLOSED_PHASES: tuple[str, ...] = ("PHASE_TOOL_CALL", "PHASE_REQUEST")


@dataclass(frozen=True)
class EvaluationContext:
    """
    Everything the engine needs to evaluate one phase.

    Filled by the caller (workflow or executor hook) BEFORE
    calling ``engine.evaluate(ctx)``. The engine never has to
    introspect ``content`` to answer "which tool was this?" —
    the caller resolves ``tool_name`` because only it has the
    local state to do so cheaply (on ``TOOL_RESULT`` the
    ``function_call_output`` payload carries ``call_id`` but no
    ``name``; the caller knows the name from the earlier
    dispatch).

    :param phase: The enforcement point.
    :param content: Phase-specific payload — shape depends on
        ``phase``:

        - ``REQUEST`` / ``RESPONSE``: ``str`` (raw user /
          assistant text).
        - ``TOOL_CALL``: ``dict[str, Any]`` shaped
          ``{"name": <name>, "arguments": <parsed-args-dict>}``.
        - ``TOOL_RESULT``: ``dict`` shaped ``{"result": <tool-output>}``.
        - ``LLM_REQUEST``: ``dict`` with the full LLM prompt
          (system instructions, messages, tool schemas).
        - ``LLM_RESPONSE``: ``dict`` with the raw model output
          before tool-call extraction or post-processing.

        Policies know which shape to expect from their declared
        ``on:`` phases — the engine never introspects this field
        itself.
    :param tool_name: Resolved tool name. Populated on
        ``TOOL_CALL`` and ``TOOL_RESULT``; ``None`` on
        ``REQUEST``, ``RESPONSE``, ``LLM_REQUEST``, and
        ``LLM_RESPONSE``.
    :param trajectory: Recent conversation items (oldest first)
        the classifier may consume to produce situational
        reason text — see designs/LIVE_POLICIES.md §4.1. The
        engine populates this on every ``evaluate()`` call by
        querying the conversation store; callers leave it
        ``None``. ``FunctionPolicy`` ignores the field;
        ``PromptPolicy`` formats it into the
        classifier prompt. ``None`` means "engine never
        populated it" (test contexts); empty list means
        "brand-new conversation, no items yet."
    :param actor: Identity of the principal executing the
        request. Shape:
        ``{"run_as": "<email>", "client_id": "<oauth-client>"}``.
        ``None`` when identity is unknown (tests, legacy
        callers). Passed through to the ``event.context.actor``
        field that :class:`FunctionPolicy` builds for its
        callable.
    :param request_data: Original tool-call payload on
        ``TOOL_RESULT`` phase, so ON RESULT policies can
        correlate input/output. ``None`` on all other phases.
        Surfaced as ``event["request_data"]`` to the callable.
    :param session_state: Mutable per-conversation key/value
        store scoped to the engine's lifetime (one workflow
        turn). Does NOT persist across turns. Injected by the
        engine before each policy dispatch so callables can
        read accumulated state (e.g. a running counter, a
        previously-extracted entity). The engine owns
        population of this field — callers leave it ``None``.
        Surfaced as ``event["session_state"]`` to the callable.
        ``None`` means "engine not yet populated" (test
        contexts); empty dict means "no state written yet."
    :param usage: Cumulative LLM token usage for this session.
        Shape: ``{"input_tokens": N, "output_tokens": M,
        "total_tokens": T}``. Injected by the engine before
        each policy dispatch so callables can read the running
        totals (e.g. for budget-enforcement policies).
        Surfaced as ``event["context"]["usage"]`` to the
        callable. ``None`` means "engine not yet populated"
        (test contexts); empty dict means "no usage recorded
        yet."
    :param subtree_usage: Subtree-scoped cumulative LLM cost for
        this conversation and its descendants only (not the whole
        session tree). Same shape as ``usage``. Injected by the
        engine ONLY when a ``subagent_cost_budget`` policy is
        configured — ``None`` otherwise, so sessions without that
        policy pay no subtree-cost lookup. Surfaced as
        ``event["context"]["subtree_usage"]`` to the callable.
    :param user_daily_cost: The session owner's per-UTC-day cost
        rollup, shape
        ``{"cost_usd": <float>, "ask_approved_usd": <float>}``,
        read from the ``user_daily_cost`` store at engine-build time.
        Injected ONLY when a policy needs it (the per-user daily
        cost-budget policy is configured) — ``None`` otherwise, so
        sessions without that policy pay no owner/daily-cost lookup.
        Surfaced as ``event["context"]["user_daily_cost"]``.
    :param model: The model the session is currently using —
        the conversation's ``model_override`` when set (e.g. via
        a mid-session ``/model`` change), else the agent spec's
        ``llm.model``, e.g. ``"databricks-claude-opus-4-8"`` or
        the native tier alias ``"opus"``. Injected by the engine
        before each policy dispatch (resolved at engine-build
        time) so callables can gate on the active model (e.g. a
        cost policy that forces a downgrade off an expensive
        model). Surfaced as ``event["context"]["model"]`` to the
        callable. ``None`` when the engine could not determine a
        model (no override and no spec ``llm``).
    :param harness: The harness running the session, e.g.
        ``"codex-native"``, stamped by a native tool hook so policies can
        tailor messages to how that harness lets the user switch model
        (codex-native is terminal-only). Surfaced as
        ``event["context"]["harness"]``. ``None`` on web / API / unstamped
        paths.
    :param labels: Read-only snapshot of the conversation's guardrails
        labels, e.g. ``{"cost_control.plan": "{...}"}``. Injected by the
        engine from its label hot cache (the same source ``condition:``
        gates read) so function policy callables can gate on persisted
        label state via ``event["context"]["labels"]``. ``None`` means
        "not populated" (runner-local gate, test contexts) — policies
        must treat that the same as an empty mapping.
    :param llm_client: An :class:`~omnigent.policies.types.PolicyLLMClient`
        instance configured with the server-level LLM credentials.
        Available to function policy callables via
        ``event["llm_client"]``. ``None`` when the server has no
        ``llm:`` config. The client is shared across all policies
        in one engine; each call should pass ``model`` and
        ``connection_params`` from the engine's resolved config.
    """

    phase: Phase
    content: Any
    tool_name: str | None = None
    trajectory: list[ConversationItem] | None = None
    actor: dict[str, str] | None = None
    request_data: Any = None
    session_state: dict[str, Any] | None = None
    usage: dict[str, float] | None = None
    subtree_usage: dict[str, float] | None = None
    user_daily_cost: dict[str, float | str] | None = None
    model: str | None = None
    harness: str | None = None
    labels: dict[str, str] | None = None
    llm_client: Any = None  # PolicyLLMClient | None — Any to avoid import cycle


@dataclass(frozen=True)
class PolicyResult:
    """
    One policy's decision (or the engine's composed decision).

    Returned by ``Policy.evaluate()`` and by
    ``PolicyEngine.evaluate()``. The same shape is used at
    both layers: individual policies return a single-policy
    decision, the engine composes them and returns the
    aggregate.

    :param action: The decision (``ALLOW``, ``ASK``, or
        ``DENY``), e.g. ``PolicyAction.DENY``.
    :param reason: Human-readable reason string. Shown to the
        user on ASK, included in logs / spans on DENY, ``None``
        on ALLOW, e.g. ``"Canada-related topics are denied."``.
    :param set_labels: Labels the policy wants to write. For
        a single-policy result: the raw writes the policy
        requested (before whitelist filtering). For an
        engine-composed result: the writes the engine has
        accumulated and intends to apply on this decision
        (filtering already done). ``None`` when the policy
        wrote no labels, e.g. ``{"integrity": "0"}``.
    :param deciding_policies: Names of all policies that drove
        the composed result. Engine-set only — single-policy
        results leave it ``None``. On DENY: a single-element
        list with the short-circuiting policy. On ASK: all
        ASKing policies in YAML order. On ALLOW: ``None``.
        ``deciding_policy`` is a computed property returning
        ``deciding_policies[0]`` (or ``None`` when unset);
        all existing callers that read ``.deciding_policy``
        work unchanged.
    :param data: Optional replacement payload returned by the
        policy callable. When present on an ALLOW result, the
        enforcement site substitutes this value for the original
        event content — e.g. a PII-redacted version of the tool
        arguments (TOOL_CALL phase) or tool output (TOOL_RESULT
        phase). ``None`` means "use original content unchanged".
        ``Any`` because the shape varies by phase: a dict of
        tool arguments on TOOL_CALL, a string on TOOL_RESULT.
        When multiple policies transform data, each policy
        receives the previous policy's output as its input —
        the engine feeds the composed result back as
        ``ctx.content`` before dispatching to the next policy.
    :param state_updates: Ordered list of :class:`StateUpdate`
        operations to apply to the engine's ``session_state``.
        Each entry specifies a key, an action (``SET``,
        ``INCREMENT``, ``DELETE``, ``APPEND``), and an optional
        value. Accumulated across all policies in the evaluation
        pass and applied by the engine on ALLOW and DENY;
        withheld on ASK pending approval (POLICIES.md §7.2 — a
        denied ASK must leave no trace). ``None`` means "no
        state changes." e.g.
        ``[StateUpdate(key="call_count", action=StateUpdateAction.INCREMENT, value=1)]``.
    """

    action: PolicyAction
    reason: str | None = None
    set_labels: dict[str, str] | None = None
    deciding_policies: list[str] | None = None
    data: Any = None
    state_updates: list[StateUpdate] | None = None

    @property
    def deciding_policy(self) -> str | None:
        """First deciding policy name, or ``None``.

        Derived from ``deciding_policies[0]`` so callers that
        read ``.deciding_policy`` work without change.
        """
        return self.deciding_policies[0] if self.deciding_policies else None


@dataclass(frozen=True)
class ElicitationRequest:
    """
    Internal contract for an ASK that surfaces upstream as an
    MCP-style elicitation.

    Mirrors the wire-shape of an MCP ``elicitation/create`` form-mode
    request (`message`, `requestedSchema`, `extra` fields), restricted
    to the binary approve / reject use case ASK policies need today.
    The verdict is carried in the consumer's MCP-style
    ``action`` field (``accept``/``decline``/``cancel``); no form
    fields are required, so :attr:`requested_schema` is empty.

    :param message: Combined human-readable reason string from all
        ASKing policies, joined with ``"; "`` per POLICIES.md §4.
        Shown to the user in the approval UI as the elicitation
        ``message``. e.g. ``"PII detected; require user approval."``
    :param requested_schema: A restricted subset of JSON Schema
        defining the structure of an expected response, per the MCP
        elicitation spec. For binary approve/reject ASKs this is the
        empty dict ``{}`` — the verdict is in the consumer's
        ``action`` field. e.g. ``{}`` or
        ``{"type": "object", "properties": {...}}``.
    :param phase: Which enforcement point produced the ASK,
        e.g. ``"request"`` or ``"tool_call"``. Surfaces in the
        elicitation event's extras so the renderer can label the
        prompt.
    :param policy_names: Names of all ASKing policies that contributed
        to this elicitation, in YAML order. Always a non-empty list.
        ``policy_name`` is a computed property returning
        ``policy_names[0]`` — existing callers that read
        ``.policy_name`` work without change.
        e.g. ``["pii_redact"]`` or ``["pii_redact", "cost_gate"]``.
    :param content_preview: Truncated snapshot of the content being
        gated. Lets a human reviewer see what they're approving
        without overwhelming the UI on a 50 KB payload. Surfaces in
        the elicitation event's extras.
    """

    message: str
    phase: str
    policy_names: list[str]
    content_preview: str
    requested_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def policy_name(self) -> str:
        """First ASKing policy name.

        Derived from ``policy_names[0]`` so callers that read
        ``.policy_name`` work without change.
        """
        return self.policy_names[0] if self.policy_names else ""


@dataclass
class PolicyLLMClient:
    """
    Pre-configured LLM client for policy function callables.

    Wraps an :class:`~omnigent.llms.client.Client` with the
    server-level model and connection params pre-bound, so policy
    callables can call
    ``await event["llm_client"].create(input=...)``
    without needing to know the model or connection details.

    The ``model`` and ``connection_params`` are resolved from the
    server-level ``llm:`` config at engine build time.

    :param _client: The underlying multi-provider LLM client.
        An :class:`~omnigent.llms.client.Client` instance
        (typed as ``Any`` to avoid an import cycle from
        policy types to the LLM module).
    :param _model: The provider-prefixed model id from the
        server ``llm:`` config, e.g. ``"openai/gpt-4o-mini"``.
    :param _connection: Connection overrides (api_key, base_url)
        from the server ``llm:`` config. ``None`` falls back to
        adapter defaults / env vars.
    :param _request_timeout: Request timeout in seconds from the
        server ``llm:`` config, e.g. ``300``.
    :param _fallback_models: Ordered backup models tried when the
        primary ``_model`` call fails. Same provider-prefixed
        format as ``_model``. Empty (the default) preserves
        single-model behaviour — one attempt, no fallback loop.
        ``_connection``/``_request_timeout`` are shared across the
        primary and every fallback, so same-provider fallbacks are
        the reliable case (a cross-provider fallback only works when
        ``_connection`` is ``None``).
    """

    _client: Any  # omnigent.llms.client.Client — Any to avoid import cycle
    _model: str
    _connection: dict[str, str] | None
    _request_timeout: int
    _fallback_models: list[str] = field(default_factory=list)

    async def create(
        self,
        *,
        input: list[dict[str, Any]],
        instructions: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Call the server-level LLM with pre-bound model and credentials.

        Thin wrapper around ``client.responses.create()`` that
        pre-fills ``model``, ``connection_params``, and ``timeout``
        from the server config. Callers can override any of these
        via kwargs.

        When ``_fallback_models`` is non-empty and the caller does
        not override ``model``, the primary model is tried first and
        each fallback in turn on failure; the last exception is
        re-raised only after every candidate has failed. A caller
        that passes an explicit ``model`` opts out of fallback — the
        single requested model is used as-is.

        Candidates are tried serially, so the worst-case latency of
        this call is ``(1 + len(_fallback_models)) * timeout``. On
        the policy hot path this delays the fail-closed (DENY)
        verdict, so keep the fallback list short.

        :param input: Messages in OpenAI Responses API format,
            e.g. ``[{"role": "user", "content": [{"type": "input_text",
            "text": "..."}]}]``.
        :param instructions: Optional system-level instructions.
        :param kwargs: Additional kwargs forwarded to
            ``client.responses.create()``.
        :returns: A :class:`~omnigent.llms.types.Response`.
        :raises Exception: The last error encountered when every
            candidate model fails. Propagates the primary model's
            exception when no fallbacks are configured.
        """
        connection_params = kwargs.pop("connection_params", self._connection)
        timeout = kwargs.pop("timeout", self._request_timeout)

        # An explicit model override opts out of the fallback chain —
        # honour exactly what the caller asked for.
        if "model" in kwargs:
            return await self._client.responses.create(
                input=input,
                model=kwargs.pop("model"),
                connection_params=connection_params,
                timeout=timeout,
                instructions=instructions,
                **kwargs,
            )

        candidates = [self._model, *self._fallback_models]
        last_exc: Exception | None = None
        for index, model in enumerate(candidates):
            try:
                response = await self._client.responses.create(
                    input=input,
                    model=model,
                    connection_params=connection_params,
                    timeout=timeout,
                    instructions=instructions,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001 — retry next model on any failure
                last_exc = exc
                remaining = len(candidates) - index - 1
                if remaining:
                    _log.warning(
                        "PolicyLLMClient: model %r failed (%s); "
                        "falling back to next of %d remaining",
                        model,
                        exc,
                        remaining,
                        exc_info=True,
                    )
                continue
            # A non-primary candidate succeeded — record which fallback
            # recovered the call so the fallback path is visible in logs.
            if index > 0:
                _log.warning(
                    "PolicyLLMClient: recovered on fallback model %r "
                    "(candidate %d of %d) after primary failure",
                    model,
                    index + 1,
                    len(candidates),
                )
            return response
        # Every candidate failed — surface the last error to the caller.
        # This is the fail-closed (DENY) path and, because candidates are
        # tried serially, the caller has now waited up to
        # ``len(candidates) * timeout``; log it so the latency is visible.
        _log.error(
            "PolicyLLMClient: all %d candidate model(s) failed after "
            "serial attempts (up to %ds each); surfacing last error",
            len(candidates),
            timeout,
        )
        assert last_exc is not None
        raise last_exc


__all__ = [
    "ElicitationRequest",
    "EvaluationContext",
    "PolicyLLMClient",
    "PolicyResult",
]
