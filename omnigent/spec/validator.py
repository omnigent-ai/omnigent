"""Validate an AgentSpec against the rules defined in AGENTSPEC.md."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from omnigent.spec.types import AgentSpec, ToolRuntime

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
# Agent names appear as components of the ``model`` field in API responses
# (e.g. ``"orchestrator.researcher"``). The allowed set mirrors OpenAI model
# name conventions: alphanumeric, hyphens, underscores only.
# Excluded: dots (delimiter), slashes (litellm provider/model separator),
# whitespace, and empty strings.
_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
# Agent names that match the pattern but are reserved by the platform.
# "ui" is the title-prefix sentinel the Web UI "Add agent" flow uses to mark
# user-added child sessions ("ui:<agent_name>:<user_label>"); a sub-agent
# named "ui" would collide with that scheme and be misparsed as a user-added
# row. See ``_UI_ADDED_AGENT_TITLE_PREFIX`` in
# ``omnigent.server.routes.sessions``.
_RESERVED_AGENT_NAMES = frozenset({"ui"})
_SKILL_NAME_MAX_LEN = 64
_SKILL_DESC_MAX_LEN = 1024
_VALID_INPUT_MODALITIES = {"text", "image", "audio", "video", "file"}
_VALID_OUTPUT_MODALITIES = {"text", "image", "audio"}


@dataclass
class ValidationError:
    """
    A single validation issue.

    :param path: Dot-separated location of the invalid field,
        e.g. ``"skills[0].name"`` or ``"llm.model"``.
    :param message: Human-readable description of the violation.
    """

    path: str  # dot-separated location, e.g. "skills[0].name"
    message: str


@dataclass
class ValidationResult:
    """
    Aggregated validation outcome.

    :param errors: Collected validation issues. An empty list
        means the spec is valid.
    """

    errors: list[ValidationError] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """
        Whether the spec passed all validation checks.

        :returns: ``True`` when no errors were recorded,
            ``False`` otherwise.
        """
        return len(self.errors) == 0

    def add(self, path: str, message: str) -> None:
        """
        Record a validation error.

        :param path: Dot-separated location of the invalid field,
            e.g. ``"skills[0].name"``.
        :param message: Human-readable description of the
            violation.
        """
        self.errors.append(ValidationError(path=path, message=message))


def validate(spec: AgentSpec) -> ValidationResult:
    """
    Validate an :class:`AgentSpec` against AGENTSPEC.md rules.

    :param spec: The parsed agent spec to validate.
    :returns: A :class:`ValidationResult`; check ``.valid`` to see
        if the spec passes all checks.
    """
    result = ValidationResult()
    _validate_spec_version(spec, result)
    _validate_executor_type(spec, result)
    _validate_llm(spec, result)
    _validate_interaction(spec, result)
    _validate_skills(spec, result)
    _validate_mcp_servers(spec, result)
    _validate_local_tools(spec, result)
    _validate_sub_agents(spec, result)
    _validate_compaction(spec, result)
    _validate_os_env(spec, result)
    return result


def _validate_spec_version(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate that ``spec_version`` is a supported value.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    if spec.spec_version != 1:
        result.add("spec_version", f"must be 1, got {spec.spec_version}")


# Omnigent compat: imported surgically from a dedicated module so
# the integration's tech debt is removable in one shot. See
# omnigent/spec/_omnigent_compat.py. Placed after the module's
# internal helpers (rather than with the top-of-file imports) so the
# integration's footprint is reviewable as one contiguous block.
from omnigent.spec._omnigent_compat import (  # noqa: E402
    OMNIGENT_EXECUTOR_TYPE,
    validate_omnigent_executor,
)

_VALID_EXECUTOR_TYPES = {
    "claude_sdk",
    "agents_sdk",
    OMNIGENT_EXECUTOR_TYPE,
}


def _validate_executor_type(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate that all spec fields are valid for the declared executor type.

    ``executor.type`` is the discriminator for the entire spec.
    Fields that are invalid for a given type are rejected with a
    clear error message. Delegates to per-type helpers.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    etype = spec.executor.type
    if etype not in _VALID_EXECUTOR_TYPES:
        result.add(
            "executor.type",
            f"must be one of {sorted(_VALID_EXECUTOR_TYPES)}, got {etype!r}",
        )
        return

    if etype == "claude_sdk":
        _validate_claude_sdk_executor(spec, result)
    elif etype == "agents_sdk":
        _validate_agents_sdk_executor(spec, result)
    elif etype == OMNIGENT_EXECUTOR_TYPE:
        validate_omnigent_executor(spec, result)


def _validate_claude_sdk_executor(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate fields for ``executor.type: claude_sdk``.

    The SDK manages its own compaction and connections.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    if spec.executor.connection is not None:
        result.add(
            "executor.connection",
            "not supported when executor.type is 'claude_sdk'",
        )
    if spec.compaction is not None:
        result.add(
            "compaction",
            "not supported when executor.type is 'claude_sdk'",
        )


def _validate_agents_sdk_executor(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate fields for ``executor.type: agents_sdk``.

    The SDK manages its own context window. Unlike ``claude_sdk``,
    ``llm.connection`` is allowed — the SDK
    supports custom OpenAI clients.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    if spec.compaction is not None:
        result.add(
            "compaction",
            "not supported when executor.type is 'agents_sdk' — SDK manages context internally",
        )


def _validate_llm(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate the ``llm`` block, if present.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    if spec.llm is None:
        return
    if not spec.llm.model:
        result.add("llm.model", "must be present when llm block is present")
    # Consistency check: if both spec.llm and spec.executor carry a
    # model, they must agree (the parser guarantees this, but guard
    # against future regressions).
    if spec.executor.model is not None and spec.llm.model != spec.executor.model:
        result.add(
            "executor.model",
            f"executor.model ({spec.executor.model!r}) and llm.model "
            f"({spec.llm.model!r}) disagree — use executor.model only",
        )


def _validate_interaction(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate input and output modalities against allowed values.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    modalities = spec.interaction.modalities
    for m in modalities.input:
        if m not in _VALID_INPUT_MODALITIES:
            result.add(
                "interaction.modalities.input",
                f"unsupported input modality: {m!r}",
            )
    for m in modalities.output:
        if m not in _VALID_OUTPUT_MODALITIES:
            result.add(
                "interaction.modalities.output",
                f"unsupported output modality: {m!r}",
            )


def _validate_skills(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate skill names, descriptions, and uniqueness.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    seen_names: set[str] = set()
    for i, skill in enumerate(spec.skills):
        prefix = f"skills[{i}]"
        # Name format
        if not _SKILL_NAME_PATTERN.match(skill.name):
            result.add(
                f"{prefix}.name",
                f"must match [a-z0-9-]+, got {skill.name!r}",
            )
        # Name length
        if len(skill.name) > _SKILL_NAME_MAX_LEN:
            result.add(
                f"{prefix}.name",
                f"must be at most {_SKILL_NAME_MAX_LEN} chars, got {len(skill.name)}",
            )
        # Description length
        if len(skill.description) > _SKILL_DESC_MAX_LEN:
            result.add(
                f"{prefix}.description",
                f"must be at most {_SKILL_DESC_MAX_LEN} chars, got {len(skill.description)}",
            )
        # Duplicate names
        if skill.name in seen_names:
            result.add(f"{prefix}.name", f"duplicate skill name: {skill.name!r}")
        seen_names.add(skill.name)


def _validate_mcp_servers(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate MCP server transport, required fields, and name
    uniqueness.

    Per-transport field rules:

    - ``transport == "http"``: ``url`` is required; ``command``,
      ``args``, ``env`` must not be populated.
    - ``transport == "stdio"``: ``command`` is required; ``url``
      must not be populated and ``headers`` must be empty. ``args``
      and ``env`` are optional.

    The parser rejects the wrong-transport-key combinations at
    YAML-load time; this validator runs on the fully-parsed spec
    and catches the same mistakes for programmatic construction
    (tests, translators, etc.) that bypass the parser.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    seen_names: set[str] = set()
    for i, mcp in enumerate(spec.mcp_servers):
        prefix = f"mcp_servers[{i}]"
        # Duplicate names
        if mcp.name in seen_names:
            result.add(f"{prefix}.name", f"duplicate MCP server name: {mcp.name!r}")
        seen_names.add(mcp.name)
        if mcp.transport == "http":
            if mcp.url is None:
                result.add(f"{prefix}.url", "required when transport is 'http'")
            if mcp.command is not None:
                result.add(f"{prefix}.command", "not allowed when transport is 'http'")
            if mcp.args:
                result.add(f"{prefix}.args", "not allowed when transport is 'http'")
            if mcp.env:
                result.add(f"{prefix}.env", "not allowed when transport is 'http'")
        elif mcp.transport == "stdio":
            if mcp.command is None:
                result.add(f"{prefix}.command", "required when transport is 'stdio'")
            if mcp.url is not None:
                result.add(f"{prefix}.url", "not allowed when transport is 'stdio'")
            if mcp.headers:
                result.add(f"{prefix}.headers", "not allowed when transport is 'stdio'")
        else:
            result.add(
                f"{prefix}.transport",
                f"must be 'http' or 'stdio', got {mcp.transport!r}",
            )


def _validate_local_tools(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate local tool name uniqueness across all tool sources
    (MCP servers and local tools), and reject collisions with
    reserved builtin names (POLICIES.md §15.8).

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    # Lazy import to avoid pulling the tools package during
    # spec load (the validator runs from ``omnigent.spec``,
    # which is a lower layer than ``omnigent.tools``).
    from omnigent.tools.builtins import BUILTIN_NAMES

    # Collect everything the agent declares and cross-check
    # against the reserved builtin name-space. The same set is
    # also used for duplicate-name detection across sources.
    all_tool_names: set[str] = set()
    for i, mcp in enumerate(spec.mcp_servers):
        if mcp.name in BUILTIN_NAMES:
            result.add(
                f"mcp_servers[{i}].name",
                f"tool name {mcp.name!r} collides with a reserved "
                f"builtin tool name; choose a different name",
            )
        all_tool_names.add(mcp.name)
    for i, tool in enumerate(spec.local_tools):
        if tool.name in BUILTIN_NAMES:
            result.add(
                f"local_tools[{i}].name",
                f"tool name {tool.name!r} collides with a reserved "
                f"builtin tool name; choose a different name",
            )
        if tool.name in all_tool_names:
            result.add(
                f"local_tools[{i}].name",
                f"duplicate tool name: {tool.name!r}",
            )
        all_tool_names.add(tool.name)
        # Cross-check ``runtime`` against the presence of a
        # server-side path. The two combinations the contract
        # forbids (server with no path, client with a path) both
        # surface here so authors get the same shape of error
        # whether the spec was hand-built or arrived through the
        # YAML adapter.
        if tool.runtime == ToolRuntime.SERVER and tool.path is None:
            result.add(
                f"local_tools[{i}].path",
                f"tool {tool.name!r} has runtime 'server' but no "
                f"callable path; server-runtime tools must declare "
                f"a 'callable:' (dotted Python path) in YAML",
            )
        if tool.runtime == ToolRuntime.CLIENT:
            if tool.path is not None:
                result.add(
                    f"local_tools[{i}].path",
                    f"tool {tool.name!r} has runtime 'client' but a "
                    f"callable path is set; client-runtime tools "
                    f"must NOT declare a 'callable:' — the SDK "
                    f"consumer provides the implementation at "
                    f"stream-start time",
                )
            if tool.parameters is None:
                # No callable to introspect → the YAML
                # ``parameters:`` block is the only schema source.
                # Without it the LLM would see a no-args tool and
                # the SDK consumer wouldn't know what arguments to
                # accept.
                result.add(
                    f"local_tools[{i}].parameters",
                    f"tool {tool.name!r} has runtime 'client' but no "
                    f"'parameters:' block; client-runtime tools must "
                    f"declare an explicit JSON-Schema 'parameters:' "
                    f"(no callable to introspect for one)",
                )


def _validate_sub_agents(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate sub-agent declarations.

    Each sub-agent is validated independently as if it were a
    top-level spec — its own ``executor.type`` determines which
    fields are valid. The parent only checks structural references
    (every name in ``tools.agents`` has a matching directory) and
    tree-wide uniqueness.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    sub_specs = {sa.name: sa for sa in spec.sub_agents if sa.name is not None}

    for agent_ref in spec.tools.agents:
        if agent_ref not in sub_specs:
            result.add(
                "tools.agents",
                f"references sub-agent {agent_ref!r} but no "
                f"matching directory found under agents/",
            )

    # Validate each sub-agent independently — its own executor.type
    # determines which fields are required/invalid.
    for sa in spec.sub_agents:
        sa_result = validate(sa)
        for err in sa_result.errors:
            sa_name = sa.name or "unnamed"
            result.add(
                f"sub_agents[{sa_name!r}].{err.path}",
                err.message,
            )

    # Agent name characters (dots, slashes, whitespace, empty)
    _validate_agent_names(spec, result)

    # Unique names across the entire spec tree
    _check_unique_sub_agent_names(spec, result)


def _validate_compaction(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate the compaction configuration if present.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    if spec.compaction is None:
        return
    if not (0.0 < spec.compaction.trigger_threshold <= 1.0):
        result.add(
            "compaction.trigger_threshold",
            f"must be in (0.0, 1.0], got {spec.compaction.trigger_threshold}",
        )
    if spec.compaction.recent_window < 0:
        result.add(
            "compaction.recent_window",
            f"must be non-negative, got {spec.compaction.recent_window}",
        )


# Set of sandbox backends that hard-enforce network isolation
# (and therefore can host an L7 egress proxy). Mirrors the loader's
# allow-list in ``omnigent/inner/loader.py``. ``none`` is excluded
# — it doesn't install a namespace or SBPL, so egress rules would be
# inert decoration on the policy.
_EGRESS_CAPABLE_BACKENDS = frozenset({"linux_bwrap", "darwin_seatbelt"})


def _validate_os_env(spec: AgentSpec, result: ValidationResult) -> None:
    """
    Validate the agent's ``os_env`` block, focused on sandbox combos
    that the runtime can't reject on its own without an obscure error
    (or, worse, silently degrades the policy to no-op).

    The Omnigent parser already enforces these on the YAML-loaded path,
    but ``AgentSpec`` may also be constructed programmatically —
    by tests, by the Omnigent compat shim, or by future API
    callers that build a spec without going through the YAML
    pipeline. The validator is the last common gate before the
    spec is handed to the runtime, so the same combos are checked
    here.

    Combos checked (all mirror loader + parser checks):

    - ``egress_rules`` requires a sandbox backend that hard-enforces
      network isolation (``linux_bwrap`` or ``darwin_seatbelt``).
      ``none`` would leave the rules as inert decoration on the
      policy — silently bypassing the operator's intent.
    - ``start_in_scratch`` requires an active sandbox; with
      ``sandbox.type=none`` there's no scratch tmpdir to chdir into.
    - ``start_in_scratch`` and ``fork`` are mutually exclusive —
      fork already provides a writable workspace copy.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    os_env = spec.os_env
    if os_env is None:
        return

    fork = bool(getattr(os_env, "fork", False))
    start_in_scratch = bool(getattr(os_env, "start_in_scratch", False))

    sandbox = getattr(os_env, "sandbox", None)
    sandbox_type = getattr(sandbox, "type", None) if sandbox is not None else None
    egress_rules = (
        list(getattr(sandbox, "egress_rules", None) or []) if sandbox is not None else []
    )

    if start_in_scratch and fork:
        result.add(
            "os_env.start_in_scratch",
            "os_env.start_in_scratch and os_env.fork are mutually exclusive: "
            "fork already provides a writable workspace by copying cwd",
        )

    if start_in_scratch and sandbox_type == "none":
        result.add(
            "os_env.start_in_scratch",
            "os_env.start_in_scratch requires an active sandbox; "
            "sandbox.type=none does not create a scratch tmpdir",
        )

    if egress_rules and sandbox_type not in _EGRESS_CAPABLE_BACKENDS:
        result.add(
            "os_env.sandbox.egress_rules",
            "os_env.sandbox.egress_rules requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) for hard "
            "enforcement of the network allow-list. "
            f"Got sandbox.type={sandbox_type!r}; the rules would be "
            "inert decoration on the policy and the agent would have "
            "unrestricted network access despite the YAML declaring otherwise. "
            "Fix: set os_env.sandbox.type to linux_bwrap on Linux or "
            "darwin_seatbelt on macOS; do not use sandbox.type=none with "
            "egress_rules.",
        )


