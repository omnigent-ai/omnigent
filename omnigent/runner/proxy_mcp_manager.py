"""Runner-side MCP proxy manager.

Routes all MCP calls through the Omnigent server's
``POST /v1/sessions/{session_id}/mcp`` endpoint (MCP Streamable HTTP,
JSON-RPC 2.0) instead of connecting to external MCP servers directly.

The Omnigent server holds the live connections in its
:class:`omnigent.server.mcp_pool.ServerMcpPool` and enforces
TOOL_CALL + TOOL_RESULT policies on every call before forwarding to the
real MCP server.  :class:`ProxyMcpManager` implements the same public
interface as :class:`omnigent.runner.mcp_manager.RunnerMcpManager` so
it can be substituted transparently at every dispatch site in
``runner/app.py``.

When to use each implementation:

- **AP mode** (out-of-process runner paired with Omnigent server):
  ``ProxyMcpManager(session_id, server_client)`` — all tool calls flow
  through the Omnigent server, policy enforced centrally.
- **No-AP / test mode**:
  ``RunnerMcpManager(stdio_cwd=...)`` — direct MCP connections, runner
  enforces policy locally via :class:`RunnerToolPolicyGate`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from time import monotonic
from typing import cast

import httpx

from omnigent.runner import pending_approvals
from omnigent.runner.mcp_execution_registry import (
    MCP_OPERATION_ID_PARAM,
    RUNNER_MCP_EXECUTION_DETACHED_CODE,
    McpExecutionRegistry,
)
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.tool_dispatch import MCP_PROXY_CALL_TIMEOUT_S
from omnigent.spec.types import AgentSpec
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

_EventPublisher = Callable[[str, _JsonObject], None]
_SERVER_RECONNECT_WAIT_S = 120.0
# Pause between re-sends of a retained operation whose request was lost between
# this runner and the server, so an unreachable server is not hammered.
_REATTACH_RETRY_DELAY_S = 2.0
# A re-send whose remaining read budget is below this cannot complete a round
# trip, so the phase ends rather than making a guaranteed-futile attempt.
_MIN_ATTEMPT_BUDGET_S = 1.0
# A request open at least this long was being executed when it was lost (an
# ingress cut a long-running tool, the tunnel dropped mid-execution), so it
# does not start the reconnect-wait streak that fast refusals do.
_LONG_REQUEST_MIN_S = 30.0
# Gateway-class statuses a proxy in front of the server returns when it gives
# up on a request; the server itself answers tool calls with JSON-RPC errors.
_GATEWAY_STATUS_CODES = frozenset({502, 503, 504})


def _is_unbound_runner_reply(code: object, message: object, session_id: str) -> bool:
    """Report a legacy server's unbound-runner reply to a retained re-send.

    Servers without the detached-while-unbound reply answer a retained re-send
    that reaches them before the tunnel rebinds with this exact message; match
    it in full so an unrelated -32000 cannot stall in the rebind wait, and
    remove the shim once such servers are retired.
    """
    return code == -32000 and message == f"No runner bound for session {session_id!r}"


def _json_object(value: object) -> _JsonObject | None:
    """Return a string-keyed object mapping when the value has that shape."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast("_JsonObject", value)


def _response_json_object(response: httpx.Response) -> _JsonObject:
    """Decode one JSON-RPC response object."""
    value: object = response.json()
    result = _json_object(value)
    if result is None:
        raise ValueError("MCP proxy response must be a JSON object")
    return result


