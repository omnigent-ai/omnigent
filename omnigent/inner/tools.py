"""Tool type hierarchy for Omnigent."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, runtime_checkable

if TYPE_CHECKING:
    from .datamodel import AgentDef, ExecutorSpec, OSEnvSpec


# ---------------------------------------------------------------------------
# Type aliases for JSON-shaped boundaries
# ---------------------------------------------------------------------------

# JSON-Schema dicts sent to the LLM. The schemas themselves are arbitrary
# nested JSON objects defined by tool authors, so a concrete TypedDict would
# under-specify them; treat the shape as opaque JSON at this boundary.
ToolSchema: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Kwargs-shaped arguments provided by the LLM at tool-call time. These are
# heterogeneous JSON values keyed by parameter name; narrow inside the tool
# body after validation.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Results returned from a tool. Callers normalise scalar returns into
# ``{"result": ...}`` so the shape is always a JSON object, but values are
# heterogeneous JSON.
ToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# User-supplied Python callables bound via YAML (``callable: mod.func``) —
# the framework has no way to know their real signatures. The variadic callable
# boundary is intentionally unconstrained, so keep the explicit-any ignore at
# the alias site.
DynamicCallable: TypeAlias = Callable[..., object]  # type: ignore[explicit-any]


# ---------------------------------------------------------------------------
# Protocols for structural runner / run objects
# ---------------------------------------------------------------------------


@runtime_checkable
class CancellableRun(Protocol):
    """Handle returned by ``CancellableRunner.start`` that supports cancellation."""

    def cancel(self, reason: str) -> bool | Awaitable[bool]: ...


@runtime_checkable
class CancellableRunner(Protocol):
    """A runner object backing a :class:`CancellableFunctionTool`.

    ``start`` kicks off work and arranges for ``on_complete`` to be invoked
    with the final result. The returned handle optionally exposes a
    ``cancel(reason)`` method — callers must check at runtime because older
    runners may omit it.
    """

    def start(
        self,
        args: ToolArgs,
        on_complete: Callable[[ToolResult], None],
    ) -> CancellableRun | None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PY_TYPE_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _schema_from_callable(fn: DynamicCallable) -> ToolSchema:
    """Auto-generate a JSON-Schema ``parameters`` object from a function's signature.

    Uses type annotations when available; falls back to ``"string"`` for
    unannotated parameters.  Skips ``self`` and ``cls``.
    """
    sig = inspect.signature(fn)
    properties: ToolSchema = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            json_type = "string"
        else:
            json_type = _PY_TYPE_TO_JSON.get(annotation, "string")
        properties[pname] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    schema: ToolSchema = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """Base class for all tool specifications.

    :param name: Tool identifier, e.g. ``"web_search"``. ``None``
        on a freshly constructed placeholder; ``ToolRegistry.resolve_all``
        populates it from the registry key.
    :param description: Human-readable description shown to the LLM.
        ``None`` when the tool relies on its name alone.
    :param input_schema: JSON-Schema ``parameters`` object the LLM
        must satisfy when calling the tool. ``None`` when derived
        from a Python callable signature or a UC function metadata.
    :param output_schema: Optional JSON-Schema for the tool's return
        value. Advisory only — not enforced.
    :param scopes: Capability scopes required to invoke this tool,
        e.g. ``{"files:write"}``. ``None`` means no scope gating.
    :param cancellable: Whether the tool supports cancellation.
        Only :class:`CancellableFunctionTool` may set this to True.
    """

    name: str | None = None
    description: str | None = None
    input_schema: ToolSchema | None = None
    output_schema: ToolSchema | None = None
    scopes: set[str] | None = None
    cancellable: bool = False

    def tool_schema(self) -> ToolSchema:
        """Return the JSON-Schema-like description sent to the LLM."""
        # The LLM-facing tool schema is a JSON object with ``name`` and
        # ``description`` as plain strings. ``Tool.name`` /
        # ``Tool.description`` are ``str | None``; the ternaries substitute
        # ``""`` only at this outbound boundary.
        schema: ToolSchema = {
            "name": self.name if self.name is not None else "",
            "description": self.description if self.description is not None else "",
        }
        if self.input_schema:
            schema["parameters"] = self.input_schema
        elif isinstance(self, FunctionTool) and self.callable:
            schema["parameters"] = _schema_from_callable(self.callable)
        elif isinstance(self, AgentTool):
            if self.max_sessions is not None:
                limit_text = f"Max concurrent sessions: {self.max_sessions}"
                schema["description"] = (
                    f"{schema['description']}\n\n{limit_text}"
                    if schema["description"]
                    else limit_text
                )
            schema["parameters"] = {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "The input text or request to send to this agent.",
                    },
                },
                "required": ["input"],
            }
        return schema


# ---------------------------------------------------------------------------
# Concrete tool types
# ---------------------------------------------------------------------------


@dataclass
class FunctionTool(Tool):
    """A tool backed by a Python callable, UC function, or client SDK impl.

    :param callable: Resolved server-side callable. ``None`` for
        ``runtime: client``, UC functions, or pre-resolve.
    :param catalog_path: UC function reference. Mutually exclusive
        with ``callable`` and ``runtime: client``.
    :param runtime: ``"server"`` (default) or ``"client"``. With
        ``"client"``, ``callable`` must be ``None``; the spec
        validator enforces this.
    :param warehouse_id: Databricks SQL warehouse ID for UC
        function execution, e.g. ``"abc123def456"``. Required
        when ``catalog_path`` is set. ``None`` for non-UC tools.
    """

    callable: DynamicCallable | None = None
    catalog_path: str | None = None
    runtime: Literal["server", "client"] = "server"
    warehouse_id: str | None = None


@dataclass
class CancellableFunctionTool(Tool):
    """A tool backed by a runner object that supports cancellation.

    The runner exposes ``start(args, on_complete)`` (see ``CancellableRunner``).
    The field also accepts a bare ``DynamicCallable`` because YAML-loaded
    tools resolve ``runner:`` via a dotted path, and the loader can't know
    the target is a runner instance until resolve-time — the session
    runtime isinstance-narrows before invocation.
    """

    runner: CancellableRunner | DynamicCallable | None = None


@dataclass
class MCPTool(Tool):
    """A tool (or set of tools) exposed by an MCP server.

    Exactly one of ``url``, ``command``, or ``databricks_server``
    selects how the server is reached.

    :param url: HTTP(S) URL of an MCP server, e.g.
        ``"https://mcp.example.com/sse"``. ``None`` when connecting
        via stdio (``command``) or a named Databricks server.
    :param command: Local stdio command to spawn an MCP server,
        e.g. ``"npx"``. ``None`` when using ``url`` or
        ``databricks_server``.
    :param args: Arguments passed to ``command`` when spawning an
        stdio server.
    :param env: Environment variables injected into the stdio
        server process.
    :param tools: Allow-list of remote tool names to import.
        ``None`` means import every tool the server advertises.
    :param tool_name: When this instance represents a single
        remote tool, the remote tool's name. ``None`` when the
        instance fans out multiple tools; the runtime replaces
        the fan-out placeholder with per-tool :class:`MCPTool`
        entries whose ``tool_name`` is populated.
    :param profile: Databricks profile used to authenticate to
        the MCP server, e.g. ``"<your-profile>"``. ``None`` when the
        server doesn't require Databricks auth.
    :param databricks_server: Named Databricks-managed MCP server
        (e.g. ``"unity-catalog"``). ``None`` when ``url`` or
        ``command`` is used.
    :param headers: Extra HTTP headers for the ``url`` transport.
    """

    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    tools: list[str] | None = None  # specific tools to import
    tool_name: str | None = None
    profile: str | None = None
    databricks_server: str | None = None
    headers: dict[str, str] | None = None


@dataclass
class AgentTool(Tool):
    """A tool backed by a sub-agent.  Calling it starts a sub-session.

    :param prompt: System prompt for the spawned sub-agent. ``None``
        when the sub-agent should run with no explicit prompt.
    :param tools: Sub-agent's tool registry — may include
        :class:`InheritedTool` placeholders that resolve against the
        parent's tools at spawn time.
    :param executor: Override for the sub-agent's
        :class:`ExecutorSpec`. ``None`` means clone the parent's
        executor.
    :param os_env: OS-environment override for the sub-agent. The
        literal ``"inherit"`` copies the parent's concrete
        :class:`OSEnvSpec`; ``None`` means no OS env.
    :param pass_history: If True, snapshot the parent's ``"self"``
        history into the sub-session as ``"parent"``.
    :param pass_histories: Named histories from the parent to
        snapshot into the sub-session under the same name.
    :param max_sessions: Maximum concurrent named sub-sessions.
        ``None`` means unlimited.
    """

    prompt: str | None = None
    tools: dict[str, Tool] = field(default_factory=dict)
    executor: ExecutorSpec | None = None
    os_env: OSEnvSpec | Literal["inherit"] | None = None
    pass_history: bool = False
    pass_histories: list[str] | None = None
    max_sessions: int | None = None  # Maximum concurrent named sessions; None = unlimited


@dataclass
class SelfAgentTool(Tool):
    """A sub-agent whose spec is a clone of the parent's spec.

    Loaded from the ``tools.<name>: self`` string shorthand or the
    ``tools.<name>: {type: agent, spec: self}`` dict form. The
    translator (:mod:`omnigent.spec.omnigent`) materializes
    the sub-agent's :class:`AgentSpec` by deep-copying the parent's
    :class:`AgentDef` and re-running the translation — same model,
    same system prompt, same tool surface, same executor, same
    os_env. The LLM dispatches to it via
    ``sys_session_send(tool=<name>, session=..., args=...)``, the
    standard sub-agent invocation surface.

    No fields beyond :class:`Tool`'s ``name`` + ``description`` are
    needed — the entire sub-agent configuration comes from the
    parent. Authors who want partial overrides should use a regular
    :class:`AgentTool` declaration with explicit fields instead.
    """


@dataclass
class InheritedTool(Tool):
    """Placeholder: resolved from parent agent's tool with the same name."""