def _validate_agent_names(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate that every agent name in the spec tree is a legal identifier.

    Agent names appear as components of the ``model`` field in API
    responses (e.g. ``"orchestrator.researcher"``). They must match
    ``_AGENT_NAME_PATTERN`` (``[a-zA-Z0-9_-]+``), which enforces:

    - Non-empty — empty strings have no meaningful identity.
    - No dots — reserved as the delimiter between parent and sub-agent
      in the ``model`` field (e.g. ``"root.child"``).
    - No slashes — reserved by litellm as the ``provider/model``
      separator; a slash in a name would silently mis-route LLM calls.
    - No whitespace — whitespace in a model identifier confuses most
      API clients and logging pipelines.

    Names are additionally checked against ``_RESERVED_AGENT_NAMES`` —
    syntactically legal names the platform reserves (currently ``"ui"``,
    the Web UI "Add agent" title sentinel).

    :param spec: The root agent spec to check (recursed into sub_agents).
    :param result: Accumulator for any validation errors found.
    """
    if spec.name is not None and not _AGENT_NAME_PATTERN.match(spec.name):
        result.add(
            "name",
            f"agent name {spec.name!r} must match [a-zA-Z0-9_-]+ "
            f"(no dots, slashes, whitespace, or empty strings)",
        )
    if spec.name is not None and spec.name in _RESERVED_AGENT_NAMES:
        result.add(
            "name",
            f"agent name {spec.name!r} is reserved by the platform "
            f"(reserved names: {sorted(_RESERVED_AGENT_NAMES)})",
        )
    for sa in spec.sub_agents:
        _validate_agent_names(sa, result)


def _check_unique_sub_agent_names(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate that sub-agent names are unique across the entire
    spec tree (not just within one level).

    Flat uniqueness enables O(1) lookup by name during spec
    loading — see designs/SUBAGENT.md.

    :param spec: The root agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    seen: set[str] = set()
    _collect_sub_agent_names(spec, seen, result)


def _collect_sub_agent_names(
    spec: AgentSpec,
    seen: set[str],
    result: ValidationResult,
) -> None:
    """
    Recursively collect sub-agent names and flag duplicates.

    :param spec: The current spec node to check.
    :param seen: Accumulator of names seen so far.
    :param result: Accumulator for any validation errors found.
    """
    for sa in spec.sub_agents:
        if sa.name is not None:
            if sa.name in seen:
                result.add(
                    f"sub_agents[{sa.name!r}]",
                    f"duplicate sub-agent name {sa.name!r} across the spec tree",
                )
            seen.add(sa.name)
        _collect_sub_agent_names(sa, seen, result)