def _input_response(
    verdict: pending_approvals.Verdict,
    input_request: _JsonObject | None,
) -> _JsonObject:
    """
    Build the MRTR ``inputResponses`` entry for one resolved elicitation.

    Carries the person's ``content`` when the prompt collected fields, so a
    server that asked "which environment?" is told which one rather than
    only that someone said yes. The content arrives from a browser, so it is
    validated against the request's ``requestedSchema`` first — an answer
    that does not fit (undeclared keys, wrong types, values outside an enum)
    is not forwarded across the trust boundary; the entry declines rather
    than smuggling unvalidated fields or substituting an answer nobody gave.

    :param verdict: The resolved verdict for this elicitation.
    :param input_request: The ``inputRequests`` entry this verdict answers,
        carrying ``params.requestedSchema``; ``None`` when unavailable.
    :returns: The wire object for this elicitation id.
    """
    from omnigent.tools._elicitation_schema import (
        build_accept_content_from_schema,
        schema_requires_fields,
        validate_content_against_schema,
    )

    if not verdict.approved:
        return {"action": "decline"}
    # The Omnigent server always populates ``params.requestedSchema`` on its
    # InputRequiredResult; a missing schema with supplied content fails closed.
    params = _json_object(input_request.get("params")) if input_request else None
    schema = _json_object(params.get("requestedSchema")) if params else None
    schema_dict = cast("dict[str, object]", schema) if schema is not None else None
    content = validate_content_against_schema(verdict.content, schema_dict)
    if content is None and verdict.content:
        # An answer WAS given but does not conform — fail closed instead of
        # forwarding it or letting the server act on a value nobody chose.
        _logger.warning(
            "MCP proxy elicitation answer does not conform to the "
            "requestedSchema — declining instead of forwarding it"
        )
        return {"action": "decline"}
    if content is None and schema_requires_fields(schema_dict):
        # Nobody chose, but the server requires fields — the surface
        # collected none (a bare approve card, or the REPL's y/n prompt).
        # Fall back to the schema auto-fill the inline path uses, so a
        # consent-shaped schema (e.g. the policy-ASK required boolean)
        # still accepts instead of inverting the person's answer.
        content = build_accept_content_from_schema(schema_dict) if schema_dict else None
        if content is None:
            # Nothing to fall back on either (e.g. a required free-form
            # field): an accept without the fields the server requires is
            # malformed — it rejects it and the MRTR retry loop spins — so
            # decline, matching the inline path's required-aware gate.
            _logger.info(
                "MCP proxy elicitation accepted with no content for a schema "
                "that requires fields — declining instead"
            )
            return {"action": "decline"}
    response: _JsonObject = {"action": "accept"}
    if content is not None:
        response["content"] = cast(object, content)
    return response


def _json_object_list(value: object) -> list[_JsonObject]:
    """Return only object entries from a JSON array."""
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := _json_object(raw)) is not None]


