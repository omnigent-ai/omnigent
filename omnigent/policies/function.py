"""
:class:`FunctionPolicy` — policy backed by a Python callable.

The callable receives two arguments: ``event`` and ``config``.

``event`` is a dict shaped::

    {
        "type": "input" | "tool_call" | "tool_result" | "output",
        "target": "<tool-name-or-None>",
        "data": <phase-specific-payload>,
        "context": {"actor": {"run_as": "...", "client_id": "..."},
                    "usage": {"input_tokens": N, ...}},
        "session_state": {...},
        "llm_client": <PolicyLLMClient-or-None>,
        "request_data": <original-tool-call-on-TOOL_RESULT>,
    }

``config`` is a dict of runtime key-value pairs declared at
policy attachment time (the ``config:`` block in the YAML spec).

The callable must return a dict shaped::

    {
        "result": "ALLOW" | "DENY" | "ASK",
        "reason": "human-readable string or null"
    }

Two YAML shapes parse into a :class:`FunctionRef`:

- ``function: myorg.policies.rate_limit`` — the resolved
  dotted path IS the evaluator, called directly.
- ``function: {path: ..., arguments: {...}}`` — the resolved
  path is a factory, called once at build time with
  ``**arguments`` and returning the evaluator. Enables
  closure-state policies (rate limits, budgets).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Callable
from typing import Any

from omnigent.policies.base import Policy
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PolicyAction,
    StateUpdate,
    StateUpdateAction,
)

# Type alias for what a resolved FunctionPolicy callable can be.
# Distinguishing form handled at call time — the adapter wraps
# each variant into a uniform async call.
_PolicyCallable = Callable[..., Any]


def _phase_to_event_type(phase: Phase) -> str:
    """
    Map a :class:`Phase` enum to the ``event.type`` string.

    The wire encoding uses ``"request"`` / ``"response"`` for the
    pre-LLM / post-LLM phases and the full four-phase vocabulary
    for tool call and result phases.

    :param phase: The enforcement point enum value.
    :returns: The phase's wire string, e.g. ``"tool_call"``.
    """
    return phase.value


class FunctionPolicy(Policy):
    """
    A policy driven by a Python callable (POLICIES.md §9.1).

    The callable receives an ``event`` dict and a ``config`` dict.
    Sync callables are dispatched to a thread via
    :func:`asyncio.to_thread` so async policies (e.g. a
    PromptPolicy running alongside) are not blocked.

    :param spec: The :class:`FunctionPolicySpec` this policy
        was built from.
    :param callable_obj: The resolved callable. Either the
        evaluator directly (short-form spec) or the result of
        calling the factory with ``spec.function.arguments``
        (dict-form spec).
    """

    spec: FunctionPolicySpec

    def __init__(
        self,
        spec: FunctionPolicySpec,
        callable_obj: _PolicyCallable,
    ) -> None:
        """
        Wrap a resolved callable in the Policy contract.

        :param spec: The spec declaration.
        :param callable_obj: The evaluator callable (already
            unwrapped from any factory).
        """
        self.spec = spec
        self._callable = callable_obj
        self._is_async = inspect.iscoroutinefunction(callable_obj)
        self._arity = _callable_arity(callable_obj)
        self._config: dict[str, Any] = dict(spec.config) if spec.config else {}

    async def evaluate(
        self,
        ctx: EvaluationContext,
        context: dict[str, Any],  # noqa: ARG002 — engine contract; callable uses event.context instead
    ) -> PolicyResult:
        """
        Build an event dict and invoke the underlying callable.

        The engine is responsible for selector + condition
        gating + action whitelist validation +
        set_labels filtering; this method only:

        1. Builds the ``event`` dict from the
           :class:`EvaluationContext`.
        2. Dispatches sync vs async correctly.
        3. Passes ``event`` alone or ``event, config`` based
           on signature arity.
        4. Coerces the ``{"result": ..., "data": ...}``
           return into a :class:`PolicyResult`.
        5. Lets any exception bubble up — the engine wraps it
           in fail-closed DENY (or substituted ALLOW under the
           classifier-only carve-out).

        :param ctx: Current evaluation context.
        :param context: Read-only engine context bundle
            (labels, conversation_id). Not passed to the
            callable — use ``event.context`` instead.
        :returns: Normalized :class:`PolicyResult` with
            ``deciding_policy=None`` (engine sets it).
        :raises Exception: Propagates any callable-raised
            exception; the engine converts it.
        """
        raw = await self._call(ctx)
        return _coerce_to_policy_result(raw, spec_name=self.spec.name)

    def reset_turn(self) -> None:
        """
        Forward the per-turn reset to the underlying callable.

        Stateful policy callables (notably the legacy omnigent
        ``max_tool_calls_per_turn`` factory) attach a
        ``reset_turn`` attribute to their returned evaluator so
        the engine can clear per-turn counters at turn
        boundaries. Without this delegation, those counters
        would accumulate forever and a "15 calls per turn"
        limit would silently degrade to "15 calls per session"
        under Omnigent mode — see
        :meth:`omnigent.runtime.policies.engine.PolicyEngine.reset_turn`
        for the native equivalent we mirror.

        Stateless callables (no ``reset_turn`` attribute) are a
        no-op. Modern omnigent-native callables that need
        per-turn state should follow the same convention.
        """
        reset_fn = getattr(self._callable, "reset_turn", None)
        if callable(reset_fn):
            reset_fn()

    async def _call(
        self,
        ctx: EvaluationContext,
    ) -> Any:
        """
        Build an event dict and dispatch the callable.

        :param ctx: The evaluation context.
        :returns: The raw callable return value (should be a
            ``{"result": ..., "reason": ...}`` dict).
        """
        event = _build_event(ctx)
        args: tuple[Any, ...]
        if self._arity >= 2:
            args = (event, self._config)
        else:
            args = (event,)
        if self._is_async:
            return await self._callable(*args)
        return await asyncio.to_thread(self._callable, *args)


def _build_event(ctx: EvaluationContext) -> dict[str, Any]:
    """
    Build an ``event`` dict from an :class:`EvaluationContext`.

    The event shape::

        {
            "type": "input" | "tool_call" | "tool_result" | "output",
            "target": "<tool-name-or-None>",
            "data": <phase-specific-payload>,
            "context": {"actor": {"run_as": "...", "client_id": "..."},
                        "usage": {"input_tokens": N, ...},
                        "model": "<active-model-or-None>"},
            "session_state": {...},
            "llm_client": <PolicyLLMClient-or-None>,
            "request_data": <original-tool-call-on-TOOL_RESULT>,
        }

    On ``TOOL_RESULT`` phase, ``request_data`` carries the
    original tool-call payload so ON RESULT policies can
    correlate request/response.

    :param ctx: The evaluation context populated by the caller.
    :returns: Event dict ready for the callable.
    """
    event: dict[str, Any] = {
        "type": _phase_to_event_type(ctx.phase),
        "target": ctx.tool_name,
        "data": ctx.content,
        "context": {
            "actor": dict(ctx.actor) if ctx.actor else {},
            "usage": dict(ctx.usage) if ctx.usage else {},
            # The session owner's per-UTC-day cost rollup
            # ({"cost_usd", "ask_approved_usd"}), injected by the engine
            # only when the per-user daily cost-budget policy is present;
            # empty dict otherwise (that policy treats it as $0 → never trips).
            "user_daily_cost": dict(ctx.user_daily_cost) if ctx.user_daily_cost else {},
            # The session's current model (model_override or spec llm.model),
            # injected by the engine. ``None`` when undeterminable — cost
            # policies treat that as "cannot confirm a cheaper model".
            "model": ctx.model,
            # The harness (e.g. "codex-native"), stamped by a native tool
            # hook so policies can tailor messages to it. ``None`` when
            # unstamped (web / API).
            "harness": ctx.harness,
            # Conversation labels (engine hot cache), empty when unpopulated.
            "labels": dict(ctx.labels) if ctx.labels is not None else {},
            # Subtree-scoped cumulative cost (this conversation + its
            # descendants only), injected by the engine only when a
            # subagent_cost_budget policy is present; empty dict otherwise.
            "subtree_usage": dict(ctx.subtree_usage) if ctx.subtree_usage else {},
        },
        # Mutable per-conversation state readable by the callable.
        # Empty dict when no policy has written state yet; the engine
        # populates this before dispatch.
        "session_state": dict(ctx.session_state) if ctx.session_state is not None else {},
        # Server-level LLM client for policy callables that need to
        # make LLM calls (e.g. classify prompt difficulty). None when
        # the server has no ``llm:`` config.
        "llm_client": ctx.llm_client,
    }
    if ctx.request_data is not None:
        event["request_data"] = ctx.request_data
    return event


def resolve_function_policy(spec: FunctionPolicySpec) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` from its spec.

    Resolves ``spec.function.path`` via :mod:`importlib`;
    when the spec supplies ``arguments``, treats the
    resolved path as a factory and calls it with those
    kwargs. The factory's return value is the evaluator.

    :param spec: Parsed :class:`FunctionPolicySpec` from the
        YAML policies block.
    :returns: A :class:`FunctionPolicy` ready to evaluate.
    :raises ImportError: If the dotted path cannot be
        imported.
    :raises AttributeError: If the target attribute is not
        present on the resolved module.
    :raises ValueError: If ``spec.function`` is absent (the
        parser should have rejected this — fail loud here
        rather than silently build a broken policy).
    """
    func_ref = spec.function
    if func_ref is None:
        raise ValueError(
            f"FunctionPolicy {spec.name!r} has no function reference; "
            f"parser should have rejected this at spec load.",
        )
    target = _resolve_dotted_path(func_ref.path)
    # ``arguments is not None`` distinguishes factory form (dict,
    # possibly empty ``{}``) from direct-callable form (``None``).
    # Using truthiness (``if func_ref.arguments``) would treat
    # ``{}`` as direct-callable, skipping the factory invocation
    # and returning the factory function itself as the evaluator.
    #
    # When ``arguments is None`` (legacy DB rows, YAML short form),
    # detect factories by signature: if the callable has no required
    # positional parameters (only keyword-with-defaults), it's a
    # factory — call it to produce the evaluator. Direct callables
    # (e.g. ``ask_on_os_tools(event)``) have a required first param.
    if func_ref.arguments is not None:
        callable_obj = target(**func_ref.arguments)
    elif _has_no_required_params(target):
        # Factory with all-default params — invoke to get evaluator.
        callable_obj = target()
    else:
        callable_obj = target
    if not callable(callable_obj):
        raise ValueError(
            f"FunctionPolicy {spec.name!r}: resolved object at "
            f"{func_ref.path!r} is not callable (got "
            f"{type(callable_obj).__name__})",
        )
    # Deferred import avoids a circular init cycle:
    # function.py → _omnigent_legacy_shim → policies.types →
    # policies.__init__ → function.py (partially initialised).
    from omnigent.spec._omnigent_legacy_shim import (
        _has_legacy_signature,
        _wrap_legacy,
    )

    if _has_legacy_signature(callable_obj):
        callable_obj = _wrap_legacy(callable_obj)
    return FunctionPolicy(spec, callable_obj)


