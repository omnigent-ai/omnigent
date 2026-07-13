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

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from omnigent.runner import pending_approvals
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.tool_dispatch import MCP_PROXY_CALL_TIMEOUT_S
from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)


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
        publish_event: Callable[[str, dict[str, Any]], None] | None = None,
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
        """
        self._session_id = session_id
        self._omnigent_client = ap_client
        self._publish_event = publish_event

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

        payload: dict[str, Any] = {
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
            data: dict[str, Any] = resp.json()
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
            err: dict[str, Any] = data["error"]
            msg = f"MCP proxy error {err.get('code')}: {err.get('message')}"
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

        tools_list: list[dict[str, Any]] = data.get("result", {}).get("tools", [])
        schemas: list[dict[str, Any]] = []
        tool_names: set[str] = set()
        for tool in tools_list:
            name = tool.get("name", "")
            if not name:
                continue
            # The Omnigent server returns ``inputSchema`` (JSON Schema from MCP).
            # Convert to the ``parameters`` key expected by LLM providers,
            # normalizing the same way RunnerMcpManager does via
            # _normalize_input_schema (ensure ``properties`` key is present).
            raw_schema: dict[str, Any] | None = tool.get("inputSchema")
            parameters: dict[str, Any]
            if raw_schema is None:
                parameters = {"type": "object", "properties": {}}
            elif raw_schema.get("type") == "object" and "properties" not in raw_schema:
                parameters = {**raw_schema, "properties": {}}
            else:
                parameters = raw_schema
            schema: dict[str, Any] = {
                "type": "function",
                "name": name,
                "description": tool.get("description", ""),
                "parameters": parameters,
            }
            schemas.append(schema)
            tool_names.add(name)

        return McpSchemasResult(schemas=schemas, tool_names=tool_names, failures={})

    async def call_tool(
        self,
        spec: AgentSpec | None,
        tool_name: str,
        # Values are Any because MCP tool arguments are JSON objects with
        # heterogeneous value types (str, int, bool, nested dicts, etc.).
        arguments: dict[str, Any],  # type: ignore[explicit-any]
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

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        # At most two iterations: initial call + one approval retry.
        for _attempt in range(2):
            try:
                resp = await self._omnigent_client.post(
                    self._mcp_url,
                    json=payload,
                    # Short connect timeout still fails fast on an unreachable
                    # server; the read timeout covers ordinary proxy request
                    # hangs. Sub-agent dispatch returns an async handle
                    # immediately and no longer holds this call for a child turn.
                    timeout=httpx.Timeout(
                        connect=10.0,
                        read=MCP_PROXY_CALL_TIMEOUT_S,
                        write=10.0,
                        pool=10.0,
                    ),
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            except Exception as exc:
                raise RuntimeError(
                    f"MCP proxy call failed for tool {tool_name!r} in session "
                    f"{self._session_id!r}: {exc}"
                ) from exc

            if "error" in data:
                err: dict[str, Any] = data["error"]
                code = err.get("code")
                msg = err.get("message", "")
                # -32000 is the MCP convention for server-defined errors (tool
                # denials, tool errors).  Return as a JSON error string so the
                # harness feeds the refusal back to the LLM rather than raising.
                if code == -32000:
                    return json.dumps({"error": msg})
                raise RuntimeError(
                    f"MCP proxy protocol error {code} for tool {tool_name!r}: {msg}"
                )

            result: dict[str, Any] = data.get("result", {})

            # ── ASK: Omnigent server returned InputRequiredResult ───────────────
            # Park for user approval and retry with inputResponses per the
            # MCP Multi Round-Trip Requests spec.
            if result.get("resultType") == "input_required":
                if _attempt >= 1:
                    # Guard against unexpected re-elicitation after one retry.
                    return json.dumps({"error": "Approval loop exceeded"})

                input_requests: dict[str, Any] = result.get("inputRequests") or {}
                request_state: str = result.get("requestState", "")
                # The Omnigent server uses the elicitation_id as the key in
                # inputRequests (MRTR spec: keys are server-assigned and the
                # client CAN read them — only requestState is opaque).
                elicitation_id = next(iter(input_requests), "")
                if not elicitation_id:
                    return json.dumps(
                        {"error": "Approval required but no elicitation in inputRequests"}
                    )

                _pub: Callable[[str, dict[str, Any]], None] = (
                    self._publish_event
                    if self._publish_event is not None
                    else (lambda _s, _e: None)
                )
                approved = await pending_approvals.wait_for_user_approval(
                    elicitation_id=elicitation_id,
                    conversation_id=self._session_id,
                    publish_event=_pub,
                )

                payload = {
                    "jsonrpc": "2.0",
                    "id": 2,  # MRTR spec: retry MUST use a different id
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                        "requestState": request_state,
                        "inputResponses": {
                            elicitation_id: {"action": "accept" if approved else "decline"}
                        },
                    },
                }
                continue

            # ── Normal result (ALLOW or post-approval execution) ──────────
            content = result.get("content", [])
            is_error = result.get("isError", False)
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(p for p in parts if p)
                if is_error:
                    return json.dumps({"error": text})
                return text if text else json.dumps(result)
            return json.dumps(result)

        return json.dumps({"error": "Approval retry loop exhausted"})

    async def prewarm(self, spec: AgentSpec) -> None:
        """No-op — the Omnigent server warms connections lazily via ServerMcpPool.

        :param spec: Ignored.
        """
        del spec

    async def shutdown(self) -> None:
        """No-op — the Omnigent server owns and manages MCP connections."""
