"""Bidirectional translator between omnigent ``AgentSpec`` and omnigent ``AgentDef``.

Two functions live here:

- :func:`agent_spec_to_agent_def` — forward direction, consumed by
  ``OmnigentExecutor`` at executor-construction time.
- :func:`agent_def_to_agent_spec` — reverse direction, consumed by
  :func:`omnigent.spec.load` when handed an omnigent YAML so
  ``omnigent chat foo.yaml`` works transparently.

The bidirectional invariant
``agent_spec_to_agent_def(agent_def_to_agent_spec(d)) == d`` must
hold for every representative omnigent YAML; the round-trip test
under ``tests/spec/test_omnigent_roundtrip.py`` enforces it.

Design invariants (see designs/OMNIGENT_INTEGRATION.md §1 and §2):

- Tool callables are encoded as dotted import paths in ``AgentSpec``
  (e.g. ``"examples._shared.tool_functions.get_current_time"``). The forward
  direction resolves them via :func:`importlib.import_module`; the
  reverse direction recovers them from
  ``__module__`` + ``__qualname__``. ``AgentSpec`` itself stays a
  clean serializable native type — no opaque payloads.
- Unsupported concepts **fail loud** with an :class:`OmnigentError`
  naming the specific field. Currently unsupported (each is an
  omnigent spec gap to fix, not a translation to paper over):
  policies, OSEnv sandbox, MCP-type tools,
  cancellable_function-type tools.
"""

from __future__ import annotations

import copy
import importlib
from collections.abc import Callable
from typing import Any, TypeAlias, cast

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inner.datamodel import AgentDef, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
from omnigent.inner.tools import (
    AgentTool,
    CancellableFunctionTool,
    FunctionTool,
    MCPTool,
    SelfAgentTool,
    Tool,
    _schema_from_callable,
)
from omnigent.llms.routing import infer_harness_from_model as _infer_harness_from_model
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    GuardrailsSpec,
    LLMConfig,
    LocalToolInfo,
    MCPServerConfig,
    ToolRuntime,
    ToolsConfig,
)

# Dynamically-loaded Python callable resolved from a YAML ``callable:
# mod.func`` dotted path. Signatures vary; both callers and this module
# treat the return value opaquely.
DynamicCallable: TypeAlias = Callable[..., object]

# Value placed in :attr:`AgentSpec.executor.type` so the runtime
# selects ``OmnigentExecutor``. Both directions of the translator
# treat this as the discriminator between native omnigent specs
# and omnigent-sourced ones.
OMNIGENT_EXECUTOR_TYPE = "omnigent"

# Value placed in :attr:`LocalToolInfo.language` for tools that were
# sourced from an omnigent YAML. Distinguishes them from native
# omnigent tools (``"python"`` / ``"typescript"``) which live as
# files on disk under ``tools/python/``.
OMNIGENT_TOOL_LANGUAGE = "omnigent-python-callable"

# Version stamped on AgentSpecs synthesized from omnigent YAMLs.
# Omnigent YAMLs do not declare ``spec_version`` (it's an
# omnigent concept), so the adapter writes ``1`` — the currently
# valid omnigent schema version (see omnigent/spec/validator.py).
_SYNTHETIC_SPEC_VERSION = 1

# Sentinel value used when an inline ``AgentTool`` declares
# ``os_env: inherit`` in its YAML. Resolved against the parent's
# concrete :class:`OSEnvSpec` at translation time inside
# :func:`_resolve_inline_agent_tool_os_env`; the sub-spec stores
# the resolved dataclass on its top-level ``AgentSpec.os_env``
# field and the literal string is never persisted past
# translation. We resolve eagerly because omnigent spawns
# each child as an independent task — no live parent session
# exists at runtime to walk.
_OS_ENV_INHERIT_SENTINEL = "inherit"

# Omnigent → omnigent mapping for the ``monotonic`` label
# schema field. Omnigent uses ``max`` / ``min`` / ``none``
# (datamodel.LabelSchemaRule.monotonic); omnigent uses
# ``increasing`` / ``decreasing`` / absent (types.LabelDef.monotonic).
# ``max`` is monotonically increasing (each write must be ≥ current);
# ``min`` is monotonically decreasing (each write must be ≤ current).
_OMNI_TO_AP_MONOTONIC: dict[str, str] = {
    "max": "increasing",
    "min": "decreasing",
}

# Omnigent loader policy-type discriminators. Used to dispatch
# per-policy-type translation from the raw YAML dict.
_POLICY_TYPE_FUNCTION = "function"
_POLICY_TYPE_PROMPT = "prompt"
_KNOWN_POLICY_TYPES: frozenset[str] = frozenset(
    {_POLICY_TYPE_FUNCTION, _POLICY_TYPE_PROMPT},
)


# Harnesses that route through locally installed CLIs backed by the user's
# subscription plan (Claude Code, Codex). These must NOT inherit a parent
# agent's Databricks profile; doing so would trigger Databricks routing
# and bypass subscription auth.
_SUBSCRIPTION_AUTH_HARNESSES: frozenset[str] = frozenset({"claude-native", "claude-sdk", "codex"})

# ── Forward direction: AgentSpec → AgentDef ────────────────────


def agent_spec_to_agent_def(spec: AgentSpec) -> AgentDef:
    """
    Translate an omnigent ``AgentSpec`` into an omnigent
    ``AgentDef`` suitable for
    :func:`omnigent.executor_factory.create_executor`.

    Handles: name, instructions / prompt, ``llm.model``,
    ``executor.config`` (``harness``, ``profile``), and
    function-type local tools encoded as dotted module paths.

    :param spec: The omnigent spec to translate. ``spec.llm``
        must be set (the omnigent harness needs a model name);
        ``spec.executor.type`` must be ``"omnigent"`` with
        ``executor.config.harness`` populated.
    :returns: A populated :class:`omnigent.datamodel.AgentDef`
        with ``name``, ``prompt``, ``executor``, and ``tools``
        filled in. ``policies`` and ``os_env`` are left at their
        dataclass defaults — those concepts are unsupported and
        rejected upstream.
    :raises OmnigentError: If the spec uses an unsupported
        concept (``guardrails.policies``, sandbox, MCP server, or
        ``cancellable_function`` tool). The error message names
        the specific field.
    """
    _reject_unsupported_concepts(spec)

    if spec.executor.model is None:
        raise OmnigentError(
            "executor.type='omnigent' requires a model (set executor.model)",
            code=ErrorCode.INVALID_INPUT,
        )

    # For native Omnigent v1 specs ``executor.config`` is empty; infer the
    # harness from the model prefix so Claude/GPT models get the right
    # executor instead of falling back to DatabricksExecutor.
    _raw_harness: str | None = spec.executor.config.get("harness")
    if not _raw_harness:
        _raw_harness = _infer_harness_from_model(spec.executor.model) or None
    _raw_harness = canonicalize_harness(_raw_harness)
    executor_spec = OmniExecutorSpec(
        model=spec.executor.model,
        harness=_raw_harness,
        profile=spec.executor.config.get("profile"),
    )

    # ``AgentDef.name`` / ``AgentDef.prompt`` are ``str | None`` — pass
    # the spec's values (also ``str | None``) through unchanged.
    # ``spec.os_env`` is the parsed :class:`OSEnvSpec` dataclass
    # (or ``None``); :func:`_agent_tool_to_sub_spec` resolves the
    # ``"inherit"`` sentinel at translation time so it never
    # reaches the forward path as a string.
    # Bundle root: derived from any bundled skill's ``skill_dir``
    # (each lives at ``<bundle>/skills/<name>/`` per AGENTSPEC.md).
    # Without it the Claude SDK harness can't expose bundled skills
    # via ``--plugin-dir``. ``None`` when the spec has no skills —
    # nothing to expose, nothing to set.
    bundle_dir = (
        spec.skills[0].skill_dir.parents[1]
        if spec.skills and spec.skills[0].skill_dir is not None
        else None
    )
    return AgentDef(
        name=spec.name,
        prompt=spec.instructions,
        tools=_translate_tools_to_omnigent(spec),
        executor=executor_spec,
        os_env=spec.os_env,
        bundle_dir=bundle_dir,
        skills_filter=spec.skills_filter,
    )


def _reject_unsupported_concepts(spec: AgentSpec) -> None:
    """
    Fail loud when the spec uses an unsupported concept.

    Each branch names the specific field so the caller knows what
    to remove from the spec (or what omnigent bug to file to
    close the gap).

    :param spec: The omnigent spec to check.
    :raises OmnigentError: On any of:
        ``executor.config.sandbox`` set, an MCP server declared,
        or a ``cancellable_function`` in ``local_tools``.
    """
    # ``guardrails.policies`` is intentionally NOT rejected here.
    # Policies are enforced by the omnigent workflow layer
    # (see ``omnigent.runtime.policies.build_policy_engine``)
    # BEFORE the harness receives a turn — the
    # :class:`OmnigentExecutor` is invoked with tool calls
    # already gated through INPUT/TOOL_CALL, so the harness-side
    # AgentDef doesn't need to carry the policy metadata. This
    # pass-through (drop guardrails from the forward
    # translation) is the one place we accept "silently dropping
    # a spec field" because the field is consumed upstream and
    # has no meaning to the harness.

    # Sandbox declarations (omnigent ``tools.sandbox.container_image``
    # and the omnigent OSEnvSandboxSpec) are unsupported. Fail loud
    # if either is populated.
    if spec.tools.sandbox.container_image is not None:
        raise OmnigentError(
            "tools.sandbox translation to omnigent OSEnvSpec is unsupported; "
            "the adapter rejects specs with sandbox rather than silently dropping it",
            code=ErrorCode.INVALID_INPUT,
        )

    # ``cancellable_function`` is an omnigent concept — Omnigent'
    # LocalToolInfo has no cancellation flag, so this branch currently
    # only triggers when a caller constructs an AgentSpec with a tool
    # path that we later discover is a cancellable-function runner.
    for tool in spec.local_tools:
        # Client-runtime tools carry no path — skip the
        # cancellable-function detection entirely. ``path is None``
        # is only legal for ``runtime: client`` (the validator
        # enforces this on every other code path).
        if tool.path is None:
            continue
        if _is_cancellable_function_path(tool.path):
            raise OmnigentError(
                f"cancellable_function tool {tool.name!r} cannot be translated to "
                "omnigent; cancellation support is an omnigent spec extension",
                code=ErrorCode.INVALID_INPUT,
            )