def _resolve_dotted_path(path: str) -> Any:
    """
    Resolve a ``module.sub.attr`` style path to its attribute.

    Splits on the last dot: everything before is the module
    path, the trailing component is the attribute name. A
    single-segment path is treated as a module-level import
    with no attribute — not useful in practice, so we reject.

    :param path: Dotted import path, e.g.
        ``"myorg.policies.search_rate_limit"``.
    :returns: The attribute found at ``module.attr``.
    :raises ValueError: On single-segment paths.
    :raises ImportError: If the module does not import.
    :raises AttributeError: If the attribute is missing.
    """
    if "." not in path:
        raise ValueError(
            f"function path {path!r} must be a dotted module.attribute "
            f"reference (e.g. 'myorg.policies.rate_limit')",
        )
    module_path, attr = path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _callable_arity(fn: _PolicyCallable) -> int:
    """
    Count the positional parameters a callable accepts.

    Used to decide whether to pass just ``event`` or
    ``event, config`` at dispatch time. ``*args`` /
    ``**kwargs`` count as 0 here — policies that want them
    must declare explicit ``event`` / ``config`` parameters.

    Returns 1 on signature-introspection failure so the
    single-arg call path is attempted first (the caller
    surfaces errors from the actual call, not a brittle
    signature-parse).

    :param fn: The callable to inspect.
    :returns: Count of positional parameters (``POSITIONAL_ONLY``
        + ``POSITIONAL_OR_KEYWORD``).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 1
    positional_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    return sum(1 for p in sig.parameters.values() if p.kind in positional_kinds)


def _has_no_required_params(fn: _PolicyCallable) -> bool:
    """
    Check whether a callable has zero required positional params.

    Used to auto-detect factory callables when ``arguments`` is
    ``None`` (legacy DB rows). Factories like
    ``deny_pii_in_llm_request(pii_types=None, action="DENY")``
    have only keyword-with-default params — no required positional
    args. Direct callables like ``ask_on_os_tools(event)`` have
    one required positional arg.

    Returns ``False`` on introspection failure (conservative: treat
    as direct callable).

    :param fn: The callable to inspect.
    :returns: ``True`` when every positional parameter has a
        default value.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    positional_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    return all(
        p.default is not inspect.Parameter.empty
        for p in sig.parameters.values()
        if p.kind in positional_kinds
    )


