"""Built-in policy engine that delegates verdicts to an MCP Interceptor PDP.

Most built-in policies decide *locally* — they inspect the event and return a
verdict from in-process Python. This module is different: it turns Omnigent's
policy engine into a thin client of an **external Policy Decision Point (PDP)**
that speaks the **MCP Interceptor** JSON-RPC protocol
(``modelcontextprotocol/experimental-ext-interceptors``). The decision is made
remotely; this policy only translates between Omnigent's :class:`PolicyEvent` /
:class:`PolicyResponse` contract and the interceptor wire format, then maps the
PDP's validation result back.

Why this exists
---------------
An organisation that already runs a central policy service (any service
implementing the MCP Interceptor spec) wants *one* place to author and audit
tool-call policy. Rather than re-implementing those rules as Omnigent built-ins,
an admin can attach this single policy and point it at the PDP. Every
``tools/call`` the agent attempts is validated against the org's policies before
it executes.

The MCP Interceptor protocol
----------------------------
Two JSON-RPC 2.0 methods, both reached by ``POST`` to a single endpoint.
Authentication is implementation-defined; this client sends an optional
``Authorization: Bearer <token>`` when one is configured.

``interceptors/list`` — discovery. Returns the interceptors the PDP exposes::

    {"jsonrpc": "2.0", "id": 1, "method": "interceptors/list"}

``interceptor/invoke`` — the call this policy makes on every tool call. The tool
call rides in ``payload``; identity/correlation in ``context``::

    {
      "jsonrpc": "2.0",
      "id": "<uuid>",
      "method": "interceptor/invoke",
      "params": {
        "name": "default",
        "event": "tools/call",
        "phase": "request",
        "payload": {
          "method": "tools/call",
          "params": {"name": "<tool>", "arguments": {...}}
        },
        "context": {
          "principal": {"type": "user", "id": "alice@example.com"},
          "traceId": "<32-hex otel trace id>",
          "timestamp": "2026-01-01T00:00:00Z"
        }
      }
    }

A *validator* interceptor replies with a ``result`` of the shape
(``ValidationResult``)::

    {
      "interceptor": "<name>",
      "type": "validation",
      "phase": "request",
      "valid": true | false,
      "severity": "info" | "warn" | "error",
      "messages": [{"path": "<field>", "message": "<text>", "severity": "..."}]
    }

Verdict mapping
---------------
The spec gates blocking on severity ("Only validations with ``severity: error``
MUST block execution") and reports the overall outcome in ``valid``:

==========================================  =============================
ValidationResult                            Omnigent verdict
==========================================  =============================
``severity: "error"``                       ``DENY``
``severity: "warn"`` or ``valid: false``    configurable via ``on_notify``
``valid: true`` (no error/warn)             ``ALLOW``
==========================================  =============================

``warn`` is advisory in the spec ("review recommended"); the closest Omnigent
semantics is ``ASK`` — pause and let the human decide — so that is the
``on_notify`` default. Set ``on_notify: allow`` to treat warnings as
pass-through (audit-only), or ``on_notify: deny`` to harden them into blocks.

Failure handling
----------------
If the PDP is unreachable, times out, or returns a malformed body, the policy
honours ``fail_open``:

- ``fail_open: false`` (default) — return ``DENY``. This matches Omnigent's
  fail-closed posture for ``TOOL_CALL`` (see
  :data:`omnigent.policies.types.FAIL_CLOSED_PHASES`): a PDP you cannot reach
  must not silently wave tool calls through.
- ``fail_open: true`` — return ``ALLOW``. Use only when availability matters
  more than enforcement (e.g. a non-critical advisory deployment).

Phase scope
-----------
By default the policy only forwards ``tool_call`` events. Each event carries an
interceptor ``phase``: ``tool_call`` is sent as ``phase: "request"`` (pre-
execution check) and ``tool_result`` as ``phase: "response"`` (post-execution
audit). Add ``tool_result`` to ``phases`` to also validate tool *output* (the
result rides in ``payload.params.arguments.result``).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

# Event types this policy understands. The MCP Interceptor spec models tool
# calls as the ``tools/call`` event, so ``tool_call`` is the default;
# ``tool_result`` is opt-in for deployments that also want to validate output.
_SUPPORTED_PHASES: frozenset[str] = frozenset({"tool_call", "tool_result"})

# Allowed values for the ``on_notify`` knob -> Omnigent verdict. Drives how an
# advisory validation result (severity ``warn``, or a non-error ``valid: false``)
# is handled.
_ON_NOTIFY_TO_VERDICT: dict[str, str] = {
    "allow": "ALLOW",
    "ask": "ASK",
    "deny": "DENY",
}

_ALLOW: PolicyResponse = {"result": "ALLOW"}


def _resolve_api_key(api_key: str | None, api_key_env: str | None) -> str | None:
    """Resolve the bearer token from a literal value or an env var.

    Preferring ``api_key_env`` keeps the secret out of the agent/server YAML
    (and out of any spec bundle that gets uploaded). A literal ``api_key`` is
    supported for local development and tests.

    :param api_key: Literal token, or ``None``.
    :param api_key_env: Name of an environment variable holding the token, or
        ``None``. Takes precedence over ``api_key`` when both are set and the
        variable is present.
    :returns: The resolved token, or ``None`` when neither source yields one.
    """
    if api_key_env:
        from_env = os.environ.get(api_key_env)
        if from_env:
            return from_env
    return api_key


def _principal(event: PolicyEvent) -> dict[str, Any]:
    """Build the spec ``context.principal`` from the event's actor.

    The interceptor ``context`` carries ``principal: {type, id, claims}``. We map
    the authenticated user's email (``run_as``) to a ``"user"`` principal, an
    OAuth ``client_id`` to a ``"service"`` principal, and otherwise to
    ``"anonymous"`` so the PDP always receives a well-formed principal.

    :param event: The policy event.
    :returns: A ``principal`` object, e.g. ``{"type": "user", "id": "a@b.com"}``.
    """
    actor = event.get("context", {}).get("actor", {}) or {}
    run_as = actor.get("run_as")
    if run_as:
        return {"type": "user", "id": str(run_as)}
    client_id = actor.get("client_id")
    if client_id:
        return {"type": "service", "id": str(client_id)}
    return {"type": "anonymous"}


def _current_trace_id() -> str | None:
    """Active OTel trace id as 32-char hex, or ``None`` when no span is active.

    The interceptor opens an MLflow/OTel TOOL span around each decision (see
    :func:`_start_tool_span`), which is the active span while this policy runs.
    Surfacing its trace id as the spec ``context.traceId`` lets the PDP correlate
    its decision with the SAME trace the UI shows for this tool call. Falls back
    to ``None`` when tracing is off or no span is active.
    """
    try:
        from opentelemetry import trace as _ot

        ctx = _ot.get_current_span().get_span_context()
        if ctx is not None and ctx.trace_id:  # trace_id == 0 ⇒ no valid span
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 — best-effort tracing, never break the run
        pass
    return None


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the spec ``context.timestamp``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_payload(event: PolicyEvent, tool_name: str) -> dict[str, Any]:
    """Build the spec ``payload`` for a ``tools/call`` invocation.

    Request phase carries the tool call ``{method, params: {name, arguments}}``.
    Response phase carries the tool output under ``params.arguments.result`` so
    the same policy can fire on both phases.

    :param event: The policy event (``tool_call`` or ``tool_result``).
    :param tool_name: Resolved tool name.
    :returns: The ``payload`` object for ``interceptor/invoke``.
    """
    data = event.get("data") or {}
    if event.get("type") == "tool_result":
        arguments: dict[str, Any] = {"result": data.get("result")}
    else:
        arguments = data.get("arguments") or {}
    return {
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def _build_context(event: PolicyEvent) -> dict[str, Any]:
    """Build the spec ``context`` (principal, traceId, timestamp).

    The active OTel trace id is surfaced as ``traceId`` so the PDP can correlate
    its decision with the SAME trace the UI shows for this tool call.

    :param event: The policy event.
    :returns: The ``context`` object for ``interceptor/invoke``.
    """
    context: dict[str, Any] = {
        "principal": _principal(event),
        "timestamp": _utc_now_iso(),
    }
    trace_id = _current_trace_id()
    if trace_id:
        context["traceId"] = trace_id
    return context


def _format_reason(result: dict[str, Any]) -> str:
    """Build a human-readable reason from a ``ValidationResult``.

    Joins the validation ``messages`` (prefixed with ``severity`` and ``path``
    when present) so the user sees which checks failed and why on an ASK or DENY.

    :param result: The interceptor ``result`` object.
    :returns: A single reason string.
    """
    parts: list[str] = []
    for msg in result.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        text = msg.get("message")
        if not text:
            continue
        prefix = ""
        severity = msg.get("severity")
        if severity:
            prefix += f"[{severity}] "
        path = msg.get("path")
        if path:
            prefix += f"{path}: "
        parts.append(f"{prefix}{text}")
    return "; ".join(parts) or "Denied by interceptor policy."


def _start_tool_span(tool_name: str, model: str | None = None):  # type: ignore[no-untyped-def]
    """Best-effort MLflow ``execute_tool`` span around one PDP decision.

    The omnigent guardrail engine evaluates this policy with no span active, so
    the governed tool call is otherwise invisible to tracing and has no real
    trace to correlate the PDP's decision with. Opening a span here mirrors the
    claude-sdk executor's :func:`_start_tool_call_span`: it makes the tool call a
    trace in the backend (carrying a ``blocked`` disposition on a DENY) and gives
    :func:`_current_trace_id` an active OTel trace id to send as the spec
    ``context.traceId``. No-op when mlflow/tracing is unavailable.
    """
    import contextlib

    try:
        import mlflow
    except Exception:  # noqa: BLE001 — best-effort tracing, never break the run
        return contextlib.nullcontext(None)
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool_name,
        "gen_ai.tool.call.id": uuid.uuid4().hex,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    try:
        return mlflow.start_span(
            name=f"execute_tool {tool_name}", span_type="TOOL", attributes=attrs
        )
    except Exception:  # noqa: BLE001 — best-effort tracing, never break the run
        return contextlib.nullcontext(None)


def _record_tool_decision(span, blocked: bool) -> None:  # type: ignore[no-untyped-def]
    """Mark the tool span's outcome.

    A policy block is recorded as an OTLP ERROR span status (the tool call was
    denied and never ran). The "blocked" disposition is derived downstream from
    the policy decision, not from a span attribute.
    """
    if span is None:
        return
    try:
        if blocked:
            span.set_status("ERROR")
    except Exception:  # noqa: BLE001 — best-effort tracing, never break the run
        pass


def _flush_tool_span() -> None:
    """Best-effort force-flush so the span exports before an ephemeral run exits."""
    try:
        from opentelemetry import trace as _ot

        _ot.get_tracer_provider().force_flush(5000)
    except Exception:  # noqa: BLE001 — best-effort tracing, never break the run
        pass


def mcp_interceptor(
    endpoint: str,
    interceptor_name: str = "default",
    api_key: str | None = None,
    api_key_env: str | None = None,
    on_notify: str = "ask",
    fail_open: bool = False,
    timeout_s: float = 10.0,
    phases: list[str] | None = None,
) -> PolicyCallable:
    """Factory: validate tool calls against an external MCP Interceptor PDP.

    Returns an async policy callable that forwards each in-scope event to the
    PDP's ``interceptor/invoke`` method and maps the ``ValidationResult`` back
    into an Omnigent :class:`PolicyResponse`.

    :param endpoint: PDP URL that accepts the JSON-RPC ``POST`` (e.g.
        ``"https://your-pdp.example.com/api/interceptor"``).
    :param interceptor_name: The interceptor to invoke on the PDP — sent as the
        spec ``name`` param. Defaults to ``"default"``; discover others via
        :func:`list_interceptors`.
    :param api_key: Literal bearer token for the ``Authorization`` header.
        Prefer ``api_key_env`` so the secret stays out of YAML/bundles.
    :param api_key_env: Name of an environment variable holding the bearer
        token. Takes precedence over ``api_key`` when set.
    :param on_notify: Verdict for an advisory result (spec severity ``warn`` or a
        non-error ``valid: false``) — one of ``"allow"``, ``"ask"`` (default),
        or ``"deny"``.
    :param fail_open: When the PDP cannot be reached or returns a malformed
        response, ``False`` (default) denies the call, ``True`` allows it.
    :param timeout_s: Per-request timeout in seconds. Defaults to ``10``.
    :param phases: Event types to forward — subset of
        ``["tool_call", "tool_result"]``. Defaults to ``["tool_call"]`` to match
        the spec's ``tools/call`` event.
    :returns: An async policy callable.
    :raises ValueError: If ``on_notify`` or ``phases`` contain invalid values.
    """
    on_notify_norm = on_notify.strip().lower()
    if on_notify_norm not in _ON_NOTIFY_TO_VERDICT:
        raise ValueError(
            f"on_notify must be one of {sorted(_ON_NOTIFY_TO_VERDICT)}, got {on_notify!r}"
        )
    notify_verdict = _ON_NOTIFY_TO_VERDICT[on_notify_norm]

    active_phases = frozenset(phases) if phases else frozenset({"tool_call"})
    unknown_phases = active_phases - _SUPPORTED_PHASES
    if unknown_phases:
        raise ValueError(
            f"phases must be a subset of {sorted(_SUPPORTED_PHASES)}, "
            f"got unsupported: {sorted(unknown_phases)}"
        )

    fail_verdict: PolicyResponse = (
        {"result": "ALLOW"}
        if fail_open
        else {
            "result": "DENY",
            "reason": "MCP Interceptor PDP unavailable; failing closed.",
        }
    )

    async def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Forward one event to the PDP and map the validation result back.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain on phases
            this policy does not handle.
        """
        if event.get("type") not in active_phases:
            return None

        tool_name = event.get("target") or (event.get("data") or {}).get("name")
        if not tool_name:
            # No tool to validate (shouldn't happen on tool phases) — abstain.
            return None

        # Open an ``execute_tool`` span around the decision so the governed tool
        # call (1) is visible as a trace — carrying a ``blocked`` disposition
        # when the PDP denies it; (2) gives the request an active OTel trace id
        # to send as ``context.traceId`` (see ``_current_trace_id``), so the
        # PDP's decision correlates with THIS trace; and (3) lands in the same
        # OTLP pipeline as the claude-sdk executor's own tool spans.
        model = (event.get("context") or {}).get("model")
        with _start_tool_span(tool_name, model) as _span:

            def _finish(resp: PolicyResponse) -> PolicyResponse:
                _record_tool_decision(
                    _span, isinstance(resp, dict) and resp.get("result") == "DENY"
                )
                return resp

            try:
                # ``phase``: ``request`` validates *before* the tool runs
                # (``tool_call``), ``response`` validates the output *after* it
                # runs (``tool_result``).
                interceptor_phase = "response" if event.get("type") == "tool_result" else "request"
                request_body = {
                    "jsonrpc": "2.0",
                    "id": uuid.uuid4().hex,
                    "method": "interceptor/invoke",
                    "params": {
                        "name": interceptor_name,
                        "event": "tools/call",
                        "phase": interceptor_phase,
                        "payload": _build_payload(event, tool_name),
                        "context": _build_context(event),
                    },
                }

                token = _resolve_api_key(api_key, api_key_env)
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
                        response = await client.post(endpoint, json=request_body, headers=headers)
                        response.raise_for_status()
                        payload = response.json()
                except (httpx.HTTPError, ValueError):
                    # Network error, non-2xx, or non-JSON body — honour fail policy.
                    return _finish(fail_verdict)

                # A JSON-RPC error object means the PDP could not produce a result.
                if isinstance(payload, dict) and payload.get("error"):
                    return _finish(fail_verdict)

                result = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(result, dict):
                    return _finish(fail_verdict)

                # Map the ValidationResult onto a verdict. Per the spec, "Only
                # validations with severity: error MUST block execution"; `valid`
                # reports the overall outcome and a `warn` is advisory.
                severity = str(result.get("severity") or "").strip().lower()
                valid = result.get("valid")
                if severity == "error":
                    verdict = "DENY"
                elif severity == "warn" or valid is False:
                    verdict = notify_verdict
                elif valid is True:
                    verdict = "ALLOW"
                else:
                    # No decidable outcome (no `valid`, no severity).
                    return _finish(fail_verdict)

                if verdict == "ALLOW":
                    return _finish(_ALLOW)
                return _finish({"result": verdict, "reason": _format_reason(result)})
            finally:
                _flush_tool_span()

    return evaluate