def _is_cancellable_function_path(path: str) -> bool:
    """
    Detect whether a tool dotted path names a cancellable-function
    runner.

    Agent-plane's ``LocalToolInfo.path`` normally points at a plain
    Python function file. Cancellable-function tools are flagged
    explicitly in the omnigent spec; omnigent has no spec
    surface for that flag yet, so this helper returns ``False``
    today and exists as the hook where future work will wire the
    detection once ``LocalToolInfo`` grows a ``cancellable``
    attribute.

    :param path: The tool's dotted / filesystem path, e.g.
        ``"tools/python/arxiv_search.py"``.
    :returns: ``True`` if the tool is known to be a
        cancellable-function runner. Always ``False`` today.
    """
    del path
    return False


def _translate_tools_to_omnigent(spec: AgentSpec) -> dict[str, Tool]:
    """
    Build the ``AgentDef.tools`` dict from Omnigent' tool model.

    Function-type local tools only: each :class:`LocalToolInfo`
    whose ``path`` is a dotted Python module path (e.g.
    ``"examples._shared.tool_functions.get_current_time"``) is resolved via
    :func:`importlib.import_module` and wrapped in a
    :class:`omnigent.tools.FunctionTool`. Filesystem paths
    (``"tools/python/foo.py"``) are not supported — the YAML adapter
    that produces them must encode dotted paths.

    :param spec: The omnigent spec. ``spec.local_tools`` lists
        the tools to translate.
    :returns: A ``dict[str, Tool]`` mapping tool name to
        :class:`FunctionTool`, ready for assignment to
        ``AgentDef.tools``. Empty dict if the spec declares no
        local tools.
    :raises OmnigentError: If a tool path is a filesystem path
        (not importable via :func:`importlib.import_module`) or if
        the resolved attribute is not callable.
    """
    tools: dict[str, Tool] = {}
    for tool_info in spec.local_tools:
        # Client-runtime tools have no server-side callable to
        # resolve; the SDK consumer implements them at stream-start
        # time. Round-trip them as a ``FunctionTool`` with no
        # callable and ``runtime="client"`` so the discriminator
        # survives a forward→reverse pass through the spec layer.
        if tool_info.runtime == ToolRuntime.CLIENT:
            tools[tool_info.name] = FunctionTool(
                name=tool_info.name,
                input_schema=tool_info.parameters,
                runtime="client",
            )
            continue
        # UC function tools have a catalog_path and no server-side
        # callable. Round-trip them as a ``FunctionTool`` with the
        # catalog_path preserved so the inner stack can delegate to
        # the runner's UC executor at call time.
        if tool_info.runtime == ToolRuntime.UC_FUNCTION:
            tools[tool_info.name] = FunctionTool(
                name=tool_info.name,
                catalog_path=tool_info.catalog_path,
                input_schema=tool_info.parameters,
                warehouse_id=tool_info.warehouse_id,
            )
            continue
        # Step (c): every ``LocalToolInfo`` resolves to a plain
        # callable. The runner-protocol fallback (instances with
        # ``.start(args, on_complete)``) was removed once
        # ``sys_call_async`` superseded ``CancellableFunctionTool``
        # — see ``designs/SERVER_HARNESS_CONTRACT.md`` §"Async
        # work + inbox" for the rationale and migration notes.
        if tool_info.path is None:
            raise OmnigentError(
                f"tool {tool_info.name!r}: server-runtime tool has no "
                f"path. Server-runtime tools must declare a dotted "
                f"callable path; only client-runtime tools may omit it.",
                code=ErrorCode.INVALID_INPUT,
            )
        resolved = _resolve_dotted_attr(tool_info.path, tool_info.name)
        if not callable(resolved):
            raise OmnigentError(
                f"tool {tool_info.name!r}: {tool_info.path!r} resolved "
                f"to a non-callable {type(resolved).__name__}. Inner-stack "
                f"runner-protocol tools (``CancellableFunctionTool``) were "
                f"retired post-step-11; declare a plain callable and let "
                f"the LLM dispatch it asynchronously via ``sys_call_async`` "
                f"when cancellation is needed.",
                code=ErrorCode.INVALID_INPUT,
            )
        # ``parameters`` was populated on the forward trip from
        # the omnigent tool's ``input_schema``. Re-attach it so
        # the inner harness advertises the same JSON Schema to
        # the LLM that the YAML declared.
        tools[tool_info.name] = FunctionTool(
            name=tool_info.name,
            callable=resolved,
            input_schema=tool_info.parameters,
        )
    # Sub-agents named in tools.agents get an AgentTool entry so the
    # inner harness surfaces them as callable tools to the LLM.
    # Without this, coding_supervisor-style YAMLs lose their
    # claude_worker / codex_worker tools on the reverse trip.
    exposed = set(spec.tools.agents)
    for sub in spec.sub_agents:
        if sub.name and sub.name in exposed:
            tools[sub.name] = _sub_spec_to_agent_tool(sub)
    # MCP servers round-trip back to inner :class:`MCPTool` entries
    # so the omnigent harness surfaces them to the LLM via its
    # own MCP machinery (``omnigent.inner.mcp_tools``). The
    # forward direction translated ``MCPTool`` →
    # :class:`MCPServerConfig` (stdio or http); this is the inverse.
    # ``OmnigentExecutor.from_spec`` calls this reverse path when
    # wrapping an omnigent spec for an omnigent harness; if the
    # reverse trip dropped MCP entries the harness would lose every
    # MCP tool the spec declared.
    for mcp in spec.mcp_servers:
        tools[mcp.name] = _mcp_server_to_mcp_tool(mcp)
    return tools


def _mcp_server_to_mcp_tool(config: MCPServerConfig) -> MCPTool:
    """
    Build an inner :class:`MCPTool` from a native
    :class:`MCPServerConfig` — the reverse of
    :func:`_translate_mcp_tool_from_def`.

    Transport dispatch:

    - ``transport == "stdio"`` → ``MCPTool(command=...,
      args=..., env=...)``. Lists and dicts are copied so the
      inner tool doesn't share mutable state with the outer spec.
    - ``transport == "http"`` → ``MCPTool(url=..., headers=...)``.

    The spec validator rejects mixed-transport fields upstream, so
    the required-field check per branch here is a belt-and-
    suspenders for programmatic paths that bypass validation.

    :param config: The native :class:`MCPServerConfig` to invert.
    :returns: An :class:`MCPTool` with the equivalent transport
        shape for the inner omnigent runtime.
    :raises OmnigentError: If the config is missing the
        required field for its declared transport (a programmatic
        construction path that bypassed the validator).
    """
    if config.transport == "stdio":
        if config.command is None:
            raise OmnigentError(
                f"MCP server {config.name!r} transport='stdio' but command is None; "
                f"validator should have rejected this upstream",
                code=ErrorCode.INVALID_INPUT,
            )
        return MCPTool(
            command=config.command,
            args=list(config.args) if config.args else None,
            env=dict(config.env) if config.env else None,
        )
    if config.url is None:
        raise OmnigentError(
            f"MCP server {config.name!r} transport='http' but url is None; "
            f"validator should have rejected this upstream",
            code=ErrorCode.INVALID_INPUT,
        )
    return MCPTool(
        url=config.url,
        headers=dict(config.headers) if config.headers else None,
    )


def _sub_spec_to_agent_tool(sub: AgentSpec) -> AgentTool:
    """
    Rebuild an omnigent :class:`AgentTool` from a nested
    omnigent :class:`AgentSpec`.

    Inverse of :func:`_agent_tool_to_sub_spec`. Reads the sub-spec's
    ``llm.model`` and ``executor.config`` (harness / profile) to
    reconstruct the omnigent :class:`ExecutorSpec`. Lossy fields
    (``max_sessions``, ``os_env``, ``pass_history``,
    ``pass_histories``) are left at omnigent defaults.

    :param sub: The nested :class:`AgentSpec` representing a
        sub-agent exposed to the parent as a tool.
    :returns: A populated :class:`omnigent.tools.AgentTool`.
    """
    # Infer harness from model prefix when executor.config is empty
    # (native Omnigent v1 specs). Also forward os_env so sub-sessions get
    # the filesystem tools the spec declared.
    model = sub.llm.model if sub.llm is not None else None
    harness: str | None = sub.executor.config.get("harness")
    if not harness and model:
        harness = _infer_harness_from_model(model) or None
    harness = canonicalize_harness(harness)
    profile = sub.executor.config.get("profile")
    return AgentTool(
        name=sub.name,
        description=sub.description,
        prompt=sub.instructions,
        os_env=sub.os_env,
        executor=OmniExecutorSpec(
            model=model,
            harness=harness,
            profile=profile,
        ),
    )