class ProxyMcpManager:
    """Routes runner-side MCP calls through the Omnigent server MCP proxy endpoint.

    Drop-in substitute for :class:`omnigent.runner.mcp_manager.RunnerMcpManager`
    used in Omnigent mode.  The runner creates one instance per session on first MCP use
    and passes it to :func:`omnigent.runner.tool_dispatch.execute_tool` in place
    of a direct manager.

    :param session_id: The AP-allocated session (conversation) id, e.g.
        ``"conv_abc123"``.  Used to build the proxy endpoint URL:
        ``/v1/sessions/{session_id}/mcp``.
    :param ap_client: An :class:`httpx.AsyncClient` pointed at the Omnigent server.
        Must already carry the runner's service auth (e.g. Databricks bearer
        token) so requests are accepted by the Omnigent server's auth middleware.
    """

    def __init__(
        self,
        session_id: str,
        ap_client: httpx.AsyncClient,
        publish_event: _EventPublisher | None = None,
        execution_registry: McpExecutionRegistry | None = None,
    ) -> None:
        """Create a proxy manager bound to one session.

        :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
        :param ap_client: HTTP client pointed at the Omnigent server.
        :param publish_event: Optional callback that puts an SSE event on
            the runner's per-session outbound queue.  Required for the
            approval flow so ``response.elicitation_resolved`` is emitted
            when the user decides (keeps the approval-badge counter in
            sync).  Pass ``None`` only in test contexts where the badge
            is irrelevant.
        :param execution_registry: Runner-owned MCP operations that a new
            server generation can reattach to after a tunnel replacement.
        """
        self._session_id = session_id
        self._omnigent_client = ap_client
        self._publish_event = publish_event
        self._execution_registry = execution_registry

    @property
    def _mcp_url(self) -> str:
        """MCP proxy endpoint URL for this session.

        :returns: Path string, e.g. ``"/v1/sessions/conv_abc123/mcp"``.
        """
        return f"/v1/sessions/{self._session_id}/mcp"

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        """Fetch tool schemas from the Omnigent server MCP proxy (``tools/list``).

        Sends a ``tools/list`` JSON-RPC 2.0 request to the Omnigent server's MCP
        proxy endpoint.  Returns tool schemas in the same flat OpenAI
        function-tool format as
        :meth:`omnigent.runner.mcp_manager.RunnerMcpManager.schemas_for`.

        Tool names are returned with the server namespace prefix applied by
        the Omnigent server (e.g. ``github__search``).  This ensures the harness
        sees collision-safe names even when multiple MCP servers define tools
        with the same bare name.

        :param spec: The agent spec.  When ``spec.mcp_servers`` is empty,
            returns an empty result immediately without hitting the network.
        :returns: :class:`McpSchemasResult` containing schemas, tool name
            set, and per-server failure messages.
        """
        if not spec.mcp_servers:
            return McpSchemasResult(schemas=[], tool_names=set(), failures={})

        payload: _JsonObject = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        try:
            resp = await self._omnigent_client.post(
                self._mcp_url,
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = _response_json_object(resp)
        except Exception as exc:  # noqa: BLE001 — network + HTTP errors all surface as failures
            _logger.warning(
                "ProxyMcpManager tools/list failed for session %r: %s",
                self._session_id,
                exc,
            )
            return McpSchemasResult(
                schemas=[],
                tool_names=set(),
                failures={"proxy": f"{type(exc).__name__}: {exc}"},
            )

        if "error" in data:
            err = _json_object(data.get("error"))
            msg = (
                f"MCP proxy error {err.get('code')}: {err.get('message')}"
                if err is not None
                else "MCP proxy returned a non-object RPC error"
            )
            _logger.warning(
                "ProxyMcpManager tools/list returned RPC error for session %r: %s",
                self._session_id,
                msg,
            )
            return McpSchemasResult(
                schemas=[],
                tool_names=set(),
                failures={"proxy": msg},
            )

        result = _json_object(data.get("result"))
        if result is None:
            msg = "MCP proxy returned a non-object tools/list result"
            _logger.warning(
                "ProxyMcpManager tools/list returned malformed result for session %r",
                self._session_id,
            )
            return McpSchemasResult(
                schemas=[],
                tool_names=set(),
                failures={"proxy": msg},
            )
        tools_list = _json_object_list(result.get("tools"))
        schemas: list[_JsonObject] = []
        tool_names: set[str] = set()
        for tool in tools_list:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            # The Omnigent server returns ``inputSchema`` (JSON Schema from MCP).
            # Convert to the ``parameters`` key expected by LLM providers,
            # normalizing the same way RunnerMcpManager does via
            # _normalize_input_schema (ensure ``properties`` key is present).
            raw_schema = _json_object(tool.get("inputSchema"))
            parameters: _JsonObject
            if raw_schema is None:
                parameters = {"type": "object", "properties": {}}
            elif raw_schema.get("type") == "object" and "properties" not in raw_schema:
                parameters = {**raw_schema, "properties": {}}
            else:
                parameters = raw_schema
            description = tool.get("description")
            schema: _JsonObject = {
                "type": "function",
                "name": name,
                "description": description if isinstance(description, str) else "",
                "parameters": parameters,
            }
            schemas.append(schema)
            tool_names.add(name)

        return McpSchemasResult(schemas=schemas, tool_names=tool_names, failures={})

    async def call_tool(
        self,
        spec: AgentSpec | None,
        tool_name: str,
        arguments: _JsonObject,
    ) -> str:
        """Dispatch a tool call via the Omnigent server MCP proxy (``tools/call``).

        Sends a ``tools/call`` JSON-RPC 2.0 request.  The Omnigent server enforces
        TOOL_CALL and TOOL_RESULT policies before forwarding to the real MCP
        server.

        **ASK policy / approval flow**: when the Omnigent server returns an
        ``InputRequiredResult`` (MCP MRTR spec), this method parks on the
        runner-side approval Future until the user accepts or declines, then
        retries once with the user's decision in ``inputResponses``.

        :param spec: Ignored — accepted for interface parity with
            :class:`RunnerMcpManager`.  ``None`` is acceptable for callers
            that have no spec context (e.g. the claude-native relay executor).
        :param tool_name: Tool name as seen by the LLM, e.g.
            ``"github__search"`` or ``"sys_os_read"``.
        :param arguments: Decoded tool arguments dict.
        :returns: Tool output string.  On denial the Omnigent server returns an
            MCP error response which is converted here to a JSON error string
            so the harness can feed it to the LLM as a tool result.
        :raises RuntimeError: On network failure or unexpected protocol errors.
        """
        del spec  # Omnigent server resolves spec from session context

        operation_id = f"mcpop_{uuid.uuid4().hex}"
        registry = self._execution_registry
        if registry is not None:
            registry.retain_operation(self._session_id, operation_id)
        try:
            return await self._call_tool_with_operation(tool_name, arguments, operation_id)
        finally:
            if registry is not None:
                registry.release_operation(self._session_id, operation_id)

    async def _call_tool_with_operation(
        self,
        tool_name: str,
        arguments: _JsonObject,
        operation_id: str,
    ) -> str:
        """Run one proxy call under an already-retained operation id."""
        request_id = 1

        def _initial_payload() -> _JsonObject:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                    MCP_OPERATION_ID_PARAM: operation_id,
                },
            }

        def _call_failed(cause: BaseException) -> RuntimeError:
            return RuntimeError(
                f"MCP proxy call failed for tool {tool_name!r} in session "
                f"{self._session_id!r}: {cause}"
            )

        def _failure_detail(cause: BaseException | None) -> str:
            return f" (last failure: {cause})" if cause is not None else ""

        def _no_replacement_server(cause: BaseException | None) -> RuntimeError:
            return RuntimeError(
                f"MCP proxy call for tool {tool_name!r} in session "
                f"{self._session_id!r} lost its server and no replacement "
                f"connected within {_SERVER_RECONNECT_WAIT_S:.0f}s{_failure_detail(cause)}"
            )

        def _budget_exhausted(cause: BaseException | None) -> RuntimeError:
            return RuntimeError(
                f"MCP proxy call for tool {tool_name!r} in session "
                f"{self._session_id!r} did not complete within "
                f"{MCP_PROXY_CALL_TIMEOUT_S:.0f}s{_failure_detail(cause)}"
            )

        async def _wait_before_reattach(
            request_generation: int,
            cause: BaseException | None,
            *,
            sent_at: float,
        ) -> None:
            """Wait, then let the loop re-send the retained operation.

            The execution keeps running on this runner and its registry attaches
            the re-sent operation to it instead of running the tool again. A
            request lost in transit (``cause`` set) may have met the same server
            generation, so the loop re-sends after a short pause. A server that
            reported the execution detached cannot reach this runner, so the loop
            waits for the tunnel to rebind, which advances the server generation.
            """
            nonlocal detached_since
            registry = self._execution_registry
            if registry is None or not registry.has_operation(self._session_id, operation_id):
                if cause is not None:
                    raise _call_failed(cause) from cause
                raise RuntimeError(
                    f"MCP proxy call for tool {tool_name!r} in session "
                    f"{self._session_id!r} lost its server without a reserved "
                    "runner operation"
                )
            now = monotonic()
            phase_deadline = phase_started + MCP_PROXY_CALL_TIMEOUT_S
            if now >= phase_deadline:
                raise _budget_exhausted(cause) from cause
            if cause is None:
                # A detached reply arrives after the tool has run; a long
                # execution must not count against the rebind window, exactly as
                # a transport loss resets the streak below.
                if now - sent_at >= _LONG_REQUEST_MIN_S:
                    detached_since = None
                if detached_since is None:
                    detached_since = now
                rebind_deadline = detached_since + _SERVER_RECONNECT_WAIT_S
                if now >= rebind_deadline:
                    raise _no_replacement_server(None)
                try:
                    await pending_approvals.wait_for_server_reconnect(
                        request_generation,
                        timeout_seconds=min(rebind_deadline, phase_deadline) - now,
                    )
                except asyncio.TimeoutError as exc:
                    if monotonic() >= rebind_deadline:
                        raise _no_replacement_server(None) from exc
                    # The rebind window still has room, so the call budget, not a
                    # missing server, ended the wait; say so instead of a bare
                    # "did not complete" at the loop top.
                    raise _budget_exhausted(
                        RuntimeError("call budget spent waiting for the server tunnel to rebind")
                    ) from exc
                else:
                    # The rebind completed, so the runner is reachable again; a
                    # later detach starts a fresh window instead of this streak.
                    detached_since = None
                return
            if now - sent_at >= _LONG_REQUEST_MIN_S:
                detached_since = None
            elif detached_since is None:
                detached_since = sent_at
            wait_s = _REATTACH_RETRY_DELAY_S
            if detached_since is not None:
                remaining = detached_since + _SERVER_RECONNECT_WAIT_S - now
                if remaining <= 0:
                    raise _no_replacement_server(cause) from cause
                wait_s = min(wait_s, remaining)
            try:
                await pending_approvals.wait_for_server_reconnect(
                    request_generation,
                    timeout_seconds=min(wait_s, phase_deadline - now),
                )
            except asyncio.TimeoutError:
                pass
            else:
                # The rebind completed, so the runner is reachable again; a
                # later drop starts a fresh window instead of this streak.
                detached_since = None

        payload = _initial_payload()
        approval_retries = 0
        # Start of the current request phase. The call budget bounds sending one
        # request and re-sending it after drops; waiting for a user's verdict is
        # bounded by the ask gate instead and starts a new phase.
        phase_started = monotonic()
        last_failure: BaseException | None = None
        # Start of the current streak of attempts the server could not route to
        # this runner; ``None`` once an attempt shows the runner was reachable.
        detached_since: float | None = None

        while True:
            budget_left = MCP_PROXY_CALL_TIMEOUT_S - (monotonic() - phase_started)
            if budget_left <= _MIN_ATTEMPT_BUDGET_S:
                raise _budget_exhausted(last_failure) from last_failure
            request_generation = pending_approvals.current_server_generation()
            sent_at = monotonic()
            try:
                # The read timeout resets on every byte, so a slow-drip response
                # could outlive the budget; the overall deadline bounds the whole
                # attempt. Connect fails fast; read shrinks on re-sends.
                async with asyncio.timeout(budget_left):
                    resp = await self._omnigent_client.post(
                        self._mcp_url,
                        json=payload,
                        timeout=httpx.Timeout(
                            connect=10.0,
                            read=budget_left,
                            write=10.0,
                            pool=10.0,
                        ),
                    )
                    resp.raise_for_status()
                    data = _response_json_object(resp)
            except TimeoutError as exc:
                raise _budget_exhausted(last_failure) from exc
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in _GATEWAY_STATUS_CODES
                ):
                    raise _call_failed(exc) from exc
                # The request was lost between this runner and the server while
                # the retained execution continues. A fresh JSON-RPC id marks the
                # new transport attempt; the operation id prevents a replay.
                last_failure = exc
                _logger.warning(
                    "MCP proxy call for tool %r in session %r dropped after %.0fs (%s); "
                    "re-sending to reattach to the retained runner operation",
                    tool_name,
                    self._session_id,
                    monotonic() - sent_at,
                    exc,
                )
                await _wait_before_reattach(request_generation, exc, sent_at=sent_at)
                request_id += 1
                payload = cast("_JsonObject", {**payload, "id": request_id})
                continue
            except Exception as exc:
                raise _call_failed(exc) from exc

            if "error" in data:
                err = _json_object(data.get("error"))
                if err is None:
                    raise RuntimeError(
                        f"MCP proxy returned a non-object RPC error for tool {tool_name!r}"
                    )
                code = err.get("code")
                msg = err.get("message", "")
                registry = self._execution_registry
                owns_operation = registry is not None and registry.has_operation(
                    self._session_id, operation_id
                )
                if code == RUNNER_MCP_EXECUTION_DETACHED_CODE or (
                    owns_operation and _is_unbound_runner_reply(code, msg, self._session_id)
                ):
                    # The detach code and a legacy server's unbound-runner reply
                    # both mean the tunnel is unbound while this runner holds the
                    # retained operation; wait for the rebind, then re-send.
                    last_failure = None
                    await _wait_before_reattach(request_generation, None, sent_at=sent_at)
                    request_id += 1
                    payload = cast("_JsonObject", {**payload, "id": request_id})
                    continue
                # -32000 is the MCP convention for server-defined errors (tool
                # denials, tool errors). Return as a JSON error string so the
                # harness feeds the refusal back to the LLM rather than raising.
                if code == -32000:
                    return json.dumps({"error": msg})
                raise RuntimeError(
                    f"MCP proxy protocol error {code} for tool {tool_name!r}: {msg}"
                )

            detached_since = None
            last_failure = None
            result = _json_object(data.get("result"))
            if result is None:
                raise RuntimeError(
                    f"MCP proxy returned a non-object result for tool {tool_name!r}"
                )

            # ── ASK: Omnigent server returned InputRequiredResult ───────────────
            # Park for user approval and retry with inputResponses per the
            # MCP Multi Round-Trip Requests spec.
            if result.get("resultType") == "input_required":
                if approval_retries >= 1:
                    # Guard against unexpected re-elicitation after one retry.
                    return json.dumps({"error": "Approval loop exceeded"})

                input_requests = _json_object(result.get("inputRequests"))
                if input_requests is None:
                    input_requests = {}
                request_state_value = result.get("requestState")
                request_state = request_state_value if isinstance(request_state_value, str) else ""
                # The Omnigent server uses the elicitation_id as the key in
                # inputRequests (MRTR spec: keys are server-assigned and the
                # client CAN read them — only requestState is opaque).
                elicitation_id = next(iter(input_requests), "")
                if not elicitation_id:
                    return json.dumps(
                        {"error": "Approval required but no elicitation in inputRequests"}
                    )

                publisher: _EventPublisher = (
                    self._publish_event
                    if self._publish_event is not None
                    else (lambda _s, _e: None)
                )
                try:
                    verdict = await pending_approvals.wait_for_user_verdict(
                        elicitation_id=elicitation_id,
                        conversation_id=self._session_id,
                        publish_event=publisher,
                        retry_on_server_reconnect=True,
                    )
                except pending_approvals.ServerReconnected:
                    # requestState and its elicitation id belong to the server
                    # generation that issued them. Re-run the original call so
                    # the connected generation can create an answerable gate.
                    phase_started = monotonic()
                    request_id += 1
                    payload = _initial_payload()
                    continue

                phase_started = monotonic()
                request_id += 1
                # The server consumes the approval when this retry first arrives,
                # so a re-send after a lost response is not replay-safe yet.
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,  # MRTR retry MUST use a different id
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                        MCP_OPERATION_ID_PARAM: operation_id,
                        "requestState": request_state,
                        "inputResponses": {
                            elicitation_id: _input_response(
                                verdict, _json_object(input_requests.get(elicitation_id))
                            ),
                        },
                    },
                }
                approval_retries += 1
                continue

            # ── Normal result (ALLOW or post-approval execution) ──────────
            content = result.get("content", [])
            is_error = result.get("isError", False)
            if isinstance(content, list):
                parts: list[str] = []
                for raw_block in content:
                    block = _json_object(raw_block)
                    if block is None or block.get("type") != "text":
                        continue
                    block_text = block.get("text")
                    if isinstance(block_text, str):
                        parts.append(block_text)
                text = "\n".join(p for p in parts if p)
                if is_error:
                    return json.dumps({"error": text})
                return text if text else json.dumps(result)
            return json.dumps(result)

    async def prewarm(self, spec: AgentSpec) -> None:
        """No-op — the Omnigent server warms connections lazily via ServerMcpPool.

        :param spec: Ignored.
        """
        del spec

    async def shutdown(self) -> None:
        """No-op — the Omnigent server owns and manages MCP connections."""
