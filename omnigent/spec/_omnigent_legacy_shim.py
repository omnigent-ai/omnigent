"""
Compatibility shim: legacy omnigent ``(content, phase)`` policy
callables → omnigent ``(ctx, context)`` convention.

Legacy omnigent function policies were written like::

    def block_long_sleep(content, phase):
        if phase != "tool_call":
            return {"action": "allow"}
        if content.get("tool") != "sleep":
            ...

The ``content`` is a phase-shaped dict (``{"tool": ..., "args":
...}`` on ``tool_call``, a raw message string on
``input`` / ``output``). Agent-plane's :class:`FunctionPolicy`
dispatcher passes ``(EvaluationContext, engine_context)``
instead — same arity, totally different semantics. When a legacy
callable runs under omnigent it silently short-circuits to
``"allow"`` on the very first phase check (``phase`` is a dict
there, not a string) and any policy written in the old style
becomes a no-op.

This module bridges the gap. The omnigent→omnigent
translator in :mod:`omnigent.spec.omnigent` routes every
function policy through :func:`build` below; at policy-build
time :func:`build` imports the author's callable, inspects its
parameter names, and wraps legacy-style callables (parameter
names ``("content", "phase")``) in an adapter that converts
``(ctx, context)`` back to ``(content, phase[, context])`` on
every call. Modern callables (omnigent-native ``(ctx)`` /
``(ctx, context)`` convention) return unchanged — the shim
is a pass-through for them.

The detection is load-time; the hot evaluate() loop has no
conditional branch. One ``inspect.signature`` call per policy
at workflow startup, zero runtime introspection cost.
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Awaitable, Mapping
from typing import Protocol, cast

from omnigent.json_types import JsonObject as _JsonObject
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import Phase

# Parameter names that identify a legacy omnigent policy
# callable — the first two positional parameters must be exactly
# ``content`` then ``phase``. A callable whose first two params
# are ``(foo, bar)`` is treated as omnigent-native even though
# it has two arguments, and a callable whose params are
# ``(phase, content)`` (wrong order) is also treated as modern —
# the detection is intentionally narrow so hybrid/custom
# signatures fail loud at call time rather than being silently
# reshaped.
_LEGACY_FIRST_PARAM = "content"
_LEGACY_SECOND_PARAM = "phase"


class _PolicyCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


class _AsyncPolicyCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]:
        raise NotImplementedError


# Maps Omnigent' :class:`Phase` enum to the string literal
# the legacy callable expects as its ``phase`` argument.
_PHASE_TO_LEGACY_STR: dict[Phase, str] = {
    Phase.REQUEST: "input",
    Phase.RESPONSE: "output",
    Phase.TOOL_CALL: "tool_call",
    Phase.TOOL_RESULT: "tool_result",
    Phase.LLM_REQUEST: "llm_request",
    Phase.LLM_RESPONSE: "llm_response",
}


def build(
    target: str,
    factory_kwargs: Mapping[str, object] | None = None,
    configured_phases: list[str] | None = None,
) -> _PolicyCallable:
    """
    Factory invoked by :class:`FunctionPolicySpec` dispatch when
    loading an omnigent-sourced function policy.

    Imports *target*, optionally unwraps a factory call with
    *factory_kwargs*, then decides whether the resolved callable
    is a legacy omnigent policy (parameter names
    ``(content, phase)``) or an omnigent-native one
    (``(ctx)`` / ``(ctx, context)``). Legacy callables come
    back wrapped; modern callables come back unchanged.

    :param target: Dotted path to the author's callable, e.g.
        ``"examples.tool_functions.block_long_sleep"``. The
        translator forwards the YAML's original ``handler:``
        / ``runner:`` path here.
    :param factory_kwargs: If the YAML declared
        ``factory_params:``, these kwargs are applied by calling
        *target* as a factory first — ``target(**factory_kwargs)``
        produces the evaluator. ``None`` means *target* IS the
        evaluator directly. Example:
        ``{"limit": 5}`` for a rate-limit factory.
    :param configured_phases: The YAML ``on:`` list (verbatim
        strings, e.g. ``["tool_call", "tool_result"]``). When
        set, the wrapper injects this list into the legacy
        callable's ``context["configured_phases"]`` on every
        evaluation — matching the contract
        :class:`omnigent.inner.policies.FunctionPolicy`
        provides natively (``phase_context = {"configured_phases":
        list(self.on)}``). ``None`` means the wrapper does not
        add the key — used only by tests that build the shim
        directly. The translator
        (:func:`omnigent.spec.omnigent._translate_function_policy_yaml`)
        always passes a list.
    :returns: A callable matching Omnigent'
        ``(ctx, context)`` convention.
    :raises TypeError: If *target* (or its factory result)
        doesn't resolve to a callable.
    """
    module_path, attr = target.rsplit(".", 1)
    module = importlib.import_module(module_path)
    resolved: object = getattr(module, attr)
    if factory_kwargs:
        resolved = cast(_PolicyCallable, resolved)(**factory_kwargs)
    if not callable(resolved):
        raise TypeError(
            f"legacy-shim target {target!r} resolved to "
            f"{type(resolved).__name__}; expected a callable "
            f"(or a factory returning one when factory_kwargs is set).",
        )
    fn = cast(_PolicyCallable, resolved)
    if not _has_legacy_signature(fn):
        return fn
    return _wrap_legacy(fn, configured_phases=configured_phases)


def _has_legacy_signature(fn: _PolicyCallable) -> bool:
    """
    True iff *fn*'s first two positional parameters are named
    ``content`` then ``phase`` (the legacy omnigent convention).

    Signature-introspection failures (C builtins, Cython objects
    without ``__signature__``) return False — those are treated
    as modern, which matches the omnigent-native default.

    :param fn: The callable to inspect.
    :returns: True for legacy-style callables, False for modern.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    positional_names = [
        p.name
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return positional_names[:2] == [_LEGACY_FIRST_PARAM, _LEGACY_SECOND_PARAM]


def _coerce_legacy_return(result: object) -> object:
    """
    Convert a legacy callable's return value to the format expected
    by :func:`omnigent.policies.function._coerce_to_policy_result`.

    Legacy callables return either:

    - A :class:`PolicyResult`-shaped object (already handled by
      ``_coerce_to_policy_result`` via ``hasattr(raw, "action")``) — pass through.
    - A dict with ``"action"`` key (old format, e.g.
      ``{"action": "deny", "reason": "..."}``) — converted to the
      decision dict shape
      ``{"result": "DENY", "reason": "..."}``.
    - Anything else — pass through (the caller handles it).

    :param result: The raw return from the legacy callable.
    :returns: Decision-dict-compatible result.
    """
    if isinstance(result, dict) and "action" in result and "decision" not in result:
        action = str(result.get("action", "allow")).upper()
        decision: _JsonObject = {"result": action}
        if result.get("reason") is not None:
            decision["reason"] = result["reason"]
        return decision
    return result


def _wrap_legacy(
    fn: _PolicyCallable,
    *,
    configured_phases: list[str] | None = None,
) -> _PolicyCallable:
    """
    Wrap a legacy ``(content, phase)`` or ``(content, phase,
    context)`` callable so it can be invoked as
    ``(ctx, context)`` by :class:`FunctionPolicy`.

    Inspects the callable's arity at wrap time (once) to decide
    whether to pass a third legacy ``context`` dict, and whether
    it's async vs sync. Each call site re-uses those captured
    flags — no per-call introspection.

    Forwards a ``reset_turn`` attribute from *fn* onto the
    wrapper when present, so the omnigent ``FunctionPolicy``
    can call it at turn boundaries — preserving the legacy
    omnigent per-turn-reset contract for stateful policies
    like ``max_tool_calls_per_turn``.

    :param fn: The legacy callable.
    :param configured_phases: When set, every wrapper invocation
        will add ``"configured_phases"`` to the legacy context
        dict so legacy callables that read it (e.g. the
        Databricks ``google_policy``) see the same value
        omnigent-native would. ``None`` means the key is
        omitted from the legacy context.
    :returns: A ``(ctx, context)``-shaped wrapper that calls
        *fn* with the legacy argument shape.
    """
    wants_context = _positional_arity(fn) >= 3
    is_async = inspect.iscoroutinefunction(fn)
    # Capture once at wrap time. The omnigent FunctionPolicy
    # looks up ``reset_turn`` via ``getattr`` on the wrapper.
    reset_turn_fn = getattr(fn, "reset_turn", None)

    if is_async:
        async_fn = cast(_AsyncPolicyCallable, fn)

        async def _async_shim(
            ctx: EvaluationContext,
            context: _JsonObject,
        ) -> object:
            """Async wrapper for legacy async policies."""
            args = _convert_args(
                ctx,
                context,
                wants_context=wants_context,
                configured_phases=configured_phases,
            )
            result = await async_fn(*args)
            return _coerce_legacy_return(result)

        if callable(reset_turn_fn):
            _async_shim.reset_turn = reset_turn_fn  # type: ignore[attr-defined]
        return cast(_PolicyCallable, _async_shim)

    def _sync_shim(
        ctx: EvaluationContext,
        context: _JsonObject,
    ) -> object:
        """Sync wrapper for legacy sync policies."""
        args = _convert_args(
            ctx,
            context,
            wants_context=wants_context,
            configured_phases=configured_phases,
        )
        result = fn(*args)
        return _coerce_legacy_return(result)

    if callable(reset_turn_fn):
        _sync_shim.reset_turn = reset_turn_fn  # type: ignore[attr-defined]
    return cast(_PolicyCallable, _sync_shim)


def _positional_arity(fn: _PolicyCallable) -> int:
    """
    Count positional parameters on *fn*.

    Falls back to ``2`` on signature-introspection failure,
    matching the minimum legacy shape (``content, phase``) so
    wrapped calls don't silently lose the context argument
    on exotic callables.

    :param fn: The callable to inspect.
    :returns: Positional-parameter count.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 2
    return sum(
        1
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )


def _v0_event_to_legacy_phase(event: Mapping[str, object]) -> str:
    """
    Map an event ``type`` string to the legacy phase string.

    Event types match the inner system's phase strings directly
    (``"request"``, ``"response"``, ``"tool_call"``, ``"tool_result"``),
    but the legacy shim's ``_PHASE_TO_LEGACY_STR`` map was built for
    Omnigent' ``Phase`` enum. Provide a direct mapping here.

    :param event: An event dict with ``"type"`` key.
    :returns: The legacy phase string, e.g. ``"tool_call"``.
    """
    # Inner system uses "request"/"response"/"tool_call"/"tool_result" —
    # legacy callables expect the same strings (these were chosen to match).
    event_type = event.get("type")
    return event_type if isinstance(event_type, str) else "request"


def _v0_event_to_legacy_content(event: Mapping[str, object]) -> object:
    """
    Extract the legacy ``content`` value from an event dict.

    For ``tool_result`` phases the inner system may pass a JSON string;
    JSON-parse it so legacy callables that branch on
    ``isinstance(content, dict)`` keep working — mirroring
    :func:`_legacy_content` which does the same for
    :class:`EvaluationContext`.

    :param event: An event dict with ``"data"`` and ``"type"`` keys.
    :returns: The content in the legacy shape.
    """
    data = event.get("data")
    phase = event.get("type", "")
    if phase == "tool_result":
        return _maybe_parse_json(data)
    return data


def _convert_args(
    ctx: EvaluationContext | _JsonObject,
    context: Mapping[str, object],
    *,
    wants_context: bool,
    configured_phases: list[str] | None = None,
) -> tuple[object, ...]:
    """
    Produce ``(content, phase)`` or
    ``(content, phase, context)`` from Omnigent'
    :class:`EvaluationContext` (or an event dict) + engine-supplied
    context dict.

    Handles two calling conventions:

    - Agent-plane: ``ctx`` is an :class:`EvaluationContext`.
    - Inner system: the omnigent :class:`FunctionPolicy` builds an
      event dict and calls the wrapped function as ``fn(event, config)``
      (arity-2 dispatch). In that case ``ctx`` is the event dict and
      ``context`` is the config dict (ignored for legacy context).

    :param ctx: Agent-plane evaluation context OR an event dict
        (``{"type": phase_str, "target": tool_name, "data": content, ...}``).
    :param context: Engine-supplied context bundle (labels,
        etc.). Passed through to the legacy callable only when
        *wants_context* is True.
    :param wants_context: Whether the legacy callable takes a
        third ``context`` positional param.
    :param configured_phases: Forwarded to :func:`_legacy_context`
        when *wants_context* is True; ignored otherwise.
    :returns: The positional-args tuple to splat into the
        legacy callable.
    """
    if isinstance(ctx, dict):
        # Event dict from the inner system's _call_policy_callable.
        # The event has {"type": phase_str, "target": tool_name, "data": content, ...}
        phase_str = _v0_event_to_legacy_phase(ctx)
        legacy_content = _v0_event_to_legacy_content(ctx)
        if wants_context:
            event_context = ctx.get("context")
            labels = event_context.get("labels", {}) if isinstance(event_context, dict) else {}
            leg_ctx: _JsonObject = {"labels": labels}
            if configured_phases is not None:
                leg_ctx["configured_phases"] = configured_phases
            if ctx.get("target"):
                leg_ctx["tool_name"] = ctx["target"]
            return (legacy_content, phase_str, leg_ctx)
        return (legacy_content, phase_str)

    phase_str = _PHASE_TO_LEGACY_STR[ctx.phase]
    legacy_content = _legacy_content(ctx)
    if wants_context:
        return (
            legacy_content,
            phase_str,
            _legacy_context(ctx, context, configured_phases=configured_phases),
        )
    return (legacy_content, phase_str)


def _legacy_content(ctx: EvaluationContext) -> object:
    """
    Convert ``ctx.content`` to the per-phase shape the legacy
    callable expects.

    Agent-plane's workflow already normalizes ``ctx.content``
    into phase-specific shapes at policy-evaluation sites (see
    :func:`omnigent.runtime.workflow._enforce_tool_call_policy`
    and siblings). The legacy omnigent shape matches those
    normalized shapes almost exactly — this helper bridges the
    per-phase differences:

    - ``INPUT`` / ``OUTPUT``: omnigent passes the message
      text directly; legacy omnigent expects the same. Pass
      through.
    - ``TOOL_CALL``: omnigent passes
      ``{"tool": name, "args": parsed_args_dict}``; legacy
      omnigent expects the same. Pass through.
    - ``TOOL_RESULT``: omnigent passes the raw tool output
      string. Legacy omnigent passes the parsed result *dict*
      via
      :func:`omnigent.inner.mcp_tools._extract_call_result_payload`
      — when an MCP server emits a single JSON-formatted text
      block (the common shape for the Databricks Google /
      Glean MCP tools and any FastMCP-based server returning a
      structured payload), native omnigent hands the policy
      a dict, not a string. Mirror that here so legacy
      callables that branch on ``isinstance(content, dict)``
      (e.g. the Databricks ``google_policy``'s
      file-id tracking on ``tool_result``) keep working.
      JSON-parse the omnigent string; on parse failure pass
      the raw string through (matches native fallback).

    :param ctx: The evaluation context.
    :returns: Content in the legacy shape.
    """
    if ctx.phase in (Phase.REQUEST, Phase.RESPONSE, Phase.TOOL_CALL):
        return ctx.content
    # TOOL_RESULT — JSON-parse the string so legacy callables
    # branching on ``isinstance(content, dict)`` keep working.
    # Non-JSON strings pass through unchanged.
    return _maybe_parse_json(ctx.content)


def _maybe_parse_json(value: object) -> object:
    """
    Try to parse a JSON-encoded string into its native form.

    Used by :func:`_legacy_content` on ``TOOL_RESULT`` to mirror
    omnigent-native's behavior: when an MCP text block is
    JSON, ``_extract_call_result_payload`` returns the parsed
    object; non-JSON text passes through verbatim.

    :param value: The candidate string (or any other value —
        non-strings pass through unchanged because there's
        nothing to parse).
    :returns: The parsed JSON value when *value* is a JSON
        string; otherwise *value* unchanged.
    """
    if not isinstance(value, str):
        return value
    try:
        parsed: object = json.loads(value)
        return parsed
    except (json.JSONDecodeError, ValueError):
        return value


def _legacy_context(
    ctx: EvaluationContext,
    engine_context: Mapping[str, object],
    *,
    configured_phases: list[str] | None = None,
) -> _JsonObject:
    """
    Build the ``context`` dict a 3-arg legacy callable expects.

    Legacy omnigent passed ``{"labels": {...},
    "configured_phases": [...], "tool_name": "sleep"}`` — but
    ``tool_name`` is *only* added on ``TOOL_RESULT`` (see
    :meth:`omnigent.inner.session.Session._apply_tool_result_policy`,
    which builds ``context = {"tool_name": tool_name}`` for
    that phase). On ``TOOL_CALL``, native omnigent passes no
    extra context — the legacy callable reads the tool name
    from ``content["tool"]`` instead (see
    :meth:`Session._apply_tool_call_policy`,
    ``evaluate({"tool": name, "args": args}, "tool_call")``).

    Agent-plane's engine context already carries ``labels``,
    and the per-policy ``configured_phases`` is captured at
    shim build time (matches
    :class:`omnigent.inner.policies.FunctionPolicy.evaluate`,
    which sets ``phase_context = {"configured_phases":
    list(self.on)}``).

    :param ctx: The evaluation context.
    :param engine_context: Engine-supplied context dict.
    :param configured_phases: The policy's YAML ``on:`` list,
        forwarded by :func:`build`. ``None`` omits the key —
        kept for callers that build the shim directly without
        a translator.
    :returns: A fresh dict combining all sources. Never
        mutates *engine_context*.
    """
    legacy: _JsonObject = {"labels": engine_context.get("labels", {})}
    if configured_phases is not None:
        legacy["configured_phases"] = list(configured_phases)
    # Native omnigent only adds ``tool_name`` on
    # ``TOOL_RESULT``. Mirror that exactly so callables that
    # use ``"tool_name" in context`` to discriminate phase
    # behave identically across legacy and Omnigent mode.
    if ctx.phase == Phase.TOOL_RESULT and ctx.tool_name is not None:
        legacy["tool_name"] = ctx.tool_name
    return legacy
