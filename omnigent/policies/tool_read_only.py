"""Tool read-only declarations on TOOL_CALL policy events.

A tool declares whether it has side effects; the TOOL_CALL event carries that
declaration as ``data.read_only`` (``True | False | None``) so approval
policies read it instead of guessing from tool names.

Sources, highest precedence first:

1. The agent spec, which is author-owned and immutable at runtime:
   ``read_only:`` on a ``type: function`` tool, or ``read_only_tools:`` on a
   ``type: mcp`` block.
2. The MCP server's ``Tool.annotations.readOnlyHint``. It counts only when
   the block sets ``trust_annotations: true``, because the MCP spec treats
   annotations as untrusted hints.
3. Otherwise ``None`` (undeclared). Policies must treat that as not
   read-only, i.e. fail closed.

Every TOOL_CALL :class:`EvaluationContext` is built through
:func:`tool_call_context`. The value is always resolved here and never
taken from the caller's payload, since a wire event from a sandboxed harness
could otherwise claim ``read_only: true``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import AgentSpec, Phase

# Claude-native exposes MCP servers as ``mcp__<server>__<tool>``.
_NATIVE_MCP_PREFIX = "mcp__"


def mcp_read_only_hint(tool_def: object) -> bool | None:
    """
    Read ``annotations.readOnlyHint`` from an MCP tool definition.

    :param tool_def: An ``mcp.types.Tool`` (or any object shaped like one).
    :returns: The hint when the server set a boolean, else ``None``.
    """
    hint = getattr(getattr(tool_def, "annotations", None), "readOnlyHint", None)
    return hint if isinstance(hint, bool) else None


def resolve_tool_read_only(
    spec: AgentSpec | None,
    tool_name: str,
    *,
    mcp_hints: Mapping[str, bool] | None = None,
) -> bool | None:
    """
    Resolve a tool's read-only declaration.

    :param spec: The session's agent spec. ``None`` resolves to undeclared.
    :param tool_name: Tool name as the harness calls it, e.g.
        ``"get_weather"``, ``"github__list_issues"`` or
        ``"mcp__github__list_issues"``.
    :param mcp_hints: ``readOnlyHint`` values keyed by
        ``"<server>__<tool>"``, for callers that hold discovered MCP tool
        definitions. Each hint is honoured only when its server sets
        ``trust_annotations``.
    :returns: ``True`` / ``False`` when declared, ``None`` when undeclared.
    """
    if spec is None:
        return None
    for tool in spec.local_tools:
        if tool.name == tool_name:
            return tool.read_only
    namespaced = tool_name.removeprefix(_NATIVE_MCP_PREFIX)
    for server in spec.mcp_servers:
        prefix = f"{server.name}__"
        if not namespaced.startswith(prefix):
            continue
        if namespaced[len(prefix) :] in (server.read_only_tools or ()):
            return True
        if server.trust_annotations and mcp_hints:
            return mcp_hints.get(namespaced)
        return None
    return None


def tool_call_context(
    spec: AgentSpec | None,
    tool_name: str,
    arguments: object,
    *,
    sub_agent_name: str | None = None,
    mcp_hints: Mapping[str, bool] | None = None,
    actor: dict[str, str] | None = None,
    model: str | None = None,
    harness: str | None = None,
) -> EvaluationContext:
    """
    Build the TOOL_CALL :class:`EvaluationContext`, the only place one is made.

    Content is ``{"name", "arguments", "read_only"}``. ``read_only`` is
    always resolved from *spec* and never copied from caller input.

    :param spec: The session's agent spec (``None`` if unavailable).
    :param tool_name: Tool about to be called.
    :param arguments: Decoded tool arguments (or the raw string when the
        caller could not decode them).
    :param sub_agent_name: The session's ``Conversation.sub_agent_name``.
        Server policy engines for a sub-agent session are built from the
        root spec, but the child's tools are declared on its own sub-agent
        spec, so declarations resolve against that one.
    :param mcp_hints: See :func:`resolve_tool_read_only`.
    :param actor: Authenticated principal, passed through.
    :param model: Hook-supplied model, passed through.
    :param harness: Hook-supplied harness, passed through.
    :returns: Ready-to-evaluate context.
    """
    declaring = spec
    if spec is not None and sub_agent_name:
        from omnigent.runtime.workflow import _find_spec_by_name

        declaring = _find_spec_by_name(spec, sub_agent_name) or spec
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={
            "name": tool_name,
            "arguments": arguments,
            "read_only": resolve_tool_read_only(declaring, tool_name, mcp_hints=mcp_hints),
        },
        tool_name=tool_name or None,
        actor=actor,
        model=model,
        harness=harness,
    )