def _resolve_dotted_attr(dotted_path: str, tool_name: str) -> Any:
    """
    Resolve a dotted import path to whatever object it names.

    Unlike :func:`_resolve_dotted_callable`, this does NOT require
    the resolved object to be callable — it's used by the tool-
    translation path where the resolved object may be either a
    function (→ wrap in :class:`FunctionTool`) OR a
    cancellable-runner instance (→ wrap in
    :class:`CancellableFunctionTool`). Callers are responsible
    for the shape check.

    :param dotted_path: Dotted import path to the callable, e.g.
        ``"examples._shared.tool_functions.get_current_time"``.
    :param tool_name: Tool name used only for error messages, e.g.
        ``"get_current_time"``.
    :returns: The resolved attribute object (callable or
        runner or other).
    :raises OmnigentError: If the path is not a dotted import
        path, the module cannot be imported, or the attribute is
        missing.
    """
    if "/" in dotted_path or dotted_path.endswith(".py"):
        raise OmnigentError(
            f"tool {tool_name!r} has filesystem path {dotted_path!r}; "
            "omnigent translator requires dotted import paths like "
            "'examples._shared.tool_functions.get_current_time'",
            code=ErrorCode.INVALID_INPUT,
        )
    if "." not in dotted_path:
        raise OmnigentError(
            f"tool {tool_name!r} path {dotted_path!r} must be a dotted path "
            "of the form 'module.attribute'",
            code=ErrorCode.INVALID_INPUT,
        )
    module_name, _, attr_name = dotted_path.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ValueError) as exc:
        # ``ValueError`` from importlib fires on empty module
        # names — what you get when the caller hands us a string
        # with no dots, e.g. ``"foo"`` → ``rpartition`` yields
        # ``("", "", "foo")``. Wrap both failure modes in a
        # single OmnigentError so the spec author sees a
        # consistent "use a dotted import path" message.
        raise OmnigentError(
            f"tool {tool_name!r}: cannot resolve {dotted_path!r} — "
            f"expected a dotted import path like "
            f"'examples.tool_functions.get_current_time'. "
            f"Underlying error: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if not hasattr(module, attr_name):
        raise OmnigentError(
            f"tool {tool_name!r}: module {module_name!r} has no attribute {attr_name!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return getattr(module, attr_name)


# Callable return type for tool callables. ``Callable[..., object]``
# is the best we can do because the tool functions have arbitrary
# signatures — the ``...`` args-form is the standard "we don't care
# about the args" spelling. Annotation carries a type-ignore because
# ``...`` expands to ``Any`` in mypy's internal representation; the
# alternative (listing every possible signature) isn't tractable.
def _resolve_dotted_callable(
    dotted_path: str,
    tool_name: str,
) -> Callable[..., Any]:
    """
    Resolve a dotted import path to a callable.

    Thin wrapper around :func:`_resolve_dotted_attr` that adds a
    callability check — the only difference between the two is
    the shape requirement on the resolved object.

    :param dotted_path: Dotted import path to the callable, e.g.
        ``"examples.tool_functions.get_current_time"``.
    :param tool_name: Tool name used only for error messages.
    :returns: The resolved callable.
    :raises OmnigentError: If the module can't be imported,
        the attribute is missing, or the resolved attribute is
        not callable.
    """
    resolved = _resolve_dotted_attr(dotted_path, tool_name)
    if not callable(resolved):
        raise OmnigentError(
            f"tool {tool_name!r}: {dotted_path!r} resolves to non-callable "
            f"{type(resolved).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    # ``resolved`` is typed ``object`` from _resolve_dotted_attr;
    # the runtime ``callable`` guard above narrows it. The cast
    # is only needed for mypy's return-type check.
    return cast(Callable[..., object], resolved)


# ── Policy translation (omnigent YAML dict → omnigent
# GuardrailsSpec) ────────────────────────────────────────────────
#
# We translate from the RAW YAML dict (read alongside the AgentDef)
# rather than from the parsed ``Policy`` objects. Agent-plane's own
# parser (``omnigent.spec.parser._parse_guardrails``) is invoked
# on the translated dict so the field-level validation rules (e.g.
# PhaseSelector-on-INPUT rejection, action enum coercion, label
# schema value checks) land in one place.


def _translate_guardrails_yaml(
    raw_policies: dict[str, Any] | None,
    raw_labels: dict[str, Any] | None,
    raw_label_schema: dict[str, Any] | None,
    raw_ask_timeout: Any,
    *,
    parent_profile: str | None = None,
) -> GuardrailsSpec | None:
    """
    Translate the guardrails-related top-level fields of an
    omnigent YAML into an omnigent :class:`GuardrailsSpec`.

    Omnigent declares labels/policies at the top level
    (``labels:``, ``label_schema:``, ``policies:``,
    ``ask_timeout:``); omnigent groups them under a single
    ``guardrails:`` block. This helper transforms the omnigent
    shape into the omnigent dict shape and delegates to
    :func:`omnigent.spec.parser._parse_guardrails` for
    validation — so every field-level rule (phase/tool validity,
    action enum coercion, label schema constraints) lands in the
    shared parser rather than being reimplemented here.

    Returns ``None`` when all four inputs are empty / absent —
    the synthesized :class:`AgentSpec` then carries
    ``guardrails=None`` and the runtime builds a no-op
    :class:`PolicyEngine` (per POLICIES.md §10 zero-policy case).

    :param raw_policies: Omnigent YAML ``policies:`` mapping,
        keyed by policy name, e.g.
        ``{"block_long_sleep": {"type": "function",
        "on": ["tool_call"],
        "handler": "examples.tool_functions.block_long_sleep"}}``.
        ``None`` or empty means no policies.
    :param raw_labels: Omnigent YAML top-level ``labels:`` map
        of ``{label_key: initial_value}``. ``None`` or empty means
        no labels — a schema-only label (``label_schema`` entry
        without a ``labels`` entry) is still allowed and gets
        ``initial: None``.
    :param raw_label_schema: Omnigent YAML top-level
        ``label_schema:`` map of
        ``{label_key: {values: [...], monotonic: "max"|"min"|"none"}}``.
    :param raw_ask_timeout: Omnigent YAML top-level
        ``ask_timeout:`` (seconds), e.g. ``30``. Passed through
        to ``guardrails.ask_timeout``. ``None`` means use the
        omnigent default (see
        :data:`omnigent.spec.types.DEFAULT_ASK_TIMEOUT`).
    :param parent_profile: Databricks profile from the parent
        agent's executor (``agent_def.executor.profile``). Used
        to resolve ``type: prompt`` policy LLM credentials at
        translation time — see
        :func:`_translate_prompt_policy_yaml`. ``None`` skips
        injection (policies must then carry their own
        ``connection:`` or fail at classifier call time).
    :returns: A validated :class:`GuardrailsSpec`, or ``None``
        when no guardrails-related fields were declared in the
        source YAML.
    :raises OmnigentError: On malformed entries — same error
        shape the omnigent YAML parser produces for a native
        ``guardrails:`` block, so YAML authors see consistent
        error messages whether their spec came from omnigent
        or omnigent.
    """
    if not raw_policies and not raw_labels and not raw_label_schema and raw_ask_timeout is None:
        return None
    # Build the omnigent-shaped guardrails dict, then hand it to
    # the native parser. Import locally to avoid a circular import
    # at module load time (parser imports from spec.types, which is
    # also this module's caller chain).
    from omnigent.spec.parser import _parse_guardrails

    translated: dict[str, Any] = {}
    labels_yaml = _translate_labels_yaml(raw_labels, raw_label_schema)
    if labels_yaml:
        translated["labels"] = labels_yaml
    policies_yaml = _translate_policies_yaml(raw_policies, parent_profile=parent_profile)
    if policies_yaml:
        translated["policies"] = policies_yaml
    if raw_ask_timeout is not None:
        translated["ask_timeout"] = raw_ask_timeout
    # ``expand_env`` is False because the upstream omnigent
    # loader has already expanded env vars in the dict we're
    # handed (per its own conventions). Running the expansion a
    # second time would double-expand ``$`` characters that happen
    # to appear in a legitimately-resolved value.
    return _parse_guardrails(translated, expand_env=False)


def _translate_labels_yaml(
    raw_labels: dict[str, Any] | None,
    raw_label_schema: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """
    Merge omnigent' separate ``labels:`` (initial values) and
    ``label_schema:`` (schemas) into Omnigent' unified
    ``guardrails.labels:`` shape.

    Agent-plane's :class:`LabelDef` bundles ``initial``,
    ``values``, and ``monotonic`` into one entry per key. The
    omnigent ``monotonic: none`` sentinel maps to "no
    monotonic constraint" on omnigent (field simply omitted
    from the dict).

    :param raw_labels: Initial values map, e.g.
        ``{"integrity": "1", "confidentiality": "0"}``.
    :param raw_label_schema: Schema map, e.g.
        ``{"integrity": {"values": ["0", "1"], "monotonic": "min"}}``.
    :returns: Agent-plane-shaped labels dict, e.g.
        ``{"integrity": {"initial": "1", "values": ["0", "1"],
        "monotonic": "decreasing"}}``. Empty dict when both
        inputs are empty.
    """
    initials = raw_labels or {}
    schemas = raw_label_schema or {}
    keys = set(initials.keys()) | set(schemas.keys())
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        entry: dict[str, Any] = {}
        if key in initials:
            entry["initial"] = initials[key]
        schema = schemas.get(key, {})
        if isinstance(schema, dict):
            if "values" in schema:
                entry["values"] = schema["values"]
            monotonic_raw = schema.get("monotonic")
            if monotonic_raw in _OMNI_TO_AP_MONOTONIC:
                entry["monotonic"] = _OMNI_TO_AP_MONOTONIC[monotonic_raw]
            elif monotonic_raw not in (None, "none"):
                # Unknown monotonic value — let the omnigent
                # parser produce its own error downstream. We
                # don't silently drop.
                entry["monotonic"] = monotonic_raw
        out[key] = entry
    return out


def _translate_policies_yaml(
    raw_policies: dict[str, Any] | None,
    *,
    parent_profile: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Translate the omnigent ``policies:`` mapping entry-by-entry
    into the omnigent shape.

    :param raw_policies: Raw ``policies:`` map from the omnigent
        YAML, keyed by policy name.
    :param parent_profile: Databricks profile from the parent
        agent's executor — threaded down so ``type: prompt``
        policies without their own ``connection:`` can resolve
        credentials at translation time. See
        :func:`_translate_prompt_policy_yaml`.
    :returns: Agent-plane-shaped ``policies:`` map — same keys,
        transformed values. Empty dict when *raw_policies* is
        ``None`` or empty.
    """
    if not raw_policies:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for policy_name, raw_entry in raw_policies.items():
        if not isinstance(raw_entry, dict):
            # Let the downstream parser raise its own message for
            # malformed entries — it already produces a specific
            # error naming the field.
            out[policy_name] = raw_entry
            continue
        out[policy_name] = _translate_policy_entry_yaml(
            policy_name,
            raw_entry,
            parent_profile=parent_profile,
        )
    return out


def _translate_policy_entry_yaml(
    policy_name: str,
    raw_entry: dict[str, Any],
    *,
    parent_profile: str | None = None,
) -> dict[str, Any]:
    """
    Dispatch a single policy entry to its type-specific
    translator.

    :param policy_name: YAML key under ``policies:``, e.g.
        ``"block_long_sleep"`` — included in error messages.
    :param raw_entry: Raw YAML mapping for this policy.
    :param parent_profile: Databricks profile from the parent
        agent's executor. Only consumed by the ``type: prompt``
        branch — function policies don't carry LLM configs so
        the profile is irrelevant there.
    :returns: Agent-plane-shaped dict.
    :raises OmnigentError: When the entry declares a policy
        ``type`` the translator doesn't recognize.
    """
    policy_type = raw_entry.get("type", _POLICY_TYPE_FUNCTION)
    if policy_type not in _KNOWN_POLICY_TYPES:
        raise OmnigentError(
            f"omnigent policy {policy_name!r}: unknown type "
            f"{policy_type!r} (must be one of {sorted(_KNOWN_POLICY_TYPES)})",
            code=ErrorCode.INVALID_INPUT,
        )
    if policy_type == _POLICY_TYPE_FUNCTION:
        return _translate_function_policy_yaml(raw_entry)
    # policy_type == _POLICY_TYPE_PROMPT (exhaustive after the
    # _KNOWN_POLICY_TYPES guard above).
    return _translate_prompt_policy_yaml(raw_entry, parent_profile=parent_profile)


def _translate_function_policy_yaml(
    raw_entry: dict[str, Any],
) -> dict[str, Any]:
    """
    Translate an omnigent ``type: function`` policy to the
    omnigent shape.

    Field mapping:

    - ``handler: path.to.fn`` → ``function: path.to.fn``
    - ``handler: path.to.fn`` + ``factory_params: {...}`` →
      ``function: {path: path.to.fn, arguments: {...}}``
    - Other fields (``condition``, ``action``, ``set_labels``,
      ``ask_timeout``) pass through unchanged. ``on:`` is
      intentionally excluded — ``type: function`` policies no
      longer accept ``on:`` on the omnigent side; the handler
      self-selects which events to act on by returning ``None`` to
      abstain.

    :param raw_entry: Raw YAML mapping for one ``type: function``
        policy, e.g. ``{"type": "function",
        "handler": "examples.block_long_sleep"}``.
    :returns: Agent-plane-shaped dict, e.g.
        ``{"type": "function",
        "function": "examples.block_long_sleep"}``.
    """
    out: dict[str, Any] = {}
    # Copy passthrough fields first so the order stays stable for
    # readability. ``type`` is kept for dispatch on the
    # omnigent parser side. ``on:`` is intentionally omitted —
    # function policies are now always called for all phases; the
    # handler self-selects by returning ``None`` to abstain.
    for key in ("type", "condition", "action", "set_labels", "ask_timeout"):
        if key in raw_entry:
            out[key] = raw_entry[key]
    # If ``function:`` is already present in omnigent native
    # format, pass through as-is (no shim wrapping needed).
    if "function" in raw_entry:
        out["function"] = raw_entry["function"]
        if "on" in raw_entry:
            out["on"] = raw_entry["on"]
        return out
    # Accept both ``handler:`` (current name) and ``callable:`` (legacy alias).
    callable_path = raw_entry.get("handler") or raw_entry.get("callable")
    factory_params = raw_entry.get("factory_params")
    if callable_path is None:
        # Let the omnigent parser surface the "function:
        # required" error — it already does so with a clear
        # message referencing the policy name.
        return out
    # Route every omnigent-sourced function policy through the
    # legacy-compat shim so author callables written with the
    # omnigent ``(content, phase)`` / ``(content, phase, context)``
    # convention keep working under Omnigent'
    # ``(ctx, context)`` convention. For omnigent-native
    # callables the shim is a pass-through (detected by parameter
    # names at policy-build time; see _omnigent_legacy_shim).
    # The wrapper lives at load time — zero cost in the engine's
    # hot evaluate() loop.
    shim_args: dict[str, Any] = {"target": callable_path}
    if factory_params:
        shim_args["factory_kwargs"] = factory_params
    out["function"] = {
        "path": "omnigent.spec._omnigent_legacy_shim.build",
        "arguments": shim_args,
    }
    return out


def _resolve_profile_to_connection(profile: str) -> dict[str, str] | None:
    """
    Resolve a Databricks profile name to a
    ``{base_url, api_key}`` dict by reading ``~/.databrickscfg``.

    Used at spec-translation time to make omnigent LLMConfig
    self-contained — Omnigent' LLM adapters take credentials
    via explicit ``connection:`` fields (per the "spec
    self-containment" design principle) and do not read env vars.
    Meanwhile omnigent declares Databricks creds via the
    ``profile:`` convenience shortcut. The translator closes the
    gap by resolving profile → connection once at load time.

    :param profile: Profile name in ``~/.databrickscfg``, e.g.
        ``"<your-profile>"``.
    :returns: ``{"base_url": "<host>/serving-endpoints", "api_key":
        "<token>"}`` when the profile is readable and has both
        host + token; ``None`` when the profile is missing or
        malformed (callers treat ``None`` as "skip injection" —
        the policy then falls back to whatever connection the
        YAML declared explicitly, or errors loud at call time).
    """
    # Import locally to avoid a top-level dependency on omnigent
    # runtime details from the spec module — only this single
    # translator path needs it.
    from omnigent.inner.databricks_executor import _read_databrickscfg

    creds = _read_databrickscfg(profile)
    if creds is None:
        return None
    return {
        "base_url": creds.host.rstrip("/") + "/serving-endpoints",
        "api_key": creds.token,
    }


def _translate_prompt_policy_yaml(
    raw_entry: dict[str, Any],
    *,
    parent_profile: str | None = None,
) -> dict[str, Any]:
    """
    Translate an omnigent ``type: prompt`` policy to the
    omnigent shape.

    Field mapping:

    - ``executor: {model: X}`` → ``llm: {model: X}``. The
      omnigent prompt policy uses ``executor`` to override
      the classifier LLM; omnigent uses ``llm``.
    - When *parent_profile* is set AND the policy has no
      explicit ``connection:``, resolve the profile to a
      ``connection: {base_url, api_key}`` dict so the
      omnigent classifier actually reaches the Databricks
      gateway. Without this, a policy declaring
      ``model: databricks-claude-sonnet-4`` parses as provider
      ``"openai"`` and the request hits ``api.openai.com``.
    - Other fields (``on``, ``condition``, ``prompt``,
      ``action``, ``set_labels``, ``ask_timeout``) pass through
      unchanged.

    :param raw_entry: Raw YAML mapping for one ``type: prompt``
        policy, e.g. ``{"type": "prompt", "on": ["request"],
        "executor": {"model": "databricks-claude-sonnet-4"},
        "prompt": "Deny Canada-related requests."}``.
    :param parent_profile: Databricks profile from the parent
        agent's ``executor.profile`` — usually set by the
        ``--profile`` CLI flag or the YAML's top-level
        ``executor.profile:`` field. When present and the
        policy has no explicit connection, the translator bakes
        the resolved ``{base_url, api_key}`` into the
        omnigent LLMConfig so the classifier call at runtime
        routes correctly. ``None`` means no profile — policies
        must carry their own credentials.
    :returns: Agent-plane-shaped dict with ``executor:`` renamed
        to ``llm:``.
    """
    del parent_profile  # No longer needed — prompt_policy uses event["llm_client"]
    # Translate type:prompt → type:function backed by the
    # prompt_policy builtin factory. The prompt and action fields
    # become factory_params; other fields pass through.
    factory_params: dict[str, Any] = {}
    prompt_text = raw_entry.get("prompt")
    if isinstance(prompt_text, str):
        factory_params["prompt"] = prompt_text
    out: dict[str, Any] = {"type": "function"}
    for key in ("on", "condition", "ask_timeout"):
        if key in raw_entry:
            out[key] = raw_entry[key]
    out["function"] = {
        "path": "omnigent.policies.builtins.prompt.prompt_policy",
        "arguments": factory_params,
    }
    return out


# ── Reverse direction: AgentDef → AgentSpec ────────────────────


def agent_def_to_agent_spec(
    agent_def: AgentDef,
    *,
    raw_yaml: dict[str, Any] | None = None,
) -> AgentSpec:
    """
    Translate an omnigent :class:`AgentDef` into an omnigent
    :class:`AgentSpec`.

    Reverse direction of :func:`agent_spec_to_agent_def`. Used by
    :func:`omnigent.spec.load` when handed an omnigent YAML
    so ``omnigent chat foo.yaml`` routes the spec through
    ``OmnigentExecutor`` at runtime.

    The produced spec sets ``executor.type = "omnigent"`` so the
    runtime picks ``OmnigentExecutor``, with ``harness`` and
    ``profile`` carried in ``executor.config``.

    :param agent_def: Parsed omnigent agent definition,
        typically from :func:`omnigent.loader.load_agent_def`.
        Example: ``AgentDef(name="hello_world",
        prompt="You are a friendly assistant.", ...)``.
    :param raw_yaml: Optional raw YAML dict the ``agent_def`` was
        parsed from. Required to preserve label-policy YAML-level
        fields (``condition``, ``match_tools``, ``action``,
        ``reason``, ``set_labels``) and to translate top-level
        ``labels:`` / ``label_schema:`` / ``ask_timeout:`` into
        Omnigent' ``guardrails:`` block — the omnigent
        loader compiles these into synthetic FunctionPolicy
        callables and drops the YAML-level fields, so we can't
        recover them from the parsed ``AgentDef`` alone. When
        ``None``, policies / labels are ignored (policy-less
        YAMLs continue to translate fine). Production callers
        (``load_omnigent_yaml``) read the raw YAML themselves
        and pass it through.
    :returns: A synthesized :class:`AgentSpec` equivalent. The
        round-trip ``agent_spec_to_agent_def(
        agent_def_to_agent_spec(d)) == d`` must hold for every
        representative fixture.
    :raises OmnigentError: When *agent_def* uses an omnigent
        concept Omnigent' :class:`AgentSpec` cannot currently
        represent. Each such case names the specific field.
        Current unsupported concepts: MCP-type tools.
    """
    _fail_on_unsupported_concepts_def(agent_def)
    name = _translate_name_from_def(agent_def.name)
    # ``instructions:`` (resolved from a sibling file or inline text by
    # ``omnigent.inner.loader.load_agent_def``) wins over ``prompt:``
    # when both are present. Authors who write ``instructions:
    # AGENTS.md`` are deliberately pointing at a file, and the
    # alternative — silently dropping it in favor of an inline
    # ``prompt:`` — was the bug from kasey_uhlenhuth's report.
    instructions: str | None
    if agent_def.instructions is not None:
        instructions = agent_def.instructions
    else:
        instructions = _translate_prompt_from_def(agent_def.prompt)
    raw_executor = raw_yaml.get("executor") if raw_yaml else None
    if not isinstance(raw_executor, dict):
        raw_executor = None
    llm_config = _translate_llm_from_def(agent_def.executor, raw_executor=raw_executor)
    executor_spec = _translate_executor_from_def(agent_def.executor, raw_executor=raw_executor)
    # Consolidate: ensure executor carries the authoritative model and
    # connection so downstream code reads from one place. The
    # translation functions above populate executor.model already;
    # sync connection from llm_config when present (rare in practice
    # — connection is usually resolved at runtime, not baked into the
    # translated spec).
    if llm_config is not None and llm_config.connection is not None:
        if executor_spec.connection is None:
            executor_spec.connection = llm_config.connection

    # Parent's Databricks profile (from ``--profile`` CLI arg or
    # YAML ``executor.profile``) propagates down to inline
    # sub-agents that don't declare one of their own. Without
    # this, a ``claude_worker`` inline AgentTool whose YAML
    # omits ``profile:`` inherits empty string — the inner
    # ClaudeSDKExecutor then resolves credentials with no
    # profile and 403s against the Databricks workspace. Pure
    # omnigent gets this "for free" via Session → factory; the
    # omnigent spawn path runs each sub-agent as an
    # independent task, so we have to bake the inheritance into
    # the spec.
    parent_profile = agent_def.executor.profile if agent_def.executor is not None else None
    # Same deal for the harness. Inline AgentTools in YAMLs like
    # ``coding_supervisor_with_forks.yaml`` declare ``prompt:`` +
    # ``os_env:`` + ``tools:`` but omit the ``executor:`` block,
    # relying on the parent's harness to flow down. Without this
    # propagation the sub-spec's harness is empty and the
    # validator rejects with ``sub_agents[...].executor.config.
    # harness: required``.
    #
    # We propagate the *effective* parent harness — i.e. the
    # harness after model-prefix auto-pick has run — not the raw
    # YAML value. A parent YAML like ``agent_with_subagent_session.yaml``
    # declares only ``executor.model: databricks-gpt-5-4-mini`` and
    # relies on auto-pick to resolve it to ``openai-agents``; if we
    # passed the raw ``""`` down, the child (which has no model of
    # its own) would end up with an empty harness and fail
    # validation.
    parent_harness: str | None = None
    if executor_spec.type == "omnigent":
        # executor_spec.config is ``dict[str, Any]`` by design
        # (kept widened by the tech-debt docstring on
        # :class:`ExecutorSpec`). We narrow here to a string
        # before handing off to the child; an empty string means
        # "no effective parent harness" so the child falls
        # through to its own model-prefix auto-pick / fail with
        # a clear validator error.
        resolved = executor_spec.config["harness"]
        assert isinstance(resolved, str)
        parent_harness = resolved if resolved else None
    # Same deal for the OS environment. Inline AgentTool sub-agents
    # that declare ``os_env: inherit`` need to see the parent's
    # concrete :class:`OSEnvSpec` baked into their own sub-spec at
    # translation time — the Omnigent path spawns each child as an
    # independent task so there's no live parent session to resolve
    # ``inherit`` from at runtime. ``None`` is fine: the sub-spec's
    # own os_env (or lack thereof) wins.
    parent_os_env = agent_def.os_env if agent_def.os_env is not None else None

    # Split omnigent tools by subtype:
    #   FunctionTool / CancellableFunctionTool
    #       → AgentSpec.local_tools (omnigent treats every
    #         tool as cancellable, so no subtype distinction).
    #   AgentTool
    #       → AgentSpec.sub_agents (one nested AgentSpec each)
    #         + ToolsConfig.agents (list of exposed names).
    #   MCPTool (stdio / subprocess)
    #       → AgentSpec.mcp_servers (one native stdio-transport
    #         MCPServerConfig each; subprocess is spawned by
    #         Omnigent' ToolManager, srt-wrapped when
    #         available — see omnigent/tools/mcp.py).
    #   MCPTool (HTTP) → same, but transport='http'.
    local_tools: list[LocalToolInfo] = []
    sub_agents: list[AgentSpec] = []
    agent_tool_names: list[str] = []
    mcp_servers: list[MCPServerConfig] = []
    for tool_name, tool in agent_def.tools.items():
        if isinstance(tool, FunctionTool | CancellableFunctionTool):
            local_tools.append(_translate_function_tool_from_def(tool_name, tool))
        elif isinstance(tool, SelfAgentTool):
            sub_agents.append(
                _self_agent_tool_to_sub_spec(
                    tool_name,
                    tool,
                    agent_def,
                    raw_yaml=raw_yaml,
                ),
            )
            agent_tool_names.append(tool_name)
        elif isinstance(tool, AgentTool):
            sub_agents.append(
                _agent_tool_to_sub_spec(
                    tool_name,
                    tool,
                    parent_profile=parent_profile,
                    parent_harness=parent_harness,
                    parent_os_env=parent_os_env,
                    # Inherit parent's terminals so inline
                    # sub-agents have a path to launch them — see
                    # the comment on the recipient parameter for
                    # why this is the simplest fix to the
                    # "supervisor delegates terminal work to
                    # workers" pattern.
                    parent_terminals=agent_def.terminals,
                ),
            )
            agent_tool_names.append(tool_name)
        elif isinstance(tool, MCPTool):
            mcp_servers.append(_translate_mcp_tool_from_def(tool_name, tool))

    tools_config = ToolsConfig(agents=agent_tool_names) if agent_tool_names else ToolsConfig()

    # Translate the guardrails block if the caller handed us the
    # raw YAML. Policies/labels are lifted from the omnigent
    # top-level YAML fields into Omnigent' ``guardrails:``
    # block; enforcement then happens in the omnigent workflow
    # (outside the omnigent harness) via the standard
    # :class:`PolicyEngine`. When ``raw_yaml`` is ``None`` we
    # don't have the YAML shape needed to translate label
    # policies, so we skip — callers in that position are either
    # unit tests that know their fixture has no policies or
    # legacy code paths that haven't been updated yet.
    guardrails: GuardrailsSpec | None = None
    if raw_yaml is not None:
        guardrails = _translate_guardrails_yaml(
            raw_policies=raw_yaml.get("policies"),
            raw_labels=raw_yaml.get("labels"),
            raw_label_schema=raw_yaml.get("label_schema"),
            raw_ask_timeout=raw_yaml.get("ask_timeout"),
            # Baked-in at translation time so Omnigent'
            # classifier LLM calls honor Databricks routing
            # (omnigent adapters don't read env vars per the
            # spec-self-containment design principle).
            parent_profile=parent_profile,
        )

    # Per designs/OMNIGENT_TERMINAL_BRIDGE.md §6.1 step 2 + §3 non-goal
    # bullet on sub-agent inheritance: thread the top-level agent's
    # ``terminals:`` declaration into ``AgentSpec.terminals``. Inline
    # AgentTool sub-specs (synthesized in ``_agent_tool_to_sub_spec``)
    # leave ``AgentSpec.terminals=None`` because ``AgentTool`` has no
    # ``terminals`` field in the omnigent grammar — matches legacy
    # non-AP mode exactly.
    terminals = dict(agent_def.terminals) if agent_def.terminals else None

    # Top-level ``skills:`` from the raw YAML (the omnigent
    # loader doesn't carry this field on AgentDef — it's
    # omnigent spec config that controls Claude SDK harness
    # host-skill loading). Validate the same shapes the
    # spec-side parser does so a typo here surfaces the same
    # error regardless of which spec format the user is on.
    skills_filter = _translate_skills_filter_from_yaml(raw_yaml)

    return AgentSpec(
        spec_version=_SYNTHETIC_SPEC_VERSION,
        name=name,
        llm=llm_config,
        instructions=instructions,
        local_tools=local_tools,
        sub_agents=sub_agents,
        tools=tools_config,
        executor=executor_spec,
        guardrails=guardrails,
        mcp_servers=mcp_servers,
        os_env=agent_def.os_env,
        terminals=terminals,
        timers=agent_def.timers,
        spawn=agent_def.spawn,
        skills_filter=skills_filter,
    )


def _translate_skills_filter_from_yaml(
    raw_yaml: dict[str, Any] | None,
) -> str | list[str]:
    """
    Pull the top-level YAML ``skills:`` field out of a raw
    omnigent-format YAML mapping and validate the same shapes
    the omnigent spec parser accepts (``"all"`` / ``"none"`` /
    list of names).

    The omnigent loader (``inner.loader.load_agent_def``) does
    NOT read this field — it's omnigent spec config that
    controls the Claude SDK harness's host-skill loading, not an
    omnigent runtime concept. So when the YAML is consumed via
    the legacy compat path, we re-read the raw YAML here to
    recover it.

    :param raw_yaml: The raw parsed YAML mapping the ``AgentDef``
        was synthesized from. ``None`` (no raw YAML available)
        falls back to ``"all"``.
    :returns: ``"all"``, ``"none"``, or a non-empty
        ``list[str]``. Normalizes ``[]`` → ``"none"`` to match
        the spec-side parser.
    :raises OmnigentError: When the value isn't one of the
        supported shapes (boolean, dict, integer), or list items
        are non-strings, or a string isn't ``"all"`` or
        ``"none"``.
    """
    if raw_yaml is None:
        return "all"
    raw = raw_yaml.get("skills")
    if raw is None:
        return "all"
    if isinstance(raw, str):
        if raw not in ("all", "none"):
            raise OmnigentError(
                f'top-level skills: must be "all", "none", or a list of '
                f"skill names; got string {raw!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        return raw
    if isinstance(raw, list):
        if len(raw) == 0:
            return "none"
        names: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise OmnigentError(
                    f"top-level skills: list items must be strings; "
                    f"got {type(item).__name__} {item!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            names.append(item)
        return names
    raise OmnigentError(
        f'top-level skills: must be "all", "none", or a list of skill '
        f"names; got {type(raw).__name__}",
        code=ErrorCode.INVALID_INPUT,
    )


def _self_agent_tool_to_sub_spec(
    tool_name: str,
    self_tool: SelfAgentTool,
    parent_agent_def: AgentDef,
    *,
    raw_yaml: dict[str, Any] | None,
) -> AgentSpec:
    """
    Materialize a self-clone sub-spec by re-translating the parent.

    Deep-copies the parent's :class:`AgentDef`, removes any
    :class:`SelfAgentTool` entries from the copy (recursion guard
    — without this, re-translating would re-enter this branch and
    loop forever at parse time), assigns the cloned def the
    sub-agent's tool name, and re-runs
    :func:`agent_def_to_agent_spec` to produce a fully-translated
    :class:`AgentSpec`. The result is a self-contained nested spec
    the Omnigent runtime spawns when the parent's LLM dispatches
    ``sys_session_send(tool=tool_name, ...)``.

    The clone inherits everything: model, system prompt /
    instructions, tools (minus the self-clone tools), executor,
    os_env, terminals, guardrails, timers, async_enabled, etc.
    Other AP-side concepts that the parent has (e.g.,
    ``previous_response_id`` chaining, conversation linkage) apply
    at runtime through the standard sub-agent dispatch path —
    nothing extra needed here.

    The ``raw_yaml`` is forwarded to the recursive call so the
    parent's ``policies`` / ``labels`` / ``label_schema`` /
    ``ask_timeout`` translate into the clone's
    :attr:`AgentSpec.guardrails` block too — clones share the
    parent's guardrails by design.

    Recursion at runtime (a clone spawning another clone) IS
    supported by AP's standard sub-agent dispatch path, but the
    parse-time tree is bounded to one level of cloning here. If a
    deeper parse-time clone tree is ever needed, lift the
    recursion guard and add a depth cap; not needed for v1.

    :param tool_name: The LLM-facing sub-agent name (e.g.,
        ``"subtask"``). Becomes the cloned spec's ``name`` and the
        value the parent's LLM passes as
        ``sys_session_send(tool=tool_name, ...)``.
    :param self_tool: The :class:`SelfAgentTool` instance, used
        only for its optional ``description`` (LLM-facing hint
        about what this sub-agent does, distinct from the
        parent's overall description).
    :param parent_agent_def: The parent's parsed
        :class:`AgentDef`. Deep-copied so the original isn't
        mutated.
    :param raw_yaml: Forwarded to the recursive translation so
        guardrails translate identically for parent + clone.
    :returns: A nested :class:`AgentSpec` representing the cloned
        sub-agent. Drops into the parent spec's
        :attr:`AgentSpec.sub_agents`.
    """
    cloned = copy.deepcopy(parent_agent_def)
    # Recursion guard: strip self-clone tools from the copy so the
    # recursive translation doesn't re-enter the SelfAgentTool branch.
    cloned.tools = {n: t for n, t in cloned.tools.items() if not isinstance(t, SelfAgentTool)}
    cloned.name = tool_name
    sub_spec = agent_def_to_agent_spec(cloned, raw_yaml=raw_yaml)
    if self_tool.description is not None:
        sub_spec.description = self_tool.description
    return sub_spec


def _agent_tool_to_sub_spec(
    tool_name: str,
    tool: AgentTool,
    *,
    parent_profile: str | None = None,
    parent_harness: str | None = None,
    parent_os_env: OSEnvSpec | None = None,
    parent_terminals: dict[str, TerminalEnvSpec] | None = None,
) -> AgentSpec:
    """
    Translate an omnigent inline :class:`AgentTool` (sub-agent
    exposed as a tool) into a nested omnigent :class:`AgentSpec`.

    The parent's :attr:`ToolsConfig.agents` carries ``tool_name`` so
    the runtime surfaces this sub-agent to the parent LLM as a
    callable tool (matching the legacy ``functions.<tool_name>``
    visibility). The sub-spec itself is a regular
    :class:`AgentSpec` with ``executor.type == "omnigent"`` so the
    :class:`OmnigentExecutor` runs it when spawned.

    Lossy fields (not modeled on Omnigent' AgentSpec yet):
    ``max_sessions``, ``pass_history``, ``pass_histories``.
    omnigent' runtime falls back to its defaults for these on
    the reverse trip.

    :param tool_name: The YAML key under which this AgentTool is
        declared on the parent, e.g. ``"claude_worker"``.
    :param tool: The parsed omnigent :class:`AgentTool`.
    :param parent_profile: Databricks profile carried on the
        parent agent's executor. Used as the sub-spec's profile
        when the inline AgentTool omits one — inline agents in
        YAMLs like ``coding_supervisor.yaml`` typically declare
        ``harness: claude-sdk`` + ``model: ...`` but leave
        ``profile:`` out, relying on the CLI's ``--profile`` flag
        to flow down. Agent-plane spawns each sub-agent as an
        independent task, so we bake the inheritance into the
        spec here. Empty string means "no parent profile known";
        the sub-spec then falls through the same way its own
        empty field does.
    :param parent_harness: Omnigent harness carried on the
        parent agent's executor. Used as the sub-spec's harness
        when the inline AgentTool's ``executor:`` block is
        absent. Same pattern as *parent_profile* — YAMLs like
        ``coding_supervisor_with_forks.yaml`` declare just
        ``prompt:`` + ``os_env:`` + ``tools:`` on their workers,
        expecting the parent's harness to flow down. Pure
        omnigent resolves this at ``create_executor`` time;
        omnigent has to do it at spec-translation time because
        each child runs as an independent task.
    :param parent_os_env: OS environment on the parent agent.
        Used to resolve the ``os_env: inherit`` sentinel on the
        inline AgentTool — legacy omnigent resolves it from the
        live Session at runtime, but omnigent runs each child
        as an independent task with no live parent to consult,
        so we resolve at translation time. ``None`` means "no
        parent os_env known": if the tool said ``inherit``, its
        resolved os_env is also ``None`` and the sub-agent boots
        without filesystem access (matching legacy behavior when
        the parent itself has no os_env).
    :returns: A nested :class:`AgentSpec` representing the
        sub-agent.
    """
    oa_executor = tool.executor if isinstance(tool.executor, OmniExecutorSpec) else None
    sub_os_env = _resolve_inline_agent_tool_os_env(tool, parent_os_env)
    # Inherit the parent's ``terminals:`` declaration into the
    # sub-spec so inline sub-agents can launch ``sys_terminal_*``
    # in their own conversation. Without this, the sub-agent's
    # ``ToolManager`` short-circuits the sys_terminal_* registration
    # (see ``omnigent/tools/manager.py:426`` — "if not
    # self._spec.terminals: return") and the sub-agent has no way
    # to spawn a terminal even though the parent has one configured.
    # Each sub-agent's launches land in its OWN conversation registry
    # (registries are keyed by ``conversation_id``), so the REPL's
    # cross-conversation walker (``_collect_terminals_for_conversations``)
    # surfaces them under the sub-agent target in the sidebar.
    # ``dict()`` clones so mutations to the sub-spec's terminals
    # don't ripple back to the parent's declaration.
    sub_terminals = dict(parent_terminals) if parent_terminals else None
    # Recurse into the tool's own nested tools to translate
    # sub-sub-agents and local callables. Without this, inline
    # sub-agents more than one level deep (e.g. researcher →
    # fact_checker) are silently dropped from the spec tree.
    child_sub_agents: list[AgentSpec] = []
    child_local_tools: list[LocalToolInfo] = []
    child_agent_names: list[str] = []
    effective_profile = (oa_executor.profile if oa_executor else None) or parent_profile
    effective_harness = (oa_executor.harness if oa_executor else None) or parent_harness
    for nested_name, nested_tool in (tool.tools or {}).items():
        if isinstance(nested_tool, AgentTool):
            child_sub_agents.append(
                _agent_tool_to_sub_spec(
                    nested_name,
                    nested_tool,
                    parent_profile=effective_profile,
                    parent_harness=effective_harness,
                    parent_os_env=sub_os_env,
                    parent_terminals=sub_terminals,
                ),
            )
            child_agent_names.append(nested_name)
        elif isinstance(nested_tool, FunctionTool | CancellableFunctionTool):
            child_local_tools.append(
                _translate_function_tool_from_def(nested_name, nested_tool),
            )
    child_tools_config = (
        ToolsConfig(agents=child_agent_names) if child_agent_names else ToolsConfig()
    )
    return AgentSpec(
        spec_version=_SYNTHETIC_SPEC_VERSION,
        name=tool_name,
        description=tool.description if tool.description else None,
        instructions=tool.prompt if tool.prompt else None,
        llm=_translate_llm_from_def(oa_executor),
        executor=_translate_executor_from_def(
            oa_executor,
            parent_profile=parent_profile,
            parent_harness=parent_harness,
        ),
        os_env=sub_os_env,
        terminals=sub_terminals,
        sub_agents=child_sub_agents,
        local_tools=child_local_tools,
        tools=child_tools_config,
    )


def _resolve_inline_agent_tool_os_env(
    tool: AgentTool,
    parent_os_env: OSEnvSpec | None,
) -> OSEnvSpec | None:
    """
    Resolve the ``os_env`` field on an inline :class:`AgentTool`
    declaration against the parent's concrete os_env.

    Omnigent' :class:`AgentTool.os_env` is
    ``OSEnvSpec | str | None`` with ``"inherit"`` as the sentinel
    string. Agent-plane spawns each inline child as an
    independent task at runtime, so we can't defer the
    resolution — we bake in the parent's os_env here so the
    sub-spec is self-contained.

    :param tool: The parsed :class:`AgentTool`.
    :param parent_os_env: The parent agent's own
        :class:`OSEnvSpec`, or ``None`` if the parent also has
        none.
    :returns: The :class:`OSEnvSpec` the sub-agent should use
        at runtime, or ``None`` when no os_env is declared at
        either level.
    """
    tool_os_env = tool.os_env
    if tool_os_env is None:
        return None
    if isinstance(tool_os_env, str):
        # Only the ``"inherit"`` sentinel is meaningful; other
        # strings would be an omnigent YAML that the loader
        # already rejects, so we don't invent a behavior for
        # them and return None.
        if tool_os_env == _OS_ENV_INHERIT_SENTINEL:
            return parent_os_env
        return None
    return tool_os_env


def _fail_on_unsupported_concepts_def(agent_def: AgentDef) -> None:
    """
    Raise :class:`OmnigentError` for every omnigent concept
    Omnigent' :class:`AgentSpec` cannot currently represent.

    Each unsupported concept gets its own clear error message
    naming the specific field.

    :param agent_def: Parsed omnigent agent definition.
    :raises OmnigentError: On the first unsupported concept
        encountered. Currently the only rejection is MCP-type
        tools; ``policies`` are lifted into
        ``AgentSpec.guardrails.policies`` and enforced by the
        omnigent workflow (see
        :func:`_translate_guardrails_yaml`), and ``os_env`` is
        translated and placed on ``AgentSpec.os_env``.
    """
    for tool_name, tool in agent_def.tools.items():
        _fail_on_unsupported_tool(agent_def.name, tool_name, tool)


def _fail_on_unsupported_tool(
    agent_name: str | None,
    tool_name: str,
    tool: Tool,
) -> None:
    """
    Raise :class:`OmnigentError` when *tool* uses an omnigent
    tool concept omnigent cannot currently represent.

    :param agent_name: Enclosing agent name, for error messages,
        e.g. ``"coder"``. ``None`` when the agent was not given a
        name — the error message then renders ``None`` verbatim.
    :param tool_name: The YAML key for this tool, e.g.
        ``"glean_search"``.
    :param tool: The parsed omnigent tool instance.
    :raises OmnigentError: On MCP tools or cancellable_function
        tools.
    """
    # FunctionTool, CancellableFunctionTool, and AgentTool are all
    # supported — the caller dispatches them appropriately.
    # Agent-plane treats every tool as cancellable at the runtime
    # layer so FunctionTool vs CancellableFunctionTool isn't a
    # meaningful distinction at the spec level.
    if isinstance(tool, FunctionTool | CancellableFunctionTool | AgentTool):
        return
    if isinstance(tool, MCPTool):
        # MCPTool is translated into ``AgentSpec.mcp_servers`` by
        # :func:`_translate_mcp_tool_from_def` — except for the
        # ``databricks_server=<name>`` shape, which references a
        # Databricks-managed MCP endpoint that Omnigent'
        # runtime has no resolver for yet. Reject that shape loud;
        # HTTP + stdio MCPTools fall through and translate below.
        if tool.databricks_server is not None:
            raise OmnigentError(
                f"omnigent agent {agent_name!r} declares tool "
                f"{tool_name!r} of type `mcp` with "
                f"``databricks_server={tool.databricks_server!r}`` — "
                f"Omnigent' MCPServerConfig doesn't understand the "
                f"named-Databricks-server shape. Translate to an "
                f"explicit HTTP ``url`` or stdio ``command``+``args`` "
                f"MCP first.",
                code=ErrorCode.INVALID_INPUT,
            )
        return


def _translate_name_from_def(raw_name: str | None) -> str | None:
    """
    Translate omnigent ``AgentDef.name`` to :attr:`AgentSpec.name`.

    ``AgentDef.name`` is ``str | None`` on the omnigent side;
    omnigent also uses ``None`` for absence. An empty string on
    either side collapses to ``None`` so the round-trip invariant
    holds.

    :param raw_name: The raw ``name`` field from
        :class:`AgentDef`, e.g. ``"hello_world"``, ``""``, or
        ``None``.
    :returns: The omnigent name, or ``None`` when *raw_name*
        is absent or empty.
    """
    return raw_name if raw_name else None


def _translate_prompt_from_def(raw_prompt: str | None) -> str | None:
    """
    Translate omnigent ``AgentDef.prompt`` to
    :attr:`AgentSpec.instructions`.

    ``AgentDef.prompt`` is ``str | None`` on the omnigent side;
    omnigent also uses ``None`` for absence.

    :param raw_prompt: The raw ``prompt`` field from
        :class:`AgentDef`, e.g. ``"You are a friendly
        assistant."``, ``""``, or ``None``.
    :returns: The omnigent instructions text, or ``None``
        when *raw_prompt* is absent or empty.
    """
    return raw_prompt if raw_prompt else None


def _translate_llm_from_def(
    oa_executor: OmniExecutorSpec | None,
    *,
    raw_executor: dict[str, Any] | None = None,
) -> LLMConfig | None:
    """
    Translate omnigent ``executor.model`` into an omnigent
    :class:`LLMConfig`.

    omnigent stores the model string on its own
    :class:`omnigent.datamodel.ExecutorSpec`. omnigent splits
    model selection (``llm.model``) from executor selection
    (``executor.type``). We surface the model in
    :attr:`AgentSpec.llm.model`; the executor block keeps
    ``type = "omnigent"`` plus the ``harness`` / ``profile``
    config.

    :param oa_executor: The omnigent
        :class:`~omnigent.datamodel.ExecutorSpec`, or ``None``
        when the YAML omitted the ``executor:`` block. Example:
        ``OmniExecutorSpec(model="databricks-claude-sonnet-4")``.
    :param raw_executor: Optional raw omnigent YAML ``executor:``
        mapping. When present and it carries an ``extra:`` dict,
        those kwargs flow into :attr:`LLMConfig.extra` so harness-
        specific overrides (``max_turns`` for the openai-agents
        harness, ``temperature`` for any harness, etc.) propagate
        through to :class:`omnigent.executor.ExecutorConfig.extra`.
        The omnigent loader drops unknown fields on
        ``ExecutorSpec`` at parse time, so we read them back from
        the raw dict here.
    :returns: An :class:`LLMConfig` with ``model`` populated, or
        ``None`` when omnigent declared no model.
    """
    if oa_executor is None or not oa_executor.model:
        return None
    extra: dict[str, Any] = {}
    if raw_executor is not None:
        raw_extra = raw_executor.get("extra")
        if isinstance(raw_extra, dict):
            extra = dict(raw_extra)
    return LLMConfig(model=oa_executor.model, extra=extra)


def _translate_executor_from_def(
    oa_executor: OmniExecutorSpec | None,
    *,
    parent_profile: str | None = None,
    parent_harness: str | None = None,
    raw_executor: dict[str, Any] | None = None,
) -> ExecutorSpec:
    """
    Build the omnigent :class:`ExecutorSpec` for an omnigent
    agent.

    Returns ``type="omnigent"`` when a harness can be resolved,
    routing the agent through ``OmnigentExecutor``. Raises
    :class:`OmnigentError` when a model is declared but no
    harness can be inferred — the spec must explicitly declare a
    harness or use a model whose prefix maps to a known harness
    (e.g. ``databricks-claude-*`` maps to ``claude-sdk``).

    The ``harness`` and ``profile`` fields from omnigent land in
    ``executor.config`` so :meth:`OmnigentExecutor.from_spec` can
    read them back via the typed dict (no setattr-on-dataclass
    trickery). ``os_env`` is NOT carried here — callers populate
    :attr:`AgentSpec.os_env` directly.

    Harness-resolution precedence (first non-empty wins):

    1. ``oa_executor.harness`` — explicit declaration in the YAML's
       ``executor:`` block.
    2. *parent_harness* — the parent agent's harness, threaded
       through by :func:`_agent_tool_to_sub_spec` for inline
       AgentTools that omit their own ``executor:`` block.
    3. :func:`_infer_harness_from_model` on the resolved model
       string — mirrors pure omnigent' CLI auto-pick
       (``databricks-claude-*`` → ``claude-sdk``, etc.) so YAMLs
       that declare only a model continue to work under Omnigent mode.
    4. Error — when all of the above yield an empty string and a
       model is declared, the spec is invalid (no harness can be
       inferred for the model).

    Empty-string coercion mirrors omnigent' own dataclass
    defaults (``ExecutorSpec.harness: str = ""``,
    ``ExecutorSpec.profile: str = ""``) so the round-trip is
    bit-exact against the omnigent side.

    :param oa_executor: The omnigent ``ExecutorSpec`` or
        ``None``.
    :param parent_profile: Profile to fall back to when
        *oa_executor* has no profile of its own. Used by
        inline-AgentTool sub-specs so the parent's CLI
        ``--profile`` flows into the child task's executor —
        otherwise Databricks-backed harnesses 403 on the empty
        profile. Ignored when the inline spec already carries a
        profile (explicit always wins).
    :param parent_harness: Harness to fall back to when
        *oa_executor* has no harness of its own. Matches the
        profile fallback's semantics — inline-AgentTool sub-
        specs that omit ``executor:`` rely on the parent's
        harness, and the validator requires harness to be one
        of the supported set so an empty string fails
        hard there.
    :param raw_executor: Optional raw YAML ``executor:`` mapping.
        When present, ``use_responses`` (``bool | None``) is read
        from it and forwarded into ``executor.config["use_responses"]``
        so the openai-agents harness subprocess reads the correct
        API surface (chat/completions vs. responses). The omnigent
        loader silently drops unknown fields on its own
        :class:`~omnigent.inner.datamodel.ExecutorSpec`, so we
        have to recover this field from the raw dict here.
    :returns: An :class:`ExecutorSpec` with ``type="omnigent"``
        when a harness is known.
    :raises OmnigentError: When a model is declared but no
        harness can be inferred from it.
    """
    # Omnigent' :class:`ExecutorSpec.{harness,profile,model}`
    # fields are ``str | None`` on the dataclass (post-graduation
    # from the old empty-string-as-absent convention). The rest of
    # this function and its downstream callers treat ``""`` as
    # "field absent" (``if not harness:`` etc.), so coerce ``None``
    # to ``""`` at the top of the function — one normalization
    # point instead of sprinkling ``is None`` checks through every
    # branch below. The config dict that ends up in
    # :attr:`ExecutorSpec.config` uses ``""`` as its sentinel too;
    # the forward-direction reader at line ~1120
    # (``assert isinstance(resolved, str)``) fails loud if a
    # ``None`` ever leaks through.
    harness = oa_executor.harness if oa_executor is not None else None
    if harness is None:
        harness = ""
    harness = canonicalize_harness(harness) or ""
    profile = oa_executor.profile if oa_executor is not None else None
    if profile is None:
        profile = ""
    # Harness resolution precedence:
    #   (1) explicit child ``executor.harness``  — already in
    #       ``harness`` above.
    #   (2) child's own model auto-picks — if the child declared
    #       its own model, that model's prefix is a strong intent
    #       signal and dominates parent inheritance (a Claude-model
    #       child under a GPT-model parent should run on
    #       claude-sdk, not inherit the parent's openai-agents).
    #   (3) parent harness — inline AgentTools that omit their
    #       own ``executor:`` block entirely inherit from parent.
    #   (4) auto-pick on whatever model happens to be present as
    #       a last-ditch effort (empty model → empty harness,
    #       validator rejects with a clear error).
    model = oa_executor.model if oa_executor is not None else None
    if model is None:
        model = ""
    if not harness and model:
        harness = _infer_harness_from_model(model)
    if not harness and parent_harness:
        harness = parent_harness
    if not harness and model:
        # Belt-and-suspenders fallback after the parent_harness
        # branch. ``and model`` matches the first branch's guard
        # so mypy narrows ``model`` to ``str`` for the call.
        harness = _infer_harness_from_model(model)
    # Inherit the parent's Databricks profile when the child doesn't
    # specify its own, EXCEPT for subscription-auth harnesses
    # (claude-sdk, codex). Those route through the locally installed
    # CLI's subscription auth; inheriting a parent profile would
    # trigger Databricks routing and bypass subscription auth.
    # This check runs AFTER harness resolution so that children that
    # omit ``executor.harness`` but resolve to codex/claude-sdk via
    # model inference or parent inheritance are still excluded.
    if not profile and parent_profile and harness not in _SUBSCRIPTION_AUTH_HARNESSES:
        profile = parent_profile
    config: dict[str, Any] = {
        "harness": harness,
        "profile": profile,
    }
    # ``use_responses`` is not a field on the omnigent inner
    # ExecutorSpec (the loader drops unknown keys), so we read it
    # from the raw YAML dict and carry it forward explicitly.
    # The openai-agents harness spawn-env builder reads
    # ``spec.executor.config["use_responses"]`` to set
    # ``HARNESS_OPENAI_AGENTS_USE_RESPONSES``, which controls
    # whether the inner executor uses /responses or /chat/completions.
    auth: ApiKeyAuth | DatabricksAuth | None = None
    if raw_executor is not None:
        use_responses_raw = raw_executor.get("use_responses")
        if use_responses_raw is not None:
            config["use_responses"] = bool(use_responses_raw)
        # The antigravity harness opts into Vertex AI via
        # ``executor.config`` vertex/project/location, which
        # ``_build_antigravity_spawn_env`` reads to thread
        # ``HARNESS_ANTIGRAVITY_VERTEX`` / ``_PROJECT`` / ``_LOCATION``.
        # Like ``use_responses``, none of these are fields on the
        # omnigent inner ExecutorSpec (the loader drops unknown keys), so
        # recover them from the raw YAML and carry them forward — without
        # this, the documented Vertex config shape would never reach the
        # spawn-env builder. Each key is preserved only when present, so a
        # non-Vertex executor's config stays untouched.
        for _vertex_key in ("vertex", "project", "location"):
            if _vertex_key in raw_executor:
                config[_vertex_key] = raw_executor[_vertex_key]
        # ``auth:`` is also not in the omnigent datamodel — parse it
        # directly from the raw YAML so YAML-declared auth is not
        # silently dropped and overridden by the global config default.
        from omnigent.spec.parser import _parse_executor_auth

        auth = _parse_executor_auth(raw_executor)
    if not harness and model:
        raise OmnigentError(
            f"no harness can be inferred for model {model!r}. "
            f"Declare an explicit 'harness:' in the executor block "
            f"(e.g. 'openai-agents', 'claude-sdk') or use a model "
            f"whose prefix maps to a known harness.",
            code=ErrorCode.INVALID_INPUT,
        )
    return ExecutorSpec(
        type=OMNIGENT_EXECUTOR_TYPE,
        config=config,
        model=model or None,
        profile=profile or None,
        auth=auth,
    )


def _translate_mcp_tool_from_def(
    tool_name: str,
    tool: MCPTool,
) -> MCPServerConfig:
    """
    Translate one omnigent :class:`MCPTool` into a native
    :class:`MCPServerConfig`.

    Transport dispatch:

    - ``tool.url`` populated → ``transport="http"`` with optional
      ``headers`` (used by Databricks-profile MCPs that hit an
      SSE endpoint with a Bearer token).
    - ``tool.command`` populated → ``transport="stdio"`` with
      ``args`` + ``env`` carried through verbatim. The
      subprocess spawns unsandboxed (matching legacy omnigent,
      which never sandboxed MCPs); the AP-side ``sandbox: bool``
      field that wrapped the spawn with srt was removed in
      step 7 of the harness contract migration.

    :param tool_name: YAML key the tool was registered under,
        e.g. ``"glean"``. Used as :attr:`MCPServerConfig.name`
        when the tool doesn't override it.
    :param tool: The parsed omnigent :class:`MCPTool` instance.
    :returns: A fully populated :class:`MCPServerConfig`.
    :raises OmnigentError: If *tool* has neither ``url`` nor
        ``command`` (shouldn't happen for normal MCPTools, but the
        ``databricks_server`` shape is caught upstream by
        :func:`_fail_on_unsupported_tool`).
    """
    if tool.url is not None:
        return MCPServerConfig(
            name=tool_name,
            transport="http",
            url=tool.url,
            headers=dict(tool.headers) if tool.headers else {},
            databricks_profile=tool.profile,
        )
    if tool.command is not None:
        return MCPServerConfig(
            name=tool_name,
            transport="stdio",
            command=tool.command,
            args=list(tool.args) if tool.args else [],
            env=dict(tool.env) if tool.env else {},
        )
    raise OmnigentError(
        f"omnigent MCP tool {tool_name!r} has neither 'url' nor "
        f"'command' — cannot translate to an MCPServerConfig",
        code=ErrorCode.INVALID_INPUT,
    )


def _translate_function_tool_from_def(
    tool_name: str,
    tool: Tool,
) -> LocalToolInfo:
    """
    Translate one omnigent function tool into a
    :class:`LocalToolInfo`.

    The dotted callable path is stored in
    :attr:`LocalToolInfo.path` (e.g.
    ``"examples._shared.tool_functions.get_current_time"``). The forward
    translator (``agent_spec_to_agent_def``) resolves this via
    :func:`importlib.import_module` at executor-construction time.

    :param tool_name: The YAML key for this tool, e.g.
        ``"get_current_time"``.
    :param tool: The parsed omnigent tool. Must be a
        :class:`FunctionTool` at this point (fail-loud in
        :func:`_fail_on_unsupported_tool` rejects the other
        concrete subtypes).
    :returns: A :class:`LocalToolInfo` with ``name``, ``path``
        (dotted callable), and ``language`` =
        :data:`OMNIGENT_TOOL_LANGUAGE`.
    :raises OmnigentError: When *tool* is not a
        :class:`FunctionTool` (defensive guard).
    """
    if isinstance(tool, CancellableFunctionTool):
        raise OmnigentError(
            f"omnigent tool {tool_name!r}: "
            f"``CancellableFunctionTool`` (YAML ``type: "
            f"cancellable_function`` + ``runner:``) was retired in "
            f"step (c) of the harness contract migration. Replace "
            f"with ``type: function`` + ``callable:`` pointing at "
            f"a plain Python function; the LLM dispatches it "
            f"asynchronously via ``sys_call_async`` when "
            f"cancellation is needed. See "
            f'``designs/SERVER_HARNESS_CONTRACT.md`` §"Async work '
            f'+ inbox" for the rationale.',
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(tool, FunctionTool):
        raise OmnigentError(
            f"omnigent tool {tool_name!r}: expected FunctionTool, got {type(tool).__name__}.",
            code=ErrorCode.INVALID_INPUT,
        )
    # Client-runtime tools have no server-side callable — the SDK
    # consumer implements them at stream-start time. Skip path
    # recovery (would fail: there's nothing to import) and
    # introspection (no callable to inspect — the YAML
    # ``parameters:`` block is the only schema source for these,
    # which is why the validator requires it on client tools).
    if tool.runtime == "client":
        return LocalToolInfo(
            name=tool_name,
            path=None,
            language=OMNIGENT_TOOL_LANGUAGE,
            parameters=dict(tool.input_schema) if tool.input_schema else None,
            runtime=ToolRuntime.CLIENT,
        )
    # Unity Catalog tools declare ``catalog_path:`` instead of
    # ``callable:``. They are executed at tool-call time via
    # ``WorkspaceClient.statement_execution.execute_statement()``.
    # No server-side callable to resolve or introspect — the
    # parameter schema must come from the YAML ``parameters:``
    # block or be fetched from UC metadata at registration time
    # (future enhancement).
    catalog_path = getattr(tool, "catalog_path", None)
    if catalog_path:
        return LocalToolInfo(
            name=tool_name,
            path=None,
            language=OMNIGENT_TOOL_LANGUAGE,
            parameters=dict(tool.input_schema) if tool.input_schema else None,
            runtime=ToolRuntime.UC_FUNCTION,
            catalog_path=catalog_path,
            warehouse_id=getattr(tool, "warehouse_id", None),
            description=tool.description,
        )
    callable_path = _recover_callable_path(tool_name, tool)
    # Mirror the legacy ``Tool.tool_schema()`` precedence: an
    # explicit ``input_schema:`` declared in YAML wins; otherwise
    # introspect the resolved Python callable's signature so the
    # LLM gets ``{"properties": {<arg>: ...}, "required": [...]}``
    # for typed-parameter tools. Without this fallback, omnigent
    # YAMLs that declare a function tool with no ``input_schema:``
    # block (the common case for plain Python callables — e.g.
    # ``examples/_shared/tool_functions.web_search``) ship to the
    # LLM with empty parameters and the model invokes the tool
    # zero-arg, producing
    # ``TypeError: web_search() missing 1 required positional
    # argument`` at call time.
    parameters = (
        dict(tool.input_schema)
        if tool.input_schema
        else _schema_from_callable(tool.callable)
        if tool.callable is not None
        else None
    )
    return LocalToolInfo(
        name=tool_name,
        path=callable_path,
        language=OMNIGENT_TOOL_LANGUAGE,
        parameters=parameters,
        runtime=ToolRuntime.SERVER,
    )


def _recover_callable_path(
    tool_name: str,
    tool: FunctionTool,
) -> str:
    """
    Recover the dotted import path for a function-type tool's
    callable.

    omnigent' loader resolves the YAML's ``callable:`` field
    into a real Python object and drops the original string. We
    rehydrate the dotted path from the resolved object's
    ``__module__`` + ``__qualname__``. The legacy fallback that
    scanned a module for an attribute bound to a runner-instance
    was removed in step (c) — only plain callables are supported
    now, and ``__module__`` / ``__qualname__`` always recover the
    path correctly for them.

    :param tool_name: The YAML key for this tool (used in error
        messages).
    :param tool: A :class:`FunctionTool` whose ``callable``
        attribute is a resolved Python object.
    :returns: The dotted module path, e.g.
        ``"examples._shared.tool_functions.get_current_time"``.
    :raises OmnigentError: When the callable is missing
        (unresolved ``callable:``) or has no
        ``__module__``/``__qualname__`` to recover from.
    """
    callable_obj = tool.callable
    if callable_obj is None:
        raise OmnigentError(
            f"omnigent tool {tool_name!r}: function-type tool has "
            f"no resolved callable (the YAML's `callable:` field was "
            f"missing or could not be imported at load time). The "
            f"omnigent adapter needs a resolved callable to "
            f"recover its dotted path.",
            code=ErrorCode.INVALID_INPUT,
        )
    module = getattr(callable_obj, "__module__", None)
    qualname = getattr(callable_obj, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    raise OmnigentError(
        f"omnigent tool {tool_name!r}: callable {callable_obj!r} "
        f"has no recoverable dotted path (no __module__/__qualname__ "
        f"on a {type(callable_obj).__name__}).",
        code=ErrorCode.INVALID_INPUT,
    )