def make_fixed_action_callable(
    *,
    action: str = "allow",
    reason: str | None = None,
    set_labels: dict[str, str] | None = None,
    on_phases: list[str] | None = None,
    on_tools: list[str] | None = None,
) -> _PolicyCallable:
    """
    Factory that returns a policy callable emitting a fixed decision.

    Declared under ``function: {path: ..., arguments: {...}}``
    so the standard :func:`resolve_function_policy` can construct
    it. Useful for declarative policies that need a static
    action (allow/deny/ask) with optional label writes and
    phase/tool filtering.

    Because ``type: function`` policies have ``on=None`` at the spec
    level (the parser strips it), phase/tool filtering must be baked
    into the callable itself. When *on_phases* or *on_tools* is set,
    events that don't match are abstained from (return ``None`` ->
    ALLOW fallback).

    :param action: The action string (``"allow"``, ``"deny"``,
        ``"ask"``), e.g. ``"deny"``.
    :param reason: Human-readable reason, e.g.
        ``"Untrusted content plus shell is disallowed."``.
    :param set_labels: Label writes to emit, e.g.
        ``{"integrity": "0"}``.
    :param on_phases: If set, only fire on these event types
        (``"request"``, ``"tool_call"``, ``"tool_result"``,
        ``"response"``). ``None`` means fire on all phases.
    :param on_tools: If set, only fire when
        ``event["target"]`` is one of these tool names.
        ``None`` means no tool-name filtering.
    :returns: A one-arg callable suitable for a
        :class:`FunctionPolicy`.
    """
    frozen_labels = dict(set_labels) if set_labels else None
    frozen_phases = set(on_phases) if on_phases else None
    frozen_tools = set(on_tools) if on_tools else None

    def _fixed(event: dict[str, Any]) -> dict[str, Any] | None:
        """Return the fixed decision, or None to abstain."""
        # Phase gate: abstain if the event type is not in the
        # declared phases.
        if frozen_phases is not None:
            if event.get("type") not in frozen_phases:
                return None
        # Tool gate: abstain if the tool name doesn't match.
        if frozen_tools is not None:
            if event.get("target") not in frozen_tools:
                return None
        result: dict[str, Any] = {"result": action, "reason": reason}
        if frozen_labels:
            result["set_labels"] = frozen_labels
        return result

    return _fixed