@dataclass
class SkillTool(Tool):
    """Loads knowledge / documentation into context on demand.

    :param path: Filesystem path to the skill content to load, e.g.
        ``"skills/sql_style.md"``. ``None`` on a placeholder skill
        tool that must be populated before use.
    """

    path: str | None = None


@dataclass
class HandoffTool(Tool):
    """Transfers the Connection to another agent's session.

    :param target_agent: Either a registered agent name (``str``),
        e.g. ``"billing"``, or an inline :class:`AgentDef` spec.
        ``None`` on a placeholder handoff tool that hasn't been
        populated. :class:`AgentDef` is imported lazily under
        ``TYPE_CHECKING`` to avoid a circular import at module
        load time.
    :param pass_history: Whether to copy the current history to
        the target session.
    :param history_filter: Optional callable applied to the history
        before handoff — signature is opaque to the framework.
    :param input_type: Expected Python type of the next user input
        the target agent will receive. Advisory only.
    :param on_handoff: Optional callback invoked at handoff time.
    :param bidirectional: If True, the target can hand control back
        to the caller; if False, the handoff is one-way.
    """

    target_agent: str | AgentDef | None = None
    pass_history: bool = True
    history_filter: DynamicCallable | None = None
    input_type: type | None = None
    on_handoff: DynamicCallable | None = None
    bidirectional: bool = True
