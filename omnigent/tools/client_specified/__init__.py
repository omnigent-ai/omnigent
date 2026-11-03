"""Client-specified tools — tools whose schemas are supplied by the API caller.

These tools are defined at request time rather than baked into the agent
image. The caller provides standard OpenAI-format function schemas; when
the LLM invokes one, the runtime persists the ``function_call`` output
items, streams them to the client, and completes the response. The client
handles execution externally and continues via ``previous_response_id``.

Public API:
- ``ClientSideTool``: A :class:`~omnigent.tools.base.Tool` that must
  never be executed server-side — its ``invoke()`` raises ``RuntimeError``.
- ``ClientSideToolSpec``: Configuration for one client-side tool (name
  and schema only — no callback URL or headers).
- ``parse_client_side_tool_spec``: Parse one raw OpenAI tool dict into a
  :class:`ClientSideToolSpec`.
- ``parse_client_side_tool_specs``: Parse a list of raw OpenAI tool dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omnigent.tools.base import Tool, ToolContext, is_valid_tool_name


@dataclass
class ClientSideToolSpec:
    """
    Configuration for one client-specified tool.

    Holds the information needed to present the tool to the LLM.
    Execution is handled entirely by the API caller — the runtime
    never invokes client-side tools server-side.

    :param name: Tool function name, e.g. ``"get_weather"``. Must
        match the ``function.name`` in the OpenAI schema.
    :param schema: Standard OpenAI-format function tool object, e.g.
        ``{"type": "function", "function": {"name": "get_weather",
        "description": "...", "parameters": {...}}}``. To allow
        per-call async dispatch (Phase 5), the tool's
        ``parameters.properties`` may include a ``synchronous``
        boolean — the LLM sets it ``false`` per call to receive
        a ``{task_id, kind: "client_tool"}`` handle and have the
        result auto-delivered later via the
        ``async_work_complete`` drain.
    """

    name: str
    schema: dict[str, Any]


class ClientSideTool(Tool):
    """
    A tool that is presented to the LLM but executed by the API caller.

    When the LLM invokes this tool, the runtime persists the
    ``function_call`` output items, streams them to the client, and
    completes the response. The client handles execution and continues
    via ``previous_response_id``.

    ``invoke()`` raises ``RuntimeError`` — client-side tools must never
    be dispatched through the tool execution path.

    :param spec: The :class:`ClientSideToolSpec` describing this tool.
    """

    def __init__(self, spec: ClientSideToolSpec) -> None:
        """
        :param spec: The :class:`ClientSideToolSpec` describing this tool.
        """
        self._spec = spec

    def name(self) -> str:  # type: ignore[override]
        """
        :returns: The tool function name, e.g. ``"get_weather"``.
        """
        return self._spec.name

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return "Client-side tool executed by the frontend."

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: The schema dict as supplied by the caller.
        """
        return self._spec.schema

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Raise ``RuntimeError`` — client-side tools must never be executed
        server-side.

        The workflow detects client-side tool calls via
        ``ToolManager.is_client_side_tool()`` before dispatching, so this
        method should never be reached in normal operation.

        :param arguments: JSON-encoded arguments string (unused).
        :param ctx: Server-side execution context (unused).
        :raises RuntimeError: Always — indicates a workflow bug.
        """
        raise RuntimeError(
            f"ClientSideTool {self._spec.name!r} must not be invoked server-side. "
            "The workflow must detect client-side tools via ToolManager.is_client_side_tool() "
            "and complete the response without executing them."
        )


def parse_client_side_tool_spec(raw: dict[str, Any]) -> ClientSideToolSpec:
    """
    Parse a raw OpenAI tool dict into a :class:`ClientSideToolSpec`.

    Validates that the dict is a well-formed OpenAI function tool
    schema with a ``function.name``. Per-call async dispatch is
    expressed by the tool's own schema declaring a ``synchronous``
    boolean inside ``parameters.properties`` — see
    :class:`ClientSideToolSpec` and the workflow's
    :func:`_handle_tool_calls` for routing.

    :param raw: A dict in standard OpenAI function tool format, e.g.:

        .. code-block:: json

            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "synchronous": {
                                "type": "boolean",
                                "description": "Set to false to dispatch as a background task..."
                            }
                        }
                    }
                }
            }

    :returns: A :class:`ClientSideToolSpec` with the name and
        schema.
    :raises ValueError: If ``type`` is not ``"function"`` or
        ``function.name`` is missing.
    """
    if raw.get("type") != "function":
        raise ValueError(
            f"client-specified tools must have type 'function', got {raw.get('type')!r}"
        )

    func = raw.get("function")
    if not isinstance(func, dict):
        raise ValueError("client-specified tool missing 'function' object")

    name = func.get("name")
    if not name:
        raise ValueError("client-specified tool missing function.name")

    if not is_valid_tool_name(name):
        raise ValueError(f"Invalid tool name {name!r}: must match [a-zA-Z0-9_-]{{1,256}}")

    return ClientSideToolSpec(name=name, schema=raw)


def parse_client_side_tool_specs(
    raw_tools: list[dict[str, Any]],
) -> list[ClientSideToolSpec]:
    """
    Parse a list of raw tool dicts into :class:`ClientSideToolSpec` objects.

    :param raw_tools: List of raw tool dicts from the API request, each
        in standard OpenAI function format.
    :returns: A list of :class:`ClientSideToolSpec` instances.
    :raises ValueError: If any tool in the list is malformed.
    """
    return [parse_client_side_tool_spec(raw) for raw in raw_tools]


__all__ = [
    "ClientSideTool",
    "ClientSideToolSpec",
    "parse_client_side_tool_spec",
    "parse_client_side_tool_specs",
]