def _coerce_to_policy_result(raw: Any, *, spec_name: str) -> PolicyResult:
    """
    Normalize a FunctionPolicy callable's return value.

    Accepts the policy callable output shape::

        {"result": "ALLOW"|"DENY"|"ASK", "reason": "...",
         "data": <optional-transformed-content>,
         "state_updates": [{"key": "k", "action": "set", "value": v}, ...]}

    The ``state_updates`` field accepts a list of dicts with
    ``key``/``action``/``value`` entries. A legacy ``dict[str, Any]``
    (flat key-value map) is also accepted for backward
    compatibility and converted to a list of ``SET`` operations.

    Also accepts:

    - ``None`` — treated as ALLOW (defensive fallback; callables should
      return an explicit ALLOW decision rather than relying on this).
    - :class:`PolicyResult` — returned as-is.
    - A foreign PolicyResult-shaped object (any object with at
      least ``.action``, e.g. ``omnigent.policies.PolicyResult``
      returned by callables ported from the omnigent examples).
    - Anything else → :class:`TypeError` with a clear
      message. The engine catches it and fails closed (or
      substitutes ALLOW under the carve-out).

    :param raw: The raw return value.
    :param spec_name: Policy name for the error message.
    :returns: A :class:`PolicyResult`.
    :raises TypeError: On unrecognized return shape.
    """
    if raw is None:
        # Defensive fallback — callables should return explicit ALLOW.
        return PolicyResult(action=PolicyAction.ALLOW)
    if isinstance(raw, PolicyResult):
        return raw
    if isinstance(raw, dict):
        return _policy_result_from_dict(raw, spec_name=spec_name)
    if hasattr(raw, "action"):
        # Cross-package PolicyResult-shaped object. Read
        # ``action``/``reason``/``set_labels`` structurally —
        # isinstance would fail on types like
        # ``omnigent.policies.PolicyResult`` that happen to
        # share the class name but come from a different
        # module, producing the tautological error message
        # described in the docstring above.
        action_raw = getattr(raw.action, "value", raw.action)
        try:
            action = PolicyAction(str(action_raw).lower())
        except ValueError as exc:
            raise ValueError(
                f"FunctionPolicy {spec_name!r} returned invalid action "
                f"{action_raw!r}; must be one of 'allow', 'ask', 'deny'",
            ) from exc
        raw_set_labels = getattr(raw, "set_labels", None)
        raw_state_updates = getattr(raw, "state_updates", None)
        return PolicyResult(
            action=action,
            reason=getattr(raw, "reason", None),
            set_labels=(dict(raw_set_labels) if isinstance(raw_set_labels, dict) else None),
            state_updates=_coerce_state_updates(raw_state_updates, spec_name=spec_name),
        )
    raise TypeError(
        f"FunctionPolicy {spec_name!r} returned unsupported type "
        f"{type(raw).__name__}; expected dict with 'result' key or PolicyResult",
    )