async def list_interceptors(
    endpoint: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Call the PDP's ``interceptors/list`` discovery method.

    A convenience helper (used by diagnostics and tests) so an operator can see
    which interceptors a PDP exposes before wiring up :func:`mcp_interceptor`.

    :param endpoint: PDP URL.
    :param api_key: Literal bearer token, or ``None``.
    :param api_key_env: Env var name holding the bearer token, or ``None``.
    :param timeout_s: Per-request timeout in seconds.
    :returns: The list of interceptor descriptors from the ``result``.
    :raises httpx.HTTPError: On network errors or non-2xx responses.
    """
    token = _resolve_api_key(api_key, api_key_env)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request_body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "interceptors/list"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        response = await client.post(endpoint, json=request_body, headers=headers)
        response.raise_for_status()
        payload = response.json()

    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        interceptors = result.get("interceptors")
        if isinstance(interceptors, list):
            return interceptors
    if isinstance(result, list):  # spec returns a bare array of descriptors
        return result
    return []


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.mcp_interceptor.mcp_interceptor",
        "kind": "factory",
        "name": "MCP Interceptor PDP",
        "description": (
            "Delegates tool-call verdicts to an external Policy Decision Point "
            "that implements the MCP Interceptor spec. Forwards each tools/call "
            "to the PDP's interceptor/invoke method and maps the ValidationResult "
            "(severity:error->DENY, warn->ASK, valid:true->ALLOW; configurable)."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "endpoint": {
                    "type": "string",
                    "description": "PDP URL accepting the JSON-RPC POST, e.g. "
                    "https://your-pdp.example.com/api/interceptor",
                },
                "interceptor_name": {
                    "type": "string",
                    "description": "Interceptor to invoke on the PDP (the spec `name`)",
                    "default": "default",
                },
                "api_key": {
                    "type": "string",
                    "description": "Literal bearer token (prefer api_key_env to keep "
                    "secrets out of YAML)",
                },
                "api_key_env": {
                    "type": "string",
                    "description": "Environment variable name holding the bearer token; "
                    "takes precedence over api_key",
                },
                "on_notify": {
                    "type": "string",
                    "description": "Verdict for an advisory result (severity 'warn'): "
                    "allow, ask, or deny",
                    "default": "ask",
                },
                "fail_open": {
                    "type": "boolean",
                    "description": "Allow (true) or deny (false) when the PDP is "
                    "unreachable or returns a bad response",
                    "default": False,
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Per-request timeout in seconds",
                    "default": 10,
                },
                "phases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Event types to forward: subset of "
                    "['tool_call', 'tool_result']. Defaults to ['tool_call'].",
                },
            },
            "required": ["endpoint"],
        },
    },
]
