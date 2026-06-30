"""Typed contracts for policy function callables.

Defines the exact shapes of the ``event`` dict passed TO a policy
callable and the ``response`` dict returned FROM it. These are
``TypedDict`` definitions — they are not enforced at runtime (the
actual coercion lives in :func:`_coerce_to_policy_result` in
:mod:`omnigent.policies.function`), but they serve as the
authoritative reference for authors implementing policy callables.

Usage in a policy callable::

    from omnigent.policies.schema import PolicyEvent, PolicyResponse

    def my_policy(event: PolicyEvent) -> PolicyResponse | None:
        if event["type"] != "tool_call":
            return None  # abstain
        tool = event["data"].get("tool", "")
        if tool == "dangerous_tool":
            return {"result": "DENY", "reason": "Blocked."}
        return {"result": "ALLOW"}

Two-argument form (with config)::

    def my_policy(event: PolicyEvent, config: dict[str, str]) -> PolicyResponse | None:
        limit = int(config.get("limit", "10"))
        ...

Factory form (with ``factory_params``)::

    def create_policy(limit: int = 10) -> PolicyCallable:
        def evaluate(event: PolicyEvent) -> PolicyResponse | None:
            ...
        return evaluate
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from omnigent.policies.types import PolicyLLMClient

# ── Event (input to callable) ────────────────────────────────────────────────


class ActorContext(TypedDict, total=False):
    """Identity of the principal executing the request.

    :param run_as: Email of the authenticated user, e.g.
        ``"alice@example.com"``. Empty string when unknown.
    :param client_id: OAuth client ID of the caller. Empty
        string when unknown.
    """

    run_as: str
    client_id: str


class UsageContext(TypedDict, total=False):
    """Cumulative LLM token usage and cost for the session.

    Counters are cumulative across the session. ``total_cost_usd`` is
    priced from the model's per-token rates; it is ``0.0`` until the
    first LLM call and stays ``0.0`` when pricing is unavailable for the
    model. Cost-budget policies read it via
    ``event["context"]["usage"]["total_cost_usd"]``.

    :param input_tokens: Total input tokens consumed.
    :param output_tokens: Total output tokens consumed.
    :param total_tokens: Sum of input and output tokens.
    :param total_cost_usd: Cumulative LLM cost in USD, e.g. ``0.0123``.
        ``0.0`` when token pricing is unavailable for the model.
    """

    input_tokens: float
    output_tokens: float
    total_tokens: float
    total_cost_usd: float


# Reserved ``state_updates`` key the per-user daily cost-budget policy
# emits on an ASK so the engine routes the approved checkpoint to the
# ``user_daily_cost.ask_approved_usd`` store column (per user+day)
# instead of the per-conversation ``session_state``. Shared by the
# policy (emits it) and :class:`PolicyEngine.apply_state_updates`
# (intercepts it) so a daily approval persists across the user's
# sessions, not just the one conversation.
USER_DAILY_ASK_APPROVED_STATE_KEY = "_policy_user_daily_ask_approved_usd"

# Reserved ``state_updates`` key the per-session cost-budget policy emits on
# an ASK to record the highest soft checkpoint approved. The cost budget is
# per-SESSION (the whole spawn tree), but a sub-agent runs as its own
# conversation, so :class:`PolicyEngine.apply_state_updates` routes this key
# to the ROOT conversation's ``session_state`` (and the engine seeds it from
# the root). Without that, approving on the parent wouldn't carry to a
# sub-agent and it would re-ask at the same threshold. Shared by the policy
# (emits it) and the engine (routes + seeds it).
SESSION_COST_ASK_APPROVED_STATE_KEY = "_policy_cost_ask_approved_usd"

# Reserved ``state_updates`` key the cost-budget policy emits when the user
# approves continuing despite an unpriced model. Like
# ``SESSION_COST_ASK_APPROVED_STATE_KEY``, routed to the ROOT conversation so
# approving once on the parent covers the whole spawn tree. Value is ``True``
# (a boolean flag, not a float checkpoint).
SESSION_COST_UNPRICED_APPROVED_KEY = "_policy_cost_unpriced_approved"


class UserDailyCostContext(TypedDict, total=False):
    """The session owner's per-UTC-day LLM cost rollup.

    Injected into the event context only when a policy needs it (the
    per-user daily cost-budget policy is configured); absent / empty
    otherwise. Read via ``event["context"]["user_daily_cost"]``.

    :param cost_usd: The session owner's accumulated LLM spend (USD)
        for the current UTC day, as of this turn's start, e.g.
        ``0.18``. ``0.0`` when nothing recorded yet / pricing
        unavailable.
    :param ask_approved_usd: Highest soft warning checkpoint (USD) the
        owner has already approved continuing past today, so an
        approved checkpoint does not re-prompt across the owner's
        sessions. ``0.0`` when none approved.
    :param user_id: The session owner the rollup belongs to, e.g.
        ``"alice@example.com"`` — surfaced so the budget policy can name
        whose spend tripped the gate. Absent in single-user mode (no
        owner grant), where messages fall back to an un-named phrasing.
    """

    cost_usd: float
    ask_approved_usd: float
    user_id: str


class EventContext(TypedDict, total=False):
    """Context metadata attached to every policy event.

    :param actor: The identity of the user driving the session.
    :param usage: Cumulative LLM token usage for the session.
    :param user_daily_cost: The session owner's per-UTC-day cost
        rollup (``cost_usd`` / ``ask_approved_usd``). Present only when
        the per-user daily cost-budget policy is configured; read via
        ``event["context"]["user_daily_cost"]``.
    :param model: The model the session is currently using —
        the conversation's ``model_override`` when set (e.g. via a
        mid-session ``/model`` change), else the agent spec's
        ``llm.model``. Absent when the engine could not determine a
        model. Cost-budget policies read it via
        ``event["context"]["model"]`` to gate on the active model
        (e.g. force a downgrade off an expensive model over budget),
        e.g. ``"databricks-claude-opus-4-8"`` or the tier alias
        ``"opus"``.
    :param harness: The harness running the session, e.g.
        ``"codex-native"``, stamped by a native tool hook. ``None`` /
        absent on web / API paths. Lets policies tailor messages to how
        the harness exposes model switching (codex-native is
        terminal-only). Read via ``event["context"]["harness"]``.
    :param labels: Read-only snapshot of the conversation's guardrails
        labels, e.g. ``{"cost_control.plan": "{...}"}``. Populated by
        the server-side engine; empty on paths that don't carry labels
        (the runner-local gate). Read via ``event["context"]["labels"]``.
    """

    actor: ActorContext
    usage: UsageContext
    user_daily_cost: UserDailyCostContext
    # ``str | None`` (not ``str``): the value is ``ctx.model``, which is
    # ``None`` when the engine could not determine a model — the dict carries
    # ``None``, it is not merely absent. Cost policies treat ``None`` as an
    # unknown model (fail closed).
    model: str | None
    harness: str | None
    labels: dict[str, str]


class PolicyEvent(TypedDict, total=False):
    """The event dict passed to a policy callable.

    Shape varies by ``type``:

    - ``"request"``: ``data`` is the user message string.
    - ``"tool_call"``: ``data`` is ``{"name": "<tool-name>",
      "arguments": {...}}``. ``target`` is the tool name.
    - ``"tool_result"``: ``data`` is ``{"result": <tool-output>}``.
      ``target`` is the tool name. ``request_data`` carries
      the original tool-call payload.
    - ``"response"``: ``data`` is the assistant message string.
    - ``"llm_request"``: ``data`` is a dict with the LLM call
      metadata: ``model``, ``messages_count``, ``tools_count``,
      ``system_prompt_preview``, ``last_user_message``.
      Fires per round-trip, not per turn.
    - ``"llm_response"``: ``data`` is a dict with the LLM response
      metadata: ``model``, ``text_preview``, ``tool_calls_count``,
      and optionally ``usage``. Fires per round-trip.

    :param type: Enforcement phase — ``"request"``, ``"tool_call"``,
        ``"tool_result"``, ``"response"``, ``"llm_request"``, or
        ``"llm_response"``.
    :param target: Tool name on ``tool_call`` / ``tool_result``,
        ``None`` on ``request`` / ``response``.
    :param data: Phase-specific payload. See above.
    :param context: Actor identity and usage metadata.
    :param session_state: Mutable per-session key/value store.
        Policies can read accumulated state (e.g. counters,
        flags) set by earlier policies in the same session.
        Mutations are done via ``state_updates`` in the return
        value, not by modifying this dict directly.
    :param llm_client: A
        :class:`~omnigent.policies.types.PolicyLLMClient`
        pre-configured with the server-level LLM credentials,
        or ``None`` when the server has no ``llm:`` config.
        Policy callables that need to make LLM calls (e.g.
        classify prompt difficulty) use this rather than
        constructing their own client.
    :param request_data: On ``tool_result`` phase only — the
        original tool-call payload so the policy can correlate
        request and response.
    """

    type: Literal[
        "request",
        "tool_call",
        "tool_result",
        "response",
        "llm_request",
        "llm_response",
    ]
    target: str | None
    data: Any
    context: EventContext
    session_state: dict[str, Any]
    llm_client: PolicyLLMClient | None
    request_data: Any


# ── Response (output from callable) ──────────────────────────────────────────


class StateUpdateEntry(TypedDict, total=False):
    """A single mutation to the session state.

    :param key: The state key to mutate, e.g. ``"call_count"``.
    :param action: The operation — ``"set"`` overwrites,
        ``"increment"`` adds a numeric delta, ``"delete"``
        removes, ``"append"`` appends to a list.
    :param value: The operand. Required for ``set``,
        ``increment``, ``append``; ignored for ``delete``.
    """

    key: str
    action: Literal["set", "increment", "delete", "append"]
    value: Any


class PolicyResponse(TypedDict, total=False):
    """The dict returned by a policy callable.

    Minimal form::

        {"result": "ALLOW"}

    Full form::

        {
            "result": "DENY",
            "reason": "Blocked.",
            "data": <transformed-content>,
            "state_updates": [
                {"key": "call_count", "action": "increment", "value": 1}
            ],
            "set_labels": {"integrity": "0"},
        }

    Returning ``None`` is treated as abstain (equivalent to
    ``{"result": "ALLOW"}``).

    :param result: The verdict — ``"ALLOW"``, ``"DENY"``, or
        ``"ASK"`` (case-insensitive). ``"ALLOW"`` lets the
        action proceed; ``"DENY"`` blocks it; ``"ASK"`` parks
        for user approval.
    :param reason: Human-readable explanation. Shown to the
        user on ``ASK``, included in logs on ``DENY``.
        Optional on ``ALLOW``.
    :param data: Optional replacement payload. When present on
        an ``ALLOW`` result, the enforcement site substitutes
        this value for the original event content (e.g. a
        PII-redacted version of tool arguments).
    :param state_updates: Ordered list of session state
        mutations. Applied by the engine on ``ALLOW`` and
        ``DENY``; withheld on ``ASK`` pending approval.
    :param set_labels: Label key-value writes. Filtered through
        the policy's ``set_labels`` whitelist (if declared).
    """

    result: Literal["ALLOW", "DENY", "ASK"]
    reason: str
    data: Any
    state_updates: list[StateUpdateEntry]
    set_labels: dict[str, str]


# ── Callable protocol ────────────────────────────────────────────────────────


class PolicyCallable(Protocol):
    """Protocol for a one-argument policy callable.

    Accepts an event dict and returns a response dict, a
    :class:`~omnigent.policies.types.PolicyResult`, or
    ``None`` to abstain. May be sync or async.

    Example::

        def my_policy(event: PolicyEvent) -> PolicyResponse | None:
            if event["type"] != "tool_call":
                return None
            return {"result": "ALLOW"}
    """

    def __call__(self, event: PolicyEvent) -> PolicyResponse | None: ...


class PolicyCallableWithConfig(Protocol):
    """Protocol for a two-argument policy callable.

    Same as :class:`PolicyCallable` but also receives a
    ``config`` dict (from the spec's ``config:`` block).

    Example::

        def my_policy(
            event: PolicyEvent,
            config: dict[str, str],
        ) -> PolicyResponse | None:
            threshold = int(config.get("threshold", "5"))
            ...
    """

    def __call__(self, event: PolicyEvent, config: dict[str, str]) -> PolicyResponse | None: ...


__all__ = [
    "USER_DAILY_ASK_APPROVED_STATE_KEY",
    "ActorContext",
    "EventContext",
    "PolicyCallable",
    "PolicyCallableWithConfig",
    "PolicyEvent",
    "PolicyResponse",
    "StateUpdateEntry",
    "UsageContext",
    "UserDailyCostContext",
]