def _policy_result_from_dict(
    raw: dict[str, Any],
    *,
    spec_name: str,
) -> PolicyResult:
    """
    Parse a policy callable's dict return into a
    :class:`PolicyResult`.

    Accepts the flat shape::

        {"result": "ALLOW"|"DENY"|"ASK", "reason": "...",
         "data": ..., "state_updates": [...], "set_labels": {...}}

    :param raw: The callable's dict return.
    :param spec_name: Policy name for error messages.
    :returns: A :class:`PolicyResult` with the corresponding
        action / reason / data.
    :raises ValueError: If ``result`` is missing or not a
        valid :class:`PolicyAction` value.
    """
    result_raw = raw.get("result")
    if result_raw is None:
        raise ValueError(
            f"FunctionPolicy {spec_name!r} dict return missing 'result' key",
        )
    try:
        if isinstance(result_raw, PolicyAction):
            action = result_raw
        else:
            # Accept both "ALLOW" and "allow" forms.
            action_value = getattr(result_raw, "value", result_raw)
            action = PolicyAction(str(action_value).lower())
    except ValueError as exc:
        raise ValueError(
            f"FunctionPolicy {spec_name!r} returned invalid decision result "
            f"{result_raw!r}; must be one of 'allow', 'ask', 'deny' "
            f"(case-insensitive)",
        ) from exc
    raw_state_updates = raw.get("state_updates")
    raw_set_labels = raw.get("set_labels")
    return PolicyResult(
        action=action,
        reason=raw.get("reason"),
        data=raw.get("data"),
        state_updates=_coerce_state_updates(raw_state_updates, spec_name=spec_name),
        set_labels=dict(raw_set_labels) if isinstance(raw_set_labels, dict) else None,
    )


def _coerce_state_updates(
    raw: Any,
    *,
    spec_name: str,
) -> list[StateUpdate] | None:
    """
    Normalize raw ``state_updates`` into a list of
    :class:`StateUpdate`.

    Accepts four shapes:

    1. ``None`` → ``None`` (no updates).
    2. ``list[dict]`` — each dict has ``key``, ``action``,
       and optional ``value``. This is the canonical form.
    3. ``dict[str, Any]`` — legacy flat key-value map, converted
       to a list of ``SET`` operations for backward compat.
    4. ``list[StateUpdate]`` — already coerced (from a
       cross-package PolicyResult-shaped object), returned as-is.

    :param raw: The raw ``state_updates`` value from the
        callable's return dict.
    :param spec_name: Policy name for error messages.
    :returns: A list of :class:`StateUpdate` or ``None``.
    :raises ValueError: On invalid action strings.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        # Legacy compat: {"call_count": 5} → [SET("call_count", 5)]
        return [StateUpdate(key=k, action=StateUpdateAction.SET, value=v) for k, v in raw.items()]
    if isinstance(raw, list):
        result: list[StateUpdate] = []
        for entry in raw:
            if isinstance(entry, StateUpdate):
                result.append(entry)
                continue
            if not isinstance(entry, dict):
                raise TypeError(
                    f"FunctionPolicy {spec_name!r}: state_updates entry "
                    f"must be a dict, got {type(entry).__name__}",
                )
            key = entry.get("key")
            action_raw = entry.get("action")
            if key is None or action_raw is None:
                raise ValueError(
                    f"FunctionPolicy {spec_name!r}: state_updates entry "
                    f"missing required 'key' or 'action' field",
                )
            try:
                action = StateUpdateAction(str(action_raw).lower())
            except ValueError as exc:
                raise ValueError(
                    f"FunctionPolicy {spec_name!r}: invalid state_updates "
                    f"action {action_raw!r}; must be one of "
                    f"'set', 'increment', 'delete', 'append'",
                ) from exc
            result.append(StateUpdate(key=key, action=action, value=entry.get("value")))
        return result if result else None
    return None
