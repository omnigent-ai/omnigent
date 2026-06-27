"""Parse an agent image directory into an AgentSpec."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import (
    DEFAULT_BASIC_USERNAME,
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
    TerminalEnvSpec,
)
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    AgentSpec,
    ApiKeyAuth,
    BuiltinToolConfig,
    CompactionConfig,
    DatabricksAuth,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    InteractionConfig,
    LabelDef,
    LLMConfig,
    LocalToolInfo,
    MCPServerConfig,
    ModalityConfig,
    Phase,
    PhaseSelector,
    PolicyAction,
    PolicySpec,
    ProviderAuth,
    RetryPolicy,
    SandboxConfig,
    SharePolicy,
    SkillSpec,
    ToolsConfig,
)

_log = logging.getLogger(__name__)

# Context files scanned in priority order when ``instructions:`` is absent.
# First file found wins (no merge).
_CONTEXT_FILE_PRIORITY: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".cursorrules")

# Pattern for SKILL.md YAML frontmatter delimited by ---
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


class _ConfigYamlLoader(yaml.SafeLoader):
    """
    SafeLoader variant that does NOT treat ``on``/``off``/
    ``yes``/``no`` as booleans.

    Default PyYAML resolves these per the YAML 1.1 spec — a
    trap for our spec because the policy system uses
    ``on:`` as the selector field (see POLICIES.md §3.3
    implementation notes). Without this override, an author
    writing ``on: [request]`` would get a dict keyed by ``True``
    instead of ``"on"``. We scope the override to a dedicated
    loader class so the rest of the YAML 1.1 type inference
    stays intact.

    YAML 1.2 drops these bool aliases entirely; this override
    makes our loader YAML-1.2-aligned for the narrow set of
    aliases that matter here.
    """


# Replace the YAML 1.1 bool resolver pattern with a YAML 1.2
# pattern that accepts only ``true`` / ``false`` (and their
# title/upper-case variants). Strip the old bool resolvers
# first, then add back the narrowed one.
_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAML_1_2_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")

# ``executor.config`` keys kept as their nested YAML structure instead of
# string-coerced — their consumers read the nested mapping/list shape.
_STRUCTURED_EXECUTOR_CONFIG_KEYS = frozenset({"cost_optimize"})
for _ch in list(_ConfigYamlLoader.yaml_implicit_resolvers.keys()):
    _ConfigYamlLoader.yaml_implicit_resolvers[_ch] = [
        (tag, regexp)
        for tag, regexp in _ConfigYamlLoader.yaml_implicit_resolvers[_ch]
        if tag != _BOOL_TAG
    ]
# Re-register a narrowed bool resolver keyed on ``t`` / ``T`` /
# ``f`` / ``F`` only (the YAML 1.1 aliases keyed on o/O/y/Y/n/N
# are now gone, so those characters parse as plain strings).
# mypy flags BaseResolver.add_implicit_resolver as untyped
# (PyYAML lacks type stubs on this classmethod); the call
# is the only way to register an implicit resolver, so the
# ignore is narrowly scoped to this YAML-1.2 compatibility
# override.
_ConfigYamlLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    _BOOL_TAG,
    _YAML_1_2_BOOL_RE,
    list("tTfF"),
)


def parse(root: Path, *, expand_env: bool = True) -> AgentSpec:
    """
    Parse an agent image directory into an :class:`AgentSpec`.

    :param root: Path to the agent image directory. Must contain
        ``config.yaml``.
    :param expand_env: Whether to expand ``${VAR}`` references in
        connection blocks and MCP headers. ``True`` (default) for
        deploy/runtime — raises on unresolved vars. ``False`` for
        scaffolding/validation where env vars may not yet be set.
    :returns: A fully populated :class:`AgentSpec` (not yet
        validated).
    :raises OmnigentError: If ``config.yaml`` is not valid YAML,
        has structural issues, or (when *expand_env* is ``True``)
        contains unresolved env vars.
    :raises FileNotFoundError: If ``config.yaml`` is missing.
    """
    config_path = root / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found in {root}")

    raw = yaml.load(config_path.read_text(), Loader=_ConfigYamlLoader)
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"config.yaml must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )

    spec_version = raw.get("spec_version")
    if spec_version is None:
        raise OmnigentError(
            "config.yaml missing required field: spec_version",
            code=ErrorCode.INVALID_INPUT,
        )

    raw_executor = raw.get("executor")
    raw_llm = raw.get("llm")
    raw_tools = raw.get("tools")
    llm = _parse_llm(raw_llm, expand_env=expand_env)
    interaction = _parse_interaction(raw.get("interaction"))
    tools_config = _parse_tools_config(raw_tools)
    executor = _parse_executor(raw_executor, expand_env=expand_env)
    # ── Consolidate llm: → executor ────────────────────────────────
    # ``executor.model`` and ``executor.connection`` are the primary
    # source of truth. When the deprecated ``llm:`` block provides
    # values that the ``executor:`` block doesn't, lift them into
    # executor so all downstream code reads from one place.
    # ``spec.llm`` is still populated (for internal consumers that
    # need extra/retry/request_timeout) but model and connection
    # are authoritative on executor.
    if llm is not None:
        if executor.model is None:
            executor.model = llm.model
        if executor.connection is None and llm.connection is not None:
            executor.connection = llm.connection
    # Ensure spec.llm is populated from executor fields when only the
    # executor: block declares model/connection (the common case for
    # user-authored YAML). Internal consumers (policy builder,
    # web_fetch sub-agent) still read spec.llm for extra, retry,
    # and request_timeout.
    if llm is None and executor.model is not None:
        llm = LLMConfig(model=executor.model, connection=executor.connection)
    elif llm is not None:
        # Keep llm.model and llm.connection in sync with executor
        # (executor is authoritative after the lift above).
        llm = LLMConfig(
            model=executor.model or llm.model,
            extra=llm.extra,
            connection=executor.connection,
            request_timeout=llm.request_timeout,
            retry=llm.retry,
        )
    compaction = _parse_compaction(raw.get("compaction"))
    guardrails = _parse_guardrails(raw.get("guardrails"), expand_env=expand_env)
    os_env = _parse_os_env(raw.get("os_env"))
    terminals = _parse_terminals(raw.get("terminals"))
    params = raw.get("params", {})
    # Top-level ``async:`` flag gates the LLM-callable async-dispatch
    # builtins (``sys_call_async``, ``sys_read_inbox``,
    # ``sys_cancel_async``). Defaults to True to match
    # ``omnigent/inner/datamodel.py::AgentDef.async_enabled`` — the
    # same YAML must produce the same tool surface under Omnigent mode and
    # the legacy inner stack. Agents that want to suppress the surface
    # declare ``async: false`` explicitly. ``bool()`` accepts YAML
    # truthy/falsy values (``true`` / ``True`` / ``yes`` /
    # ``false`` / ``no``) consistently.
    async_enabled = bool(raw.get("async", True))
    # Top-level ``timers:`` flag gates the LLM-callable timer
    # builtins (``sys_timer_set``, ``sys_timer_cancel``).
    # Defaults to False to match
    # ``omnigent/inner/datamodel.py::AgentDef.timers`` — agents
    # opt into the timer surface explicitly. See step 10 of the
    # harness contract migration.
    timers = bool(raw.get("timers", False))
    # Top-level ``spawn:`` flag grants spawning OUTSIDE any declared
    # sub-agent list: ``sys_session_create`` (existing agents by id,
    # or custom bundles via config_path) plus send/close to drive the
    # children. Distinct from ``tools.agents``, which permits only
    # the specified sub-agent types. Defaults to False — session
    # reads stay always-on, but every write grant is explicit.
    spawn = bool(raw.get("spawn", False))
    # Top-level ``agent_session_sharing:`` flag is the SOLE enabler of
    # the ``sys_session_share`` tool, independent of ``spawn`` /
    # ``tools.agents`` (and unrelated to server-API / CLI sharing).
    # ``none`` (default) leaves it unregistered; ``non-public`` allows
    # granting named users; ``public`` also allows ``__public__``
    # anonymous read.
    agent_session_sharing = _parse_share_policy(raw.get("agent_session_sharing"))

    # Honor ``prompt:`` as the legacy alias for ``instructions:`` (per
    # ``_OMNIGENT_SYSTEM_PROMPT_KEYS``); ``instructions:`` wins if both set.
    raw_instructions = raw.get("instructions")
    if raw_instructions is None:
        raw_instructions = raw.get("prompt")
    instructions = _resolve_instructions(root, raw_instructions)
    skills = _discover_skills(root / "skills")
    skills_filter = _parse_skills_filter(raw.get("skills"))
    mcp_servers = _discover_mcp_servers(root / "tools" / "mcp", expand_env=expand_env)
    mcp_servers = mcp_servers + _parse_inline_mcp_servers(raw_tools, expand_env=expand_env)
    local_tools = _discover_local_tools(root / "tools")
    sub_agents = _discover_sub_agents(root / "agents", expand_env=expand_env)

    return AgentSpec(
        spec_version=spec_version,
        name=raw.get("name"),
        description=raw.get("description"),
        llm=llm,
        interaction=interaction,
        tools=tools_config,
        executor=executor,
        compaction=compaction,
        guardrails=guardrails,
        params=params,
        instructions=instructions,
        skills=skills,
        skills_filter=skills_filter,
        mcp_servers=mcp_servers,
        local_tools=local_tools,
        sub_agents=sub_agents,
        async_enabled=async_enabled,
        os_env=os_env,
        terminals=terminals,
        timers=timers,
        spawn=spawn,
        agent_session_sharing=agent_session_sharing,
    )


def _parse_llm(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> LLMConfig | None:
    """
    Parse the ``llm:`` block from config.yaml into an
    :class:`LLMConfig`.

    :param raw: The raw ``llm:`` mapping from config.yaml, or
        ``None`` if the block was absent. Example:
        ``{"model": "openai/gpt-4o", "temperature": 0.7}``.
    :param expand_env: Whether to expand ``${VAR}`` references in
        the connection block. ``False`` keeps literals as-is.
    :returns: A populated :class:`LLMConfig`, or ``None`` when
        the ``llm:`` block is absent.
    :raises OmnigentError: If the ``llm:`` block is present but
        missing the required ``model`` field.
    """
    if raw is None:
        return None
    model = raw.get("model")
    if model is None:
        raise OmnigentError(
            "llm block present but missing required field: model",
            code=ErrorCode.INVALID_INPUT,
        )
    # ``connection``, ``profile``, ``request_timeout``, and ``retry``
    # are separated into their own typed fields; everything else is
    # passed through to the LLM SDK as extra kwargs.
    connection_raw = raw.get("connection")
    connection: dict[str, str] | None = None
    if isinstance(connection_raw, dict):
        raw_dict = {str(k): str(v) for k, v in connection_raw.items()}
        # Expand ${VAR} references so api_key: ${OPENAI_API_KEY} works.
        # Skipped when expand_env is False (scaffolding/validation).
        connection = expand_env_vars(raw_dict) if expand_env else raw_dict
    profile_raw = raw.get("profile")
    profile = str(profile_raw) if profile_raw is not None else None
    request_timeout = int(raw["request_timeout"]) if "request_timeout" in raw else 300
    retry = _parse_retry(raw.get("retry"))
    reserved = {"model", "connection", "profile", "request_timeout", "retry"}
    extra = {k: v for k, v in raw.items() if k not in reserved}
    return LLMConfig(
        model=str(model),
        extra=extra,
        connection=connection,
        profile=profile,
        request_timeout=request_timeout,
        retry=retry,
    )


def _parse_interaction(
    raw: dict[str, Any] | None,
) -> InteractionConfig:
    """
    Parse the ``interaction:`` block from config.yaml into an
    :class:`InteractionConfig`.

    :param raw: The raw ``interaction:`` mapping from config.yaml,
        or ``None`` if the block was absent. Example:
        ``{"conversational": false, "modalities": {"input":
        ["text", "image"]}}``.
    :returns: A populated :class:`InteractionConfig`. Returns
        defaults when *raw* is ``None``.
    """
    if raw is None:
        return InteractionConfig()
    modalities_raw = raw.get("modalities")
    if not isinstance(modalities_raw, dict):
        modalities = ModalityConfig()
    else:
        modalities = ModalityConfig(
            input=modalities_raw.get("input", ["text"]),
            output=modalities_raw.get("output", ["text"]),
        )
    conversational = raw.get("conversational", True)
    return InteractionConfig(
        conversational=bool(conversational),
        modalities=modalities,
    )


def _parse_tools_config(
    raw: dict[str, Any] | None,
) -> ToolsConfig:
    """
    Parse the ``tools:`` block from config.yaml into a
    :class:`ToolsConfig`.

    :param raw: The raw ``tools:`` mapping from config.yaml, or
        ``None`` if the block was absent. Example:
        ``{"agents": ["summarizer", "code-reviewer"],
        "timeout": 60}``.
    :returns: A populated :class:`ToolsConfig`. Returns defaults
        when *raw* is ``None``.
    """
    if raw is None:
        return ToolsConfig()
    timeout = int(raw["timeout"]) if "timeout" in raw else 60
    retry = _parse_retry(raw.get("retry"))
    builtins = _parse_builtin_tools(raw.get("builtins", []))
    sandbox = _parse_sandbox_config(raw.get("sandbox"))
    return ToolsConfig(
        agents=raw.get("agents", []),
        builtins=builtins,
        timeout=timeout,
        retry=retry,
        sandbox=sandbox,
    )


def _parse_sandbox_config(
    raw: dict[str, Any] | None,
) -> SandboxConfig:
    """
    Parse the ``tools.sandbox`` block from config.yaml.

    Accepted settings: ``container_image`` (preferred),
    ``docker_image`` (deprecated alias), and
    ``container_runtime``. Whether sandboxing is enabled is a
    runtime decision, not an agent config decision::

        sandbox:
          container_image: python:3.12-slim
          container_runtime: podman  # optional, defaults to docker

    :param raw: The raw ``sandbox`` value from the ``tools``
        block. ``None`` means not specified (use defaults).
    :returns: A :class:`SandboxConfig`.
    """
    if raw is None or not isinstance(raw, dict):
        return SandboxConfig()
    runtime = raw.get("container_runtime", "docker")
    if runtime not in ("docker", "podman"):
        raise ValueError(
            f"Unsupported container_runtime {runtime!r}; expected 'docker' or 'podman'."
        )
    image = raw.get("container_image") or raw.get("docker_image")
    return SandboxConfig(
        container_image=image,
        container_runtime=runtime,
    )


def _parse_builtin_tools(
    raw: list[str | dict[str, Any]],
) -> list[BuiltinToolConfig]:
    """
    Parse the ``tools.builtins`` list into
    :class:`BuiltinToolConfig` objects.

    Each entry is either a plain string (tool name with no config)
    or a dict with a ``name`` key and tool-specific config fields::

        builtins:
          - web_search
          - name: web_search
            api_key: ${GOOGLE_SEARCH_API_KEY}
            engine_id: ${GOOGLE_SEARCH_ENGINE_ID}

    :param raw: The raw ``builtins`` list from config.yaml.
    :returns: A list of :class:`BuiltinToolConfig` instances.
    :raises OmnigentError: If a dict entry is missing ``name``.
    """
    result: list[BuiltinToolConfig] = []
    for entry in raw:
        if isinstance(entry, str):
            result.append(BuiltinToolConfig(name=entry))
        elif isinstance(entry, dict):
            name = entry.get("name")
            if not name:
                raise OmnigentError(
                    "Each dict entry in tools.builtins must have a 'name' field.",
                    code=ErrorCode.INVALID_INPUT,
                )
            # Everything except 'name' is tool-specific config.
            config = {str(k): str(v) for k, v in entry.items() if k != "name"}
            result.append(
                BuiltinToolConfig(
                    name=str(name),
                    config=config,
                )
            )
        else:
            raise OmnigentError(
                f"tools.builtins entries must be strings or dicts, got {type(entry).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
    return result


def _parse_retry(
    raw: dict[str, Any] | None,
) -> RetryPolicy:
    """
    Parse a ``retry:`` block into a :class:`RetryPolicy`.

    Returns defaults when *raw* is ``None`` or empty.

    :param raw: The raw ``retry:`` mapping, or ``None`` if absent.
        Example: ``{"max_attempts": 5, "status_codes": [429, 502]}``.
    :returns: A populated :class:`RetryPolicy`.
    """
    if not raw:
        return RetryPolicy()
    defaults = RetryPolicy()
    return RetryPolicy(
        max_retries=int(raw.get("max_retries", defaults.max_retries)),
        backoff_base_s=float(raw.get("backoff_base_s", defaults.backoff_base_s)),
        backoff_max_s=float(raw.get("backoff_max_s", defaults.backoff_max_s)),
        jitter=bool(raw.get("jitter", defaults.jitter)),
        timeout_per_request_s=(
            float(raw["timeout_per_request_s"])
            if raw.get("timeout_per_request_s") is not None
            else defaults.timeout_per_request_s
        ),
        retryable_status_codes=tuple(
            int(c) for c in raw.get("retryable_status_codes", defaults.retryable_status_codes)
        ),
    )


def _parse_executor(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> ExecutorSpec:
    """
    Parse the ``executor:`` block into an :class:`ExecutorSpec`.

    Returns defaults (``type="omnigent"``) when *raw* is ``None``.

    Lifts a top-level ``executor.profile`` into the concrete
    :attr:`ExecutorSpec.profile` field for ALL executor types. For
    ``type == "omnigent"`` ALSO mirrors that value into
    ``config["profile"]`` (back-compat — the omnigent executor
    reads ``config["profile"]`` today; will be migrated when the
    omnigent-compat sunset lands).

    :param raw: The raw ``executor:`` mapping, or ``None`` if
        absent. Example: ``{"type": "omnigent"}``.
    :returns: A populated :class:`ExecutorSpec`.
    """
    if raw is None:
        return ExecutorSpec()
    etype = str(raw.get("type", "omnigent"))
    # ``config`` is a free-form dict[str, Any] owned by each executor
    # type. Scalar values are coerced to strings so YAML booleans /
    # numbers round-trip as their string form (the omnigent
    # harness/profile fields are both strings in the source YAML).
    # Structured keys whose consumer needs the nested shape are kept
    # verbatim: ``cost_optimize`` is the cost advisor's tier config (a
    # nested mapping), which ``parse_advisor_config`` reads as a Mapping.
    raw_config = raw.get("config")
    config: dict[str, Any] = {}
    if isinstance(raw_config, dict):
        config = {
            str(k): (v if k in _STRUCTURED_EXECUTOR_CONFIG_KEYS else str(v))
            for k, v in raw_config.items()
        }
    # Top-level ``executor.profile`` populates the concrete
    # ``ExecutorSpec.profile`` field for every executor type. For
    # ``omnigent`` we ALSO mirror it into ``config["profile"]``
    # so the existing omnigent executor (which still reads from
    # ``config["profile"]``) keeps working until it is migrated.
    profile_raw = raw.get("profile")
    profile: str | None = None
    if profile_raw is not None:
        profile = str(profile_raw)
    if etype == "omnigent" and profile is not None and "profile" not in config:
        config["profile"] = profile
    raw_cw = raw.get("context_window")
    context_window: int | None = int(raw_cw) if raw_cw is not None else None
    raw_model = raw.get("model")
    model: str | None = str(raw_model) if raw_model is not None else None
    # Parse ``executor.connection:`` — same shape as ``llm.connection:``
    # (a flat dict of string key-value pairs with optional ${VAR}
    # expansion). Lifted from the ``executor:`` block so connection
    # config lives alongside the harness and model it belongs to.
    connection_raw = raw.get("connection")
    connection: dict[str, str] | None = None
    if isinstance(connection_raw, dict):
        raw_dict = {str(k): str(v) for k, v in connection_raw.items()}
        connection = expand_env_vars(raw_dict) if expand_env else raw_dict
    auth = _parse_executor_auth(raw, expand_env=expand_env)
    return ExecutorSpec(
        type=etype,
        timeout=int(raw.get("timeout", 3600)),
        max_iterations=int(raw.get("max_iterations", 1000)),
        profile=profile,
        config=config,
        model=model,
        connection=connection,
        context_window=context_window,
        auth=auth,
    )


def _parse_executor_auth(
    raw: dict[str, Any],  # type: ignore[explicit-any]
    *,
    expand_env: bool = True,
) -> ApiKeyAuth | DatabricksAuth | ProviderAuth | None:
    """
    Parse the ``executor.auth:`` block into a typed auth dataclass.

    Returns ``None`` when the ``auth:`` key is absent from the executor
    block (the harness will fall back to env-var / profile defaults).

    Supported types:

    - ``type: api_key`` — requires ``api_key``.  Env-var references
      (e.g. ``$OPENAI_API_KEY``) are expanded when *expand_env* is
      ``True``.
    - ``type: databricks`` — requires ``profile``.
    - ``type: provider`` — requires ``name`` (a provider declared in
      the ``providers:`` block of ``~/.omnigent/config.yaml``).

    :param raw: The raw ``executor:`` mapping already read from YAML.
        Example: ``{"harness": "openai-agents", "auth": {"type": "api_key",
        "api_key": "$OPENAI_API_KEY"}}``.
    :param expand_env: Whether to expand ``${VAR}`` / ``$VAR`` references
        in the ``api_key`` value. ``True`` for runtime; ``False`` for
        scaffolding / validation where env vars may not be set yet.
    :returns: A populated :class:`ApiKeyAuth`, :class:`DatabricksAuth`,
        or :class:`ProviderAuth`, or ``None`` when ``auth:`` is absent.
    :raises OmnigentError: If the ``auth:`` block is present but
        malformed (unknown type, missing required field).
    """
    raw_auth = raw.get("auth")
    if raw_auth is None:
        return None
    if not isinstance(raw_auth, dict):
        raise OmnigentError(
            "executor.auth must be a mapping, e.g. {type: databricks, profile: oss}",
            code=ErrorCode.INVALID_INPUT,
        )
    auth_type = str(raw_auth.get("type", ""))
    if auth_type == "api_key":
        raw_key = str(raw_auth.get("api_key") or "")
        if not raw_key:
            raise OmnigentError(
                "executor.auth.api_key is required when type is 'api_key'",
                code=ErrorCode.INVALID_INPUT,
            )
        api_key = expand_env_vars({"api_key": raw_key})["api_key"] if expand_env else raw_key
        raw_base_url = raw_auth.get("base_url")
        base_url: str | None = None
        if raw_base_url is not None:
            raw_base_url_str = str(raw_base_url)
            base_url = (
                expand_env_vars({"base_url": raw_base_url_str})["base_url"]
                if expand_env
                else raw_base_url_str
            )
        return ApiKeyAuth(api_key=api_key, base_url=base_url)
    if auth_type == "databricks":
        profile_val = str(raw_auth.get("profile") or "")
        if not profile_val:
            raise OmnigentError(
                "executor.auth.profile is required when type is 'databricks'",
                code=ErrorCode.INVALID_INPUT,
            )
        return DatabricksAuth(profile=profile_val)
    if auth_type == "provider":
        name_val = str(raw_auth.get("name") or "")
        if not name_val:
            raise OmnigentError(
                "executor.auth.name is required when type is 'provider'",
                code=ErrorCode.INVALID_INPUT,
            )
        return ProviderAuth(name=name_val)
    raise OmnigentError(
        f"executor.auth.type must be 'api_key', 'databricks', or 'provider', got {auth_type!r}",
        code=ErrorCode.INVALID_INPUT,
    )


def _parse_os_env(
    raw: object,
) -> OSEnvSpec | None:
    """
    Parse the top-level ``os_env:`` block into an :class:`OSEnvSpec`.

    Native Omnigent YAML mirrors the omnigent YAML shape so users
    moving from one to the other don't have to relearn the
    config surface — a top-level ``os_env:`` mapping with
    ``type``, ``cwd``, ``sandbox: {...}``, ``fork``, and
    ``start_in_scratch`` keys. See
    :class:`omnigent.inner.datamodel.OSEnvSpec` for the
    semantics of each field.

    :param raw: The raw ``os_env:`` value from config.yaml.
        Either a mapping (parsed) or absent (``None``).
        Example: ``{"type": "caller_process", "cwd": ".",
        "sandbox": {"type": "linux_bwrap",
        "write_paths": ["."], "allow_network": False}}``.
    :returns: A populated :class:`OSEnvSpec` when the block is
        present, ``None`` when absent.
    :raises OmnigentError: If *raw* is not a mapping, or
        ``start_in_scratch`` is set together with ``fork`` (those
        knobs both manage the agent's writable workspace and would
        fight each other), or ``start_in_scratch`` is set on a
        spec whose ``sandbox.type`` is ``"none"`` (no scratch
        tmpdir is created in that case so there is nothing to
        chdir into).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"os_env must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    sandbox = _parse_os_env_sandbox(raw.get("sandbox"))
    cwd_raw = raw.get("cwd")
    fork = bool(raw.get("fork", False))
    start_in_scratch = bool(raw.get("start_in_scratch", False))
    if start_in_scratch and fork:
        raise OmnigentError(
            "os_env.start_in_scratch and os_env.fork are mutually exclusive: "
            "fork already provides a writable workspace by copying cwd",
            code=ErrorCode.INVALID_INPUT,
        )
    if start_in_scratch and sandbox is not None and sandbox.type == "none":
        raise OmnigentError(
            "os_env.start_in_scratch requires an active sandbox; "
            "sandbox.type=none does not create a scratch tmpdir",
            code=ErrorCode.INVALID_INPUT,
        )
    return OSEnvSpec(
        type=str(raw.get("type", "caller_process")),
        cwd=str(cwd_raw) if cwd_raw is not None else None,
        sandbox=sandbox,
        fork=fork,
        start_in_scratch=start_in_scratch,
    )


def _parse_terminals(
    raw: object,
) -> dict[str, TerminalEnvSpec] | None:
    """
    Parse the top-level ``terminals:`` block into a map of
    :class:`TerminalEnvSpec`.

    Native Omnigent YAML mirrors the omnigent-compat ``terminals:`` shape — a
    mapping of ``terminal_name`` → ``{command, args, env, os_env,
    allow_cwd_override, allow_sandbox_override, scrollback, ...}`` — so a
    bundle agent registers the ``sys_terminal_*`` toolkit exactly like a
    compat agent. Closes the native-YAML gap left as additive follow-up in
    ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §3 (``_parse_terminals`` parallel to
    ``_parse_os_env``).

    :param raw: The raw ``terminals:`` value from config.yaml — a mapping of
        terminal name → config, or absent (``None``). Example:
        ``{"claude_code": {"command": "isaac", "allow_cwd_override": True,
        "os_env": {"type": "caller_process", "sandbox": {"type": "none"}}}}``.
    :returns: Map of terminal name → :class:`TerminalEnvSpec` when present and
        non-empty, else ``None`` (so ``sys_terminal_*`` stays unregistered).
    :raises OmnigentError: If ``terminals`` (or any entry) is not a mapping,
        or an entry's ``args`` / ``env`` are the wrong type.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"terminals must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    terminals: dict[str, TerminalEnvSpec] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise OmnigentError(
                f"terminals.{name} must be a YAML mapping, got {type(entry).__name__}",
                code=ErrorCode.INVALID_INPUT,
            )
        args_raw = entry.get("args") or []
        env_raw = entry.get("env") or {}
        if not isinstance(args_raw, list):
            raise OmnigentError(
                f"terminals.{name}.args must be a list", code=ErrorCode.INVALID_INPUT
            )
        if not isinstance(env_raw, dict):
            raise OmnigentError(
                f"terminals.{name}.env must be a mapping", code=ErrorCode.INVALID_INPUT
            )
        # os_env may be a nested mapping (parsed like top-level os_env), the
        # literal string "inherit", or absent.
        raw_os_env = entry.get("os_env")
        os_env = raw_os_env if isinstance(raw_os_env, str) else _parse_os_env(raw_os_env)
        terminals[name] = TerminalEnvSpec(
            command=entry.get("command"),
            args=[str(a) for a in args_raw],
            env={str(k): str(v) for k, v in env_raw.items()},
            os_env=os_env,
            allow_cwd_override=bool(entry.get("allow_cwd_override", False)),
            allow_sandbox_override=bool(entry.get("allow_sandbox_override", False)),
            log_file=entry.get("log_file"),
            scrollback=int(entry.get("scrollback", 10000)),
            session_prefix=str(entry.get("session_prefix", "omni_")),
            tmux_allow_passthrough=bool(entry.get("tmux_allow_passthrough", False)),
            tmux_start_on_attach=bool(entry.get("tmux_start_on_attach", False)),
        )
    return terminals or None


def _parse_os_env_sandbox(
    raw: object,
) -> OSEnvSandboxSpec | None:
    """
    Parse the ``os_env.sandbox:`` block into an
    :class:`OSEnvSandboxSpec`.

    :param raw: The raw ``sandbox:`` value from the
        ``os_env:`` mapping. Either a mapping (parsed) or
        absent (``None``). Example:
        ``{"type": "linux_bwrap", "read_paths": ["/usr"],
        "write_paths": ["."], "write_files":
        ["/home/me/.claude.json"], "cwd_allow_hidden": [".venv",
        ".git"], "cwd_hidden_scan_max_entries": 100000,
        "cwd_hidden_scan_overflow": "warn",
        "env_passthrough": ["AWS_PROFILE", "GITHUB_TOKEN"],
        "allow_network": False}``.
    :returns: A populated :class:`OSEnvSandboxSpec` when the
        block is present, ``None`` when absent.
    :raises OmnigentError: If *raw* is not a mapping, or
        ``cwd_allow_hidden`` is not a list of strings, or any
        ``cwd_allow_hidden`` entry contains a path separator, or
        ``cwd_hidden_scan_max_entries`` is not a positive integer,
        or ``cwd_hidden_scan_overflow`` is not one of ``"error"``,
        ``"warn"``, ``"unlimited"``, or ``env_passthrough`` is not
        a list of POSIX environment variable names.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"os_env.sandbox must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    read_paths_raw = raw.get("read_paths")
    write_paths_raw = raw.get("write_paths")
    write_files_raw = raw.get("write_files")
    cwd_allow_hidden = _parse_cwd_allow_hidden(raw.get("cwd_allow_hidden"))
    max_entries = _parse_cwd_hidden_scan_max_entries(raw.get("cwd_hidden_scan_max_entries"))
    overflow = _parse_cwd_hidden_scan_overflow(raw.get("cwd_hidden_scan_overflow"))
    env_passthrough = _parse_env_passthrough(raw.get("env_passthrough"))
    egress_rules = _parse_egress_rules(raw.get("egress_rules"))
    raw_type = raw.get("type")
    if raw_type is None:
        # No ``type:`` field in the sandbox block -- resolve via the
        # platform default (the same logic that fires when ``sandbox:``
        # is omitted entirely). On Linux this picks ``linux_bwrap``
        # when bwrap is on PATH, else ``none``; on macOS it
        # picks ``darwin_seatbelt``.
        from omnigent.inner.sandbox import _default_sandbox_for_platform

        sandbox_type = _default_sandbox_for_platform().type
    else:
        sandbox_type = str(raw_type)
    if egress_rules and sandbox_type not in ("linux_bwrap", "darwin_seatbelt"):
        raise OmnigentError(
            "os_env.sandbox.egress_rules requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) for hard "
            "network enforcement: those backends restrict network access "
            "at spawn time so the MITM proxy is the only egress path. "
            f"Got sandbox.type={sandbox_type!r}. "
            "Fix: set os_env.sandbox.type to linux_bwrap on Linux or "
            "darwin_seatbelt on macOS; do not use sandbox.type=none with "
            "egress_rules.",
            code=ErrorCode.INVALID_INPUT,
        )
    credential_proxy = _parse_credential_proxy(raw.get("credential_proxy"))
    if credential_proxy is not None and sandbox_type not in ("linux_bwrap", "darwin_seatbelt"):
        raise OmnigentError(
            "os_env.sandbox.credential_proxy requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) so credentials are "
            "bound to a hardened helper boundary. "
            f"Got sandbox.type={sandbox_type!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    if credential_proxy is not None and not egress_rules:
        raise OmnigentError(
            "os_env.sandbox.credential_proxy requires os_env.sandbox.egress_rules: "
            "the MITM egress proxy is what swaps the synthetic placeholder for the "
            "real credential and rejects placeholder leaks, so it must be active.",
            code=ErrorCode.INVALID_INPUT,
        )
    macos_reason = _credential_proxy_macos_unsupported_reason(credential_proxy, sandbox_type)
    if macos_reason is not None:
        raise OmnigentError(macos_reason, code=ErrorCode.INVALID_INPUT)
    allow_private = raw.get("egress_allow_private_destinations", False)
    if not isinstance(allow_private, bool):
        raise OmnigentError(
            "os_env.sandbox.egress_allow_private_destinations must be a "
            f"boolean, got {type(allow_private).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return OSEnvSandboxSpec(
        type=sandbox_type,
        read_paths=[str(p) for p in read_paths_raw] if read_paths_raw is not None else None,
        write_paths=[str(p) for p in write_paths_raw] if write_paths_raw is not None else None,
        write_files=[str(p) for p in write_files_raw] if write_files_raw is not None else None,
        allow_network=bool(raw.get("allow_network", True)),
        cwd_allow_hidden=cwd_allow_hidden,
        cwd_hidden_scan_max_entries=max_entries,
        cwd_hidden_scan_overflow=overflow,
        env_passthrough=env_passthrough,
        egress_rules=egress_rules,
        egress_allow_private_destinations=allow_private,
        credential_proxy=credential_proxy,
    )


def _parse_cwd_allow_hidden(raw: object) -> list[str] | None:
    """
    Parse and validate the ``cwd_allow_hidden:`` field of
    ``os_env.sandbox``.

    Each entry must be a single path component (no ``/``, ``\\``,
    or ``.`` / ``..`` traversal) so a misconfigured spec can't punch
    a hole through arbitrary subdirectories of cwd. The bwrap backend
    looks each entry up in ``cwd.iterdir()`` directly; sanitising
    here keeps the resolver simple and the failure mode loud at
    parse time rather than at runtime.

    :param raw: Raw value from the YAML, e.g. ``[".venv", ".git"]``,
        or ``None`` when the field is absent.
    :returns: List of validated component names, or ``None`` when
        ``raw`` is ``None`` (the resolver will then apply the
        backend's documented default).
    :raises OmnigentError: If ``raw`` isn't a list, contains a
        non-string entry, or contains an entry with a path separator
        or traversal component.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.cwd_allow_hidden must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    sanitized: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise OmnigentError(
                "os_env.sandbox.cwd_allow_hidden entries must be strings, "
                f"got {type(entry).__name__}: {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if not entry:
            raise OmnigentError(
                "os_env.sandbox.cwd_allow_hidden entries must not be empty strings",
                code=ErrorCode.INVALID_INPUT,
            )
        if "/" in entry or "\\" in entry or entry in (".", ".."):
            raise OmnigentError(
                "os_env.sandbox.cwd_allow_hidden entries must be single path "
                f"components (no separators or '.'/'..'): {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        sanitized.append(entry)
    return sanitized


_CWD_HIDDEN_SCAN_OVERFLOW_MODES = ("error", "warn", "unlimited")


def _parse_cwd_hidden_scan_max_entries(raw: object) -> int:
    """
    Parse ``os_env.sandbox.cwd_hidden_scan_max_entries``.

    Falls back to the dataclass default (50000) when the field is
    absent. Rejects non-integers and non-positive values at parse
    time so a misconfiguration surfaces immediately rather than at
    spawn time.

    YAML readers occasionally hand us ``True`` / ``False`` for
    fields the author meant as numbers; the explicit ``bool``
    rejection below catches that.

    :param raw: Raw value from the YAML, e.g. ``100000`` or ``None``.
    :returns: Validated positive integer, or the dataclass default
        when ``raw`` is ``None``.
    :raises OmnigentError: If ``raw`` is not an int or is not
        strictly positive.
    """
    if raw is None:
        return OSEnvSandboxSpec.__dataclass_fields__["cwd_hidden_scan_max_entries"].default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise OmnigentError(
            "os_env.sandbox.cwd_hidden_scan_max_entries must be an integer, "
            f"got {type(raw).__name__}: {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    if raw <= 0:
        raise OmnigentError(
            f"os_env.sandbox.cwd_hidden_scan_max_entries must be > 0, got {raw}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


def _parse_cwd_hidden_scan_overflow(raw: object) -> str:
    """
    Parse ``os_env.sandbox.cwd_hidden_scan_overflow``.

    Falls back to the dataclass default (``"warn"``) when the field
    is absent — a partial best-effort mask plus a ``CRITICAL`` log
    line, which beats blocking every spawn on workspaces (notably
    ones with ``node_modules``) that routinely exceed the cap. Set
    ``"error"`` explicitly for untrusted trees. Rejects any value not
    in :data:`_CWD_HIDDEN_SCAN_OVERFLOW_MODES`.

    :param raw: Raw value from the YAML, e.g. ``"warn"`` or ``None``.
    :returns: One of ``"error"``, ``"warn"``, ``"unlimited"``.
    :raises OmnigentError: If ``raw`` is not one of the supported
        modes.
    """
    if raw is None:
        return OSEnvSandboxSpec.__dataclass_fields__["cwd_hidden_scan_overflow"].default
    if not isinstance(raw, str) or raw not in _CWD_HIDDEN_SCAN_OVERFLOW_MODES:
        raise OmnigentError(
            "os_env.sandbox.cwd_hidden_scan_overflow must be one of "
            f"{list(_CWD_HIDDEN_SCAN_OVERFLOW_MODES)}, got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


# POSIX-portable environment variable name: starts with a letter or
# underscore, followed by letters, digits, or underscores. We don't
# accept anything weirder because (a) anything outside this is almost
# certainly a typo or attack surface, and (b) the env var name will
# be passed straight to ``os.execve``, which interprets ``=`` as the
# name/value separator.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_env_passthrough(raw: object) -> list[str] | None:
    """
    Parse and validate the ``env_passthrough:`` field of
    ``os_env.sandbox``.

    Each entry must be a syntactically valid POSIX environment
    variable name (``[A-Za-z_][A-Za-z0-9_]*``) so we can pass it
    straight to ``os.execve`` and so that an entry containing ``=``
    or other shell-meaningful characters can't smuggle a *value*
    through the *name* slot.

    Validation happens here at parse time so a misconfigured spec
    fails immediately rather than silently passing through a bogus
    name (which would be a no-op at spawn time and silently
    weaken whatever the user thought they were granting).

    :param raw: Raw value from the YAML, e.g.
        ``["AWS_PROFILE", "GITHUB_TOKEN"]``, or ``None`` when the
        field is absent.
    :returns: List of validated env-var names, or ``None`` when
        ``raw`` is ``None`` (the helper will then inherit only the
        always-passed defaults).
    :raises OmnigentError: If ``raw`` isn't a list, contains a
        non-string entry, or contains an entry that isn't a valid
        POSIX env var name.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.env_passthrough must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    sanitized: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise OmnigentError(
                "os_env.sandbox.env_passthrough entries must be strings, "
                f"got {type(entry).__name__}: {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if not _ENV_VAR_NAME_RE.match(entry):
            raise OmnigentError(
                "os_env.sandbox.env_passthrough entries must be POSIX "
                "environment variable names "
                f"(letters/digits/underscore, not starting with a digit): {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        sanitized.append(entry)
    return sanitized


def _parse_egress_rules(raw: object) -> list[str] | None:
    """
    Parse and validate the ``egress_rules:`` field of
    ``os_env.sandbox``.

    Each entry is validated at parse time via
    :func:`~omnigent.inner.egress.rules.parse_rule` so syntax
    errors surface immediately rather than at proxy start time.

    :param raw: The raw value from the YAML mapping. ``None``
        means "no egress filtering".
    :returns: A list of validated rule strings, or ``None``.
    :raises OmnigentError: If the value isn't a list or any
        rule fails to parse.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.egress_rules must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        return None
    from omnigent.inner.egress.rules import parse_rule

    validated: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, str):
            raise OmnigentError(
                "os_env.sandbox.egress_rules entries must be strings, "
                f"got {type(entry).__name__} at index {i}: {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            parse_rule(entry)
        except ValueError as exc:
            raise OmnigentError(
                f"os_env.sandbox.egress_rules[{i}] is invalid: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        validated.append(entry)
    return validated


# YAML ``credential_proxy[*].type`` values are validated by the
# ``Literal`` on :class:`_CredentialProxyItemModel`. ``https_*`` are
# low-level primitives that work for any SaaS; ``git_https`` /
# ``gh_basic`` are presets that auto-wire git / the GitHub CLI.
# ``gh_basic`` hosts when ``targets`` is omitted: the git host plus the
# REST/GraphQL API host.
_GH_BASIC_DEFAULT_TARGETS = ("github.com", "api.github.com")
# Env vars the GitHub CLI reads for its API token; both are set to the
# synthetic placeholder so ``gh api`` authenticates through the proxy.
_GH_TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


class _CredentialSourceModel(BaseModel):
    """Pydantic boundary model for a ``credential_proxy[*].source`` mapping.

    The secret origin is a structured single-key mapping —
    ``{env: VAR}``, ``{file: path}``, or ``{command: cmd}`` — rather than
    a prefix-encoded string. Exactly one key must be set. Pydantic
    validates the shape here; :meth:`to_spec` converts it to the internal
    :class:`CredentialSourceSpec` dataclass the runtime consumes.

    :param env: Parent environment variable name carrying the secret,
        e.g. ``"OA_TEST_GITHUB_PAT"``.
    :param file: File path (``~`` expanded at resolution time) holding the
        secret, e.g. ``"~/.config/tokens/github_pat.txt"``.
    :param command: Shell command whose stdout is the secret, e.g.
        ``"gh auth token"``.
    """

    model_config = ConfigDict(extra="forbid")

    env: str | None = None
    file: str | None = None
    command: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> _CredentialSourceModel:
        """
        Require exactly one source key and validate its value.

        :returns: ``self`` once validated.
        :raises ValueError: If zero or multiple keys are set, ``env`` is
            not a POSIX environment variable name, or ``file`` / ``command``
            is blank.
        """
        set_keys = [
            name
            for name, value in (("env", self.env), ("file", self.file), ("command", self.command))
            if value is not None
        ]
        if len(set_keys) != 1:
            raise ValueError("source must set exactly one of 'env', 'file', or 'command'")
        if self.env is not None and not _ENV_VAR_NAME_RE.match(self.env):
            raise ValueError("source 'env' must be a POSIX environment variable name")
        if self.file is not None and not self.file.strip():
            raise ValueError("source 'file' must be a non-empty path")
        if self.command is not None and not self.command.strip():
            raise ValueError("source 'command' must be a non-empty command")
        return self

    def to_spec(self) -> CredentialSourceSpec:
        """
        Convert this validated model into a :class:`CredentialSourceSpec`.

        :returns: The internal dataclass the runtime resolves the secret
            from. Exactly one of ``env`` / ``file`` / ``command`` is set
            (guaranteed by :meth:`_exactly_one_source`).
        """
        if self.env is not None:
            return CredentialSourceSpec(kind="env", env=self.env)
        if self.file is not None:
            return CredentialSourceSpec(kind="file", path=self.file.strip())
        assert self.command is not None
        return CredentialSourceSpec(kind="command", command=self.command.strip())


class _CredentialProxyItemModel(BaseModel):
    """Pydantic boundary model for one raw ``credential_proxy`` entry.

    Validates the entry's *shape* — ``type``, ``source``, ``target`` /
    ``targets`` cardinality, the optional ``env`` injection shim, and the
    optional Basic ``username`` — replacing the hand-rolled per-field
    ``isinstance`` checks. The parser then normalizes each validated model
    into one or more :class:`CredentialProxyEntry` host bindings (the
    domain transformation pydantic can't express: host/path splitting,
    ``gh_basic`` git-vs-API split, default targets). Unknown keys are
    rejected (``extra="forbid"``) so typos fail loud.

    :param type: Credential preset / primitive, one of ``"https_bearer"``,
        ``"https_basic"``, ``"git_https"``, ``"gh_basic"``.
    :param source: Where the parent resolves the real secret from.
    :param target: A single ``host`` or ``host/path`` binding, e.g.
        ``"github.com/org/repo.git"``. Mutually exclusive with ``targets``.
    :param targets: A non-empty list of ``host`` / ``host/path`` bindings.
        Mutually exclusive with ``target``.
    :param env: Optional sandbox env var that receives the synthetic
        placeholder (opt-in injection shim for credential-gating clients);
        a POSIX environment variable name. Only accepted for the
        ``https_*`` primitives — ``git_https`` / ``gh_basic`` manage
        injection themselves.
    :param username: Optional Basic-auth username for ``https_basic`` /
        ``git_https`` (defaults to ``x-access-token``).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["https_bearer", "https_basic", "git_https", "gh_basic"]
    source: _CredentialSourceModel
    target: str | None = None
    targets: list[str] | None = None
    env: str | None = None
    username: str | None = None

    @field_validator("env")
    @classmethod
    def _env_is_posix(cls, value: str | None) -> str | None:
        """
        Reject an ``env`` that is not a POSIX environment variable name.

        :param value: The raw ``env`` value, or ``None`` when absent.
        :returns: ``value`` unchanged when valid.
        :raises ValueError: If ``env`` is present but malformed.
        """
        if value is not None and not _ENV_VAR_NAME_RE.match(value):
            raise ValueError("env must be a POSIX environment variable name")
        return value

    @field_validator("username")
    @classmethod
    def _username_nonempty(cls, value: str | None) -> str | None:
        """
        Reject an empty ``username``.

        :param value: The raw ``username`` value, or ``None`` when absent.
        :returns: ``value`` unchanged when valid.
        :raises ValueError: If ``username`` is present but empty.
        """
        if value is not None and not value:
            raise ValueError("username must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _check_target_cardinality(self) -> _CredentialProxyItemModel:
        """
        Enforce ``target`` / ``targets`` cardinality and per-type options.

        ``https_*`` and ``git_https`` require exactly one of ``target`` or
        ``targets``; ``gh_basic`` allows neither (it defaults to the
        GitHub git + API hosts) but not both. The ``env`` shim is only
        meaningful for the ``https_*`` primitives, and ``username`` only
        applies to the Basic schemes.

        :returns: ``self`` once validated.
        :raises ValueError: On a cardinality violation or a per-type
            option that does not apply.
        """
        has_target = self.target is not None
        has_targets = self.targets is not None
        if has_targets and not self.targets:
            raise ValueError("targets must be a non-empty list")
        if self.type == "gh_basic":
            if has_target and has_targets:
                raise ValueError("gh_basic accepts at most one of 'target' or 'targets'")
        elif has_target == has_targets:
            raise ValueError("must declare exactly one of 'target' or 'targets'")
        if self.env is not None and self.type in ("git_https", "gh_basic"):
            raise ValueError(f"{self.type} does not accept an 'env' injection shim")
        if self.username is not None and self.type == "https_bearer":
            raise ValueError("https_bearer does not accept a 'username'")
        return self


def _format_validation_error(exc: ValidationError) -> str:
    """
    Render a pydantic ``ValidationError`` as one compact line.

    The credential-proxy parser wraps pydantic failures in
    :class:`OmnigentError` so the CLI / loader surface a single error
    type. This flattens pydantic's structured errors into ``field:
    message`` clauses joined by ``; ``, keyed by the dotted field
    location.

    :param exc: The raised pydantic validation error.
    :returns: A semicolon-joined summary, e.g. ``"source: source must
        set exactly one of 'env', 'file', or 'command'"``. When the
        failure is on the entry root (e.g. a cardinality model
        validator), the location renders as ``"(entry)"``.
    """
    clauses: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(entry)"
        clauses.append(f"{loc}: {err['msg']}")
    return "; ".join(clauses)


def _parse_credential_proxy(raw: object) -> CredentialProxySpec | None:
    """
    Parse and validate the ``credential_proxy:`` field of ``os_env.sandbox``.

    Each list entry declares one of four ``type`` values and is normalized
    into one or more :class:`CredentialProxyEntry` bindings. All four
    default to **swap-on-access** — nothing credential-shaped enters the
    sandbox; the egress proxy attaches the real credential to bound-host
    requests:

    - ``https_bearer``: ``target``/``targets`` + ``source`` + optional
      ``env``. Emits ``Authorization: Bearer <real>`` upstream.
    - ``https_basic``: ``target``/``targets`` + ``source`` + optional
      ``env`` + optional ``username``. Emits ``Authorization: Basic <real>``.
    - ``git_https``: ``target``/``targets`` + ``source`` + optional
      ``username``. Git over HTTPS via swap-on-access (Basic).
    - ``gh_basic``: ``source`` + optional ``targets``. Swap-on-access for
      the git host; injects ``GH_TOKEN`` / ``GITHUB_TOKEN`` for the API
      host because ``gh`` won't call without a local token.

    The optional ``env`` field is the opt-in injection shim for clients
    that refuse to issue a request without a local credential.

    Each entry's shape is validated by :class:`_CredentialProxyItemModel`
    (pydantic) before normalization; a :class:`pydantic.ValidationError`
    is re-raised as an :class:`OmnigentError` so callers see one error
    type.

    :param raw: Raw value from the YAML, e.g. ``[{"type": "git_https",
        "target": "github.com/org/repo.git", "source": {"env": "GH_PAT"}}]``,
        or ``None`` when the field is absent.
    :returns: A populated :class:`CredentialProxySpec`, or ``None`` when
        ``raw`` is absent or an empty list.
    :raises OmnigentError: If the value isn't a list, an entry fails
        validation (unknown ``type``, bad ``source``, target cardinality,
        etc.), or two entries bind the same host.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.credential_proxy must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        return None
    entries: list[CredentialProxyEntry] = []
    for i, item in enumerate(raw):
        try:
            model = _CredentialProxyItemModel.model_validate(item)
        except ValidationError as exc:
            raise OmnigentError(
                f"os_env.sandbox.credential_proxy[{i}] is invalid: "
                f"{_format_validation_error(exc)}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        source = model.source.to_spec()
        if model.type == "gh_basic":
            entries.extend(_normalize_gh_basic(model, source=source, index=i))
        elif model.type == "https_bearer":
            entries.extend(_normalize_https_bearer(model, source=source, index=i))
        elif model.type == "https_basic":
            entries.extend(_normalize_https_basic(model, source=source, index=i))
        else:  # git_https
            entries.extend(_normalize_git_https(model, source=source, index=i))
    if not entries:
        return None
    # Fail loud on conflicting host bindings. The egress proxy keys its
    # rewrite table by host, so two entries binding the same host would
    # silently last-win (one credential dropped). Reject it at parse time
    # rather than picking a binding nondeterministically.
    seen_hosts: dict[str, str] = {}
    for entry in entries:
        host_key = entry.host.lower()
        if host_key in seen_hosts:
            raise OmnigentError(
                "os_env.sandbox.credential_proxy binds host "
                f"{entry.host!r} more than once (also via "
                f"{seen_hosts[host_key]!r}); each host may be bound by at "
                "most one credential. Remove the duplicate entry.",
                code=ErrorCode.INVALID_INPUT,
            )
        seen_hosts[host_key] = entry.host
    return CredentialProxySpec(entries=entries)


def _credential_proxy_macos_unsupported_reason(
    credential_proxy: CredentialProxySpec | None,
    sandbox_type: str,
) -> str | None:
    """
    Explain why a ``credential_proxy`` cannot work under ``darwin_seatbelt``.

    The ``gh_basic`` preset emits a ``token``-scheme binding for the GitHub
    API host (``api.*``) and injects ``GH_TOKEN`` / ``GITHUB_TOKEN`` so the
    GitHub CLI authenticates through the egress MITM proxy. ``gh`` is a Go
    binary, and Go on macOS verifies TLS against the system keychain via
    Security.framework -- it ignores ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``,
    which is exactly how the egress proxy publishes its MITM CA to sandboxed
    tools. ``gh`` therefore rejects the proxy's forged certificate and every
    ``gh`` call fails with ``"certificate is not trusted"``. Since
    ``darwin_seatbelt`` is macOS-only, the combination can never succeed, so we
    reject it at parse time instead of surfacing an opaque runtime TLS error.

    The ``token`` scheme is emitted only by ``gh_basic`` (see
    :func:`_normalize_gh_basic`); keying on the scheme rather than re-deriving
    the original ``type`` keeps this check independent of how the presets
    normalize into bindings.

    :param credential_proxy: Parsed credential-proxy spec, or ``None`` when the
        ``credential_proxy:`` field is absent.
    :param sandbox_type: Resolved sandbox backend, e.g. ``"darwin_seatbelt"``
        (macOS) or ``"linux_bwrap"`` (Linux).
    :returns: A human-readable rejection message when a ``gh_basic`` (i.e. a
        ``token``-scheme) binding is configured on ``darwin_seatbelt``, else
        ``None``.
    """
    if credential_proxy is None or sandbox_type != "darwin_seatbelt":
        return None
    if not any(entry.scheme == "token" for entry in credential_proxy.entries):
        return None
    return (
        "os_env.sandbox.credential_proxy type 'gh_basic' does not work on macOS "
        "(sandbox.type=darwin_seatbelt). 'gh_basic' wires the GitHub CLI 'gh', "
        "which is a Go binary, and Go on macOS verifies TLS against the system "
        "keychain (Security.framework) and ignores SSL_CERT_FILE -- the "
        "environment variable the egress MITM proxy uses to publish its CA to "
        "sandboxed tools. 'gh' therefore rejects the proxy's certificate and "
        "every 'gh' call fails with 'certificate is not trusted'. Use "
        "sandbox.type=linux_bwrap (Go honors SSL_CERT_FILE on Linux), or use "
        "the 'https_bearer' / 'https_basic' primitives with a non-Go client "
        "(curl / python / node) that trusts the proxy CA on macOS."
    )


def _normalize_https_bearer(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize an ``https_bearer`` entry into per-host Bearer bindings.

    The default is swap-on-access: a tool makes its request with no
    ``Authorization`` header and the proxy injects ``Bearer <real>`` for
    the bound host. The optional ``env`` field is an opt-in shim for
    clients that won't issue a request without a local credential — when
    present, the synthetic placeholder is injected into that env var.

    :param model: The validated ``https_bearer`` entry; carries
        ``target``/``targets`` and an optional ``env`` (the sandbox env
        var that receives the synthetic placeholder).
    :param source: Parsed credential source shared by every host binding.
    :param index: Entry index for error messages.
    :returns: One :class:`CredentialProxyEntry` per declared host, each
        wiring ``Authorization: Bearer <real>`` upstream.
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    inject_env = [model.env] if model.env is not None else []
    return [
        CredentialProxyEntry(
            host=host,
            scheme="bearer",
            source=source,
            inject_env=inject_env,
        )
        for host in _resolve_credential_hosts(model, index=index)
    ]


def _normalize_https_basic(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize an ``https_basic`` entry into per-host Basic bindings.

    Like ``https_bearer`` this defaults to swap-on-access; ``env`` is an
    optional opt-in injection shim.

    :param model: The validated ``https_basic`` entry; carries
        ``target``/``targets`` with optional ``env`` and optional
        ``username`` (defaults to ``x-access-token``).
    :param source: Parsed credential source shared by every host binding.
    :param index: Entry index for error messages.
    :returns: One :class:`CredentialProxyEntry` per declared host, each
        wiring ``Authorization: Basic b64(username:<real>)`` upstream.
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    inject_env = [model.env] if model.env is not None else []
    username = model.username or DEFAULT_BASIC_USERNAME
    return [
        CredentialProxyEntry(
            host=host,
            scheme="basic",
            source=source,
            username=username,
            inject_env=inject_env,
        )
        for host in _resolve_credential_hosts(model, index=index)
    ]


def _normalize_git_https(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize a ``git_https`` entry into per-host Basic bindings.

    Git over HTTPS works purely via swap-on-access: git fires its
    unauthenticated request and the proxy injects ``Basic
    b64(username:<real>)`` for the bound host before it leaves. No env
    var, no in-sandbox git credential helper, nothing credential-shaped
    in the sandbox. (It is the ``https_basic`` primitive with a git-
    friendly default username and no ``env`` shim.)

    :param model: The validated ``git_https`` entry; carries
        ``target``/``targets`` with optional ``username`` (defaults to
        ``x-access-token``).
    :param source: Parsed credential source shared by every host binding.
    :param index: Entry index for error messages.
    :returns: One :class:`CredentialProxyEntry` per declared host (Basic
        upstream, swap-on-access).
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    username = model.username or DEFAULT_BASIC_USERNAME
    return [
        CredentialProxyEntry(
            host=host,
            scheme="basic",
            source=source,
            username=username,
        )
        for host in _resolve_credential_hosts(model, index=index)
    ]


def _normalize_gh_basic(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize a ``gh_basic`` entry into git + API credential bindings.

    The git host (anything not prefixed ``api.``) authenticates via
    swap-on-access (Basic), exactly like ``git_https``. The API host
    (prefixed ``api.``, e.g. ``api.github.com``) keeps ``GH_TOKEN`` /
    ``GITHUB_TOKEN`` injection (the ``token`` scheme): ``gh`` refuses to
    issue an API request unless it sees a token locally, so the synthetic
    placeholder is injected to make it emit a request the proxy can swap.

    :param model: The validated ``gh_basic`` entry; ``target``/``targets``
        are optional and default to ``github.com`` + ``api.github.com``.
    :param source: Parsed credential source shared by both bindings.
    :param index: Entry index for error messages.
    :returns: One or two :class:`CredentialProxyEntry` bindings.
    :raises OmnigentError: If an explicit host fails DNS-safety validation.
    """
    if model.target is not None or model.targets is not None:
        hosts = _resolve_credential_hosts(model, index=index)
    else:
        hosts = list(_GH_BASIC_DEFAULT_TARGETS)
    entries: list[CredentialProxyEntry] = []
    for host in hosts:
        if host.startswith("api."):
            entries.append(
                CredentialProxyEntry(
                    host=host,
                    scheme="token",
                    source=source,
                    inject_env=list(_GH_TOKEN_ENV_VARS),
                )
            )
        else:
            entries.append(
                CredentialProxyEntry(
                    host=host,
                    scheme="basic",
                    source=source,
                    username=DEFAULT_BASIC_USERNAME,
                )
            )
    return entries


def _resolve_credential_hosts(model: _CredentialProxyItemModel, *, index: int) -> list[str]:
    """
    Resolve a validated entry's ``target`` / ``targets`` into bound hosts.

    Cardinality (exactly one of ``target`` / ``targets`` for the
    ``https_*`` / ``git_https`` types; at most one for ``gh_basic``) is
    already enforced by :class:`_CredentialProxyItemModel`; this only
    splits each ``host`` / ``host/path`` value and validates the host
    against the DNS grammar. Only the host component binds the credential
    — path scoping is enforced by ``egress_rules``.

    :param model: The validated entry model. Exactly one of ``target`` /
        ``targets`` is set when this is called.
    :param index: Entry index for error messages.
    :returns: De-duplicated, order-preserving list of lower-cased hosts.
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    if model.target is not None:
        raw_targets = [model.target]
        field_paths = [f"os_env.sandbox.credential_proxy[{index}].target"]
    else:
        # The model validator guarantees ``targets`` is a non-empty list
        # whenever ``target`` is absent for the types that reach here.
        assert model.targets is not None
        raw_targets = model.targets
        field_paths = [
            f"os_env.sandbox.credential_proxy[{index}].targets[{j}]"
            for j in range(len(model.targets))
        ]
    hosts: list[str] = []
    for raw_target, field_path in zip(raw_targets, field_paths, strict=True):
        host = _parse_credential_proxy_host(raw_target, field_path=field_path)
        if host not in hosts:
            hosts.append(host)
    return hosts


def _parse_credential_proxy_host(raw: str, *, field_path: str) -> str:
    """
    Parse one ``host`` or ``host/path`` target into a validated host.

    :param raw: Raw target value, e.g. ``"github.com/org/repo.git"`` or
        ``"api.github.com"``.
    :param field_path: Human-readable path for parse errors.
    :returns: The lower-cased host component.
    :raises OmnigentError: If the value is empty or the host contains
        characters outside the DNS grammar ``[A-Za-z0-9.-]`` (wildcards
        included — credentials bind to an exact host).
    """
    from omnigent.inner.egress.rules import is_dns_safe_host

    if not raw.strip():
        raise OmnigentError(
            f"{field_path} must be a non-empty string (host or host/path), got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    host = raw.strip().split("/", 1)[0].lower()
    if not is_dns_safe_host(host):
        raise OmnigentError(
            f"{field_path} host {host!r} must be an exact DNS hostname "
            "(letters/digits/dot/hyphen, no wildcards)",
            code=ErrorCode.INVALID_INPUT,
        )
    return host


def _parse_compaction(
    raw: dict[str, Any] | None,
) -> CompactionConfig | None:
    """
    Parse the ``compaction:`` block from config.yaml into a
    :class:`CompactionConfig`.

    :param raw: The raw ``compaction:`` mapping from config.yaml, or
        ``None`` if the block was absent. Example:
        ``{"trigger_threshold": 0.8, "recent_window": 5}``.
    :returns: A populated :class:`CompactionConfig`, or ``None`` when
        the ``compaction:`` block is absent.
    """
    if raw is None:
        return None
    return CompactionConfig(
        trigger_threshold=float(raw.get("trigger_threshold", 0.8)),
        recent_window=int(raw.get("recent_window", 5)),
    )


def _read_contained_file(root: Path, value: str) -> str | None:
    """
    Read a bundle-relative file named by *value*, only if it stays in *root*.

    The instruction-file reference comes from a spec field (``instructions:``)
    that, for an uploaded bundle, is attacker-controlled. Resolving symlinks
    and ``..`` and confirming the target is contained in *root* prevents a
    crafted spec (e.g. ``instructions: ../../etc/passwd``) from reading files
    outside the bundle on the runner. A non-contained or non-existent path
    returns ``None`` so the caller falls back to treating *value* as literal
    instruction text — preserving the existing "missing file → inline text"
    behavior for the CLI.

    :param root: The bundle root directory the value is anchored to,
        e.g. ``Path("/tmp/agent-bundle")``.
    :param value: The single-line ``instructions:`` value, e.g.
        ``"prompts/system.md"``.
    :returns: The file contents if *value* names a file contained within
        *root*, else ``None``.
    """
    candidate = root / value
    try:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root.resolve()) and resolved.is_file():
            return resolved.read_text()
    except OSError:
        # Path too long or invalid characters — treat as inline text.
        pass
    return None


def _resolve_instructions(root: Path, raw_value: object) -> str | None:
    """
    Resolve the instructions for an agent image.

    - If ``instructions`` is set in config.yaml and the value is
      a path to an existing file relative to *root*, read that
      file.
    - If ``instructions`` is set but is not a file path, treat
      the value as inline text.
    - If ``instructions`` is not set, scan ``_CONTEXT_FILE_PRIORITY``
      and return the first file found (first-wins, no merge).

    :param root: Path to the agent image directory.
    :param raw_value: The raw ``instructions`` value from
        config.yaml, or ``None`` if the key was absent. May be
        a relative file path (e.g. ``"prompts/system.md"``) or
        inline text.
    :returns: The resolved instruction text, or ``None`` if no
        instructions are available.
    """
    if raw_value is not None:
        text = str(raw_value)
        # Only attempt file lookup for short single-line values
        # that look like filenames (multiline text can't be a path).
        if "\n" not in text:
            contained = _read_contained_file(root, text)
            if contained is not None:
                return contained
        return text
    # Default: first-wins scan across known context files.
    for filename in _CONTEXT_FILE_PRIORITY:
        candidate = root / filename
        try:
            if candidate.is_file():
                return candidate.read_text()
        except OSError:
            pass
    return None


def _parse_share_policy(raw: object) -> SharePolicy:
    """
    Parse the top-level YAML ``agent_session_sharing:`` field into a
    :class:`SharePolicy`.

    This flag is the sole enabler of the ``sys_session_share`` tool
    (independent of ``spawn`` / ``tools.agents``). Sharing mutates
    access control, so it is off by default and an unrecognized value
    fails loud rather than silently disabling the feature.

    Supported YAML shapes:

    - field omitted / ``null`` → :attr:`SharePolicy.NONE` (default;
      tool not registered).
    - ``"none"`` / ``"non-public"`` / ``"public"`` → the matching
      :class:`SharePolicy` member.

    :param raw: The raw YAML value (already parsed). ``None`` or one
        of the three policy strings, e.g. ``"non-public"``.
    :returns: The resolved :class:`SharePolicy`.
    :raises OmnigentError: When the value is neither ``None`` nor one
        of the recognized policy strings (e.g. a boolean, a typo like
        ``"private"``, or a non-string).
    """
    if raw is None:
        return SharePolicy.NONE
    try:
        return SharePolicy(raw)
    except ValueError:
        valid = ", ".join(repr(p.value) for p in SharePolicy)
        raise OmnigentError(
            f"top-level agent_session_sharing: must be one of {valid}; got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from None


def _parse_skills_filter(raw: object) -> str | list[str]:
    """
    Parse the top-level YAML ``skills:`` field into a host-skill
    filter string or list of names.

    Distinct from the bundle-side ``skills/<name>/SKILL.md`` files
    discovered by :func:`_discover_skills` — that's the agent's own
    bundled skills, always loaded. This filter only controls
    HOST-scope skills that the harness picks up from the user's
    machine (``~/.claude/skills/`` and ancestor ``.claude/skills/``
    dirs of the cwd, when running with the Claude SDK harness).

    Supported YAML shapes:

    - field omitted / ``null`` / ``"all"`` → returns ``"all"``;
      every host skill is loaded. Default.
    - ``"none"`` or ``[]`` → returns ``"none"``; no host skills,
      hermetic against the user's local skill library.
    - ``[<name>, ...]`` → returns the list as-is; only the named
      skills are exposed.

    :param raw: The raw YAML value (already parsed). One of
        ``None``, a string, or a list.
    :returns: ``"all"``, ``"none"``, or a non-empty ``list[str]``.
    :raises OmnigentError: When the value isn't one of the
        supported shapes (e.g. boolean, dict, integer), or list
        items are non-strings, or a string isn't ``"all"`` or
        ``"none"``.
    """
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
            # Explicit empty list reads as "no host skills" — same as "none".
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


def discover_host_skills(
    agent_root: Path,
    skills_filter: str | list[str],
) -> list[SkillSpec]:
    """
    Discover host-scope skills from ``.claude/skills/`` and
    ``.agents/skills/`` directories walking up from *agent_root*,
    plus the user's global ``~/.claude/skills/``.

    Not called by :func:`parse` — host-scope skills are a REPL
    concern, not a spec concern. Callers (e.g. ``chat.py``) merge
    the result into ``spec.skills`` before passing to
    ``run_repl``.

    :param agent_root: The agent bundle's root directory.
    :param skills_filter: The parsed ``skills:`` filter from the
        agent spec. ``"none"`` suppresses all host skills;
        ``"all"`` loads everything; a list of names loads only
        those.
    :returns: Deduplicated list of :class:`SkillSpec` objects.
        Later directories (closer to /) lose on name collision
        with earlier ones (closer to agent_root).
    """
    if skills_filter == "none":
        return []

    seen_names: set[str] = set()
    skills: list[SkillSpec] = []
    skipped: list[str] = []
    filter_names: set[str] | None = None
    if isinstance(skills_filter, list):
        filter_names = set(skills_filter)

    def _scan_dir(d: Path) -> None:
        for spec in _discover_skills(d, skipped=skipped):
            if spec.name in seen_names:
                continue
            if filter_names is not None and spec.name not in filter_names:
                continue
            seen_names.add(spec.name)
            skills.append(spec)

    # Walk from agent_root up to filesystem root, scanning
    # .claude/skills/ and .agents/skills/ at each level.
    current = agent_root.resolve()
    while True:
        for dotdir in (".claude", ".agents"):
            candidate = current / dotdir / "skills"
            if candidate.is_dir():
                _scan_dir(candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Also scan user-global skill directories.
    for dotdir in (".claude", ".agents"):
        home_skills = Path.home() / dotdir / "skills"
        if home_skills.is_dir():
            _scan_dir(home_skills)

    if skipped:
        dest = getattr(sys.stderr, "_original_stderr", sys.stderr)
        n = len(skipped)
        print(
            f"Warning: skipped {n} skill(s) with frontmatter errors:",
            file=dest,
        )
        for detail in skipped:
            print(f"  - {detail}", file=dest)
        print(
            "Fix the YAML frontmatter in the above SKILL.md file(s) to load them.",
            file=dest,
        )

    return skills


def _discover_skills(
    skills_dir: Path,
    *,
    skipped: list[str] | None = None,
) -> list[SkillSpec]:
    """
    Discover and parse all skills under the ``skills/`` directory.

    Each subdirectory containing a ``SKILL.md`` file is parsed via
    :func:`_parse_skill`.

    :param skills_dir: Path to the ``skills/`` directory, e.g.
        ``root / "skills"``.
    :param skipped: When not ``None``, enables lenient mode: YAML
        parse errors and missing-frontmatter errors are caught
        per-file, a human-readable message is appended to this
        list, and the skill is skipped instead of aborting.
        Pass ``None`` (the default) to fail loud on the first
        error — used for bundled skills that the agent author
        controls.
    :returns: A sorted list of parsed :class:`SkillSpec` objects.
        Returns an empty list if *skills_dir* does not exist.
    """
    if not skills_dir.is_dir():
        return []
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError as exc:
        # Lenient mode (a skipped list was passed, e.g. host/plugin menu
        # discovery): an unreadable skills dir must not 500 the caller —
        # log and yield nothing. Strict mode (bundle parse) re-raises.
        if skipped is None:
            raise
        _log.warning("Skipping unreadable skills dir %s: %s", skills_dir, exc)
        skipped.append(f"{skills_dir}: {exc}")
        return []
    skills: list[SkillSpec] = []
    for skill_dir in entries:
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            skill = _parse_skill(skill_md)
        except (OmnigentError, yaml.YAMLError) as exc:
            if skipped is None:
                raise
            msg = f"{skill_md}: {exc}"
            _log.warning("Skipping skill with bad frontmatter: %s", msg)
            skipped.append(msg)
            continue
        skills.append(skill)
    return skills


# Quoted string spellings treated as boolean false. PyYAML already parses the
# *bare* YAML 1.1 false words (``false``/``False``/``no``/``off``) to a real
# ``bool``, caught by the ``raw is False`` branch below; this set only catches
# the *quoted* forms (e.g. ``user-invocable: "no"``) that arrive as ``str``.
_FALSEY_STRINGS = frozenset({"false", "no", "off", "0"})


def _falsey_flag(raw: object) -> bool:
    """
    Return whether a YAML frontmatter flag reads as boolean ``false``.

    Accepts a genuine YAML bool (``raw is False`` — which already covers
    the bare words ``false``/``no``/``off`` that PyYAML parses to a
    ``bool``) and the quoted string spellings in :data:`_FALSEY_STRINGS`
    (case-insensitive, surrounding whitespace ignored) — YAML keeps a
    quoted value as a ``str``, so the string branch is the one that
    silently regresses without a test. Every other value (absent ⇒
    caller's default, ``true``, other strings, ``0`` as int) is not
    falsey.

    :param raw: The raw frontmatter value, e.g. ``False``, ``"no"``,
        or ``True``.
    :returns: ``True`` only when *raw* means boolean false.
    """
    if raw is False:
        return True
    return isinstance(raw, str) and raw.strip().lower() in _FALSEY_STRINGS


def _parse_skill(skill_md: Path) -> SkillSpec:
    """
    Parse a single ``SKILL.md`` file into a :class:`SkillSpec`.

    The file must begin with YAML frontmatter delimited by ``---``
    lines, containing at least ``name`` and ``description`` keys.

    :param skill_md: Path to the ``SKILL.md`` file, e.g.
        ``skills/code-review/SKILL.md``.
    :returns: A populated :class:`SkillSpec`.
    :raises OmnigentError: If the file cannot be read, or the
        frontmatter is missing, malformed, or lacks required fields.
        All failure modes funnel through a single exception type so
        the tolerant scanner in :func:`_discover_skills` (when
        ``strict=False``) can catch them uniformly.
    """
    try:
        text = skill_md.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError (a non-UTF-8 SKILL.md) is a ValueError, not an
        # OSError — funnel it through OmnigentError too so the lenient
        # scanner in _discover_skills and the per-skill guards in the menu
        # providers catch it and skip the file instead of 500-ing the menu.
        raise OmnigentError(
            f"SKILL.md could not be read: {skill_md}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise OmnigentError(
            f"SKILL.md missing YAML frontmatter: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    frontmatter_str, content = match.groups()
    try:
        frontmatter = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as exc:
        raise OmnigentError(
            f"SKILL.md has invalid YAML frontmatter: {skill_md}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if not isinstance(frontmatter, dict):
        raise OmnigentError(
            f"SKILL.md frontmatter must be a YAML mapping: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    name = frontmatter.get("name")
    if name is None:
        raise OmnigentError(
            f"SKILL.md frontmatter missing required field 'name': {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    description = frontmatter.get("description")
    if description is None:
        raise OmnigentError(
            f"SKILL.md frontmatter missing required field 'description': {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    # ``user-invocable: false`` marks an internal orchestration skill that
    # the user should not invoke directly; absent/true ⇒ invocable.
    user_invocable = not _falsey_flag(frontmatter.get("user-invocable", True))
    return SkillSpec(
        name=str(name),
        description=str(description),
        content=content.strip(),
        skill_dir=skill_md.parent,
        user_invocable=user_invocable,
    )


def expand_env_vars(
    mapping: dict[str, str],
) -> dict[str, str]:
    """
    Expand ``${VAR}`` and ``$VAR`` references in dict values
    against the current process environment.

    Raises :class:`OmnigentError` if any value still contains an
    unresolved ``$VAR`` or ``${VAR}`` reference after expansion.
    This catches typos and missing environment variables at parse
    time rather than silently passing literal ``${MISSING}`` to
    MCP servers or LLM clients.

    :param mapping: A string-to-string dict, e.g.
        ``{"TOKEN": "${GITHUB_TOKEN}"}``.
    :returns: A new dict with expanded values.
    :raises OmnigentError: If a value contains an unresolved
        environment variable reference after expansion.
    """
    result: dict[str, str] = {}
    for key, value in mapping.items():
        expanded = os.path.expandvars(value)
        check_unresolved_env_vars(key, expanded)
        result[key] = expanded
    return result


# Matches $VAR or ${VAR} patterns that survived expansion.
# Excludes $$ (escaped dollar sign).
_UNRESOLVED_VAR_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


def check_unresolved_env_vars(key: str, value: str) -> None:
    """
    Raise if *value* contains unresolved environment variable
    references.

    Called after :func:`os.path.expandvars` to catch variables
    that were not set in the environment. Without this check,
    ``os.path.expandvars`` silently passes through the literal
    ``${VAR}`` string, which causes hard-to-debug failures
    downstream (e.g. an MCP server receiving ``$GITHUB_TOKEN``
    as a literal auth token).

    :param key: The dict key (for error messages), e.g.
        ``"GITHUB_TOKEN"``.
    :param value: The expanded value to check, e.g.
        ``"Bearer ${MISSING}"``.
    :raises OmnigentError: If *value* contains an unresolved
        ``$VAR`` or ``${VAR}`` reference.
    """
    match = _UNRESOLVED_VAR_RE.search(value)
    if match is not None:
        raise OmnigentError(
            f"Unresolved environment variable {match.group()!r} "
            f"in config key {key!r}. Set the variable in the "
            f"environment or remove the reference.",
            code=ErrorCode.INVALID_INPUT,
        )


_TOOLS_CONFIG_KEYS = frozenset({"agents", "builtins", "timeout", "retry", "sandbox"})


def _parse_inline_mcp_servers(
    raw_tools: object,
    *,
    expand_env: bool = True,
) -> list[MCPServerConfig]:
    """
    Extract inline ``type: mcp`` entries from the top-level
    ``tools:`` block of config.yaml.

    The inline MCP format uses the YAML mapping key as the server
    name and derives the transport from the fields present:

    .. code-block:: yaml

        tools:
          github:
            type: mcp
            command: npx
            args: ["-y", "@modelcontextprotocol/server-github"]
          search:
            type: mcp
            url: https://mcp.example.com/sse
            headers:
              Authorization: "Bearer ${MCP_TOKEN}"

    ``type: mcp`` entries are those whose value is a dict containing
    ``type: mcp``. Standard :class:`ToolsConfig` keys (``agents``,
    ``builtins``, ``timeout``, ``retry``, ``sandbox``) are skipped
    even when they appear as dict values.

    Transport is inferred: ``command`` present → ``"stdio"``,
    ``url`` present → ``"http"``. Entries where neither is present
    (e.g. ``databricks_server``-only Databricks MCPs) are skipped —
    they don't have a local spawn or SSE endpoint to display.

    :param raw_tools: The raw value of the top-level ``tools:`` key
        in config.yaml. ``None`` or a non-dict value returns an empty
        list without raising.
    :param expand_env: Whether to expand ``${VAR}`` references in
        ``headers`` and ``env`` values. ``True`` (default) for
        deploy/runtime; ``False`` for scaffolding/validation.
    :returns: A list of :class:`MCPServerConfig` objects, one per
        inline MCP entry, in YAML key order.
    """
    if not isinstance(raw_tools, dict):
        return []
    servers: list[MCPServerConfig] = []
    for key, val in raw_tools.items():
        if key in _TOOLS_CONFIG_KEYS:
            continue
        if not isinstance(val, dict):
            continue
        if str(val.get("type", "")) != "mcp":
            continue
        name = str(key)
        command = val.get("command")
        url = val.get("url")
        if command is not None:
            transport: str = "stdio"
        elif url is not None:
            transport = "http"
        else:
            # Databricks-managed server or unknown shape — no local
            # endpoint to display; skip.
            continue
        raw_args = val.get("args", [])
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        raw_headers = val.get("headers", {})
        if raw_headers and not isinstance(raw_headers, dict):
            raise OmnigentError(
                f"Inline MCP server {name!r} 'headers' must be a mapping",
                code=ErrorCode.INVALID_INPUT,
            )
        headers = expand_env_vars(raw_headers) if expand_env and raw_headers else raw_headers
        raw_env = val.get("env", {})
        if raw_env and not isinstance(raw_env, dict):
            raise OmnigentError(
                f"Inline MCP server {name!r} 'env' must be a mapping",
                code=ErrorCode.INVALID_INPUT,
            )
        env = expand_env_vars(raw_env) if expand_env and raw_env else raw_env
        # Optional Databricks auth — resolves a bearer token at
        # connection time from ~/.databrickscfg.
        raw_auth = val.get("auth")
        databricks_profile: str | None = None
        if isinstance(raw_auth, dict) and str(raw_auth.get("type", "")) == "databricks":
            raw_profile = raw_auth.get("profile")
            if raw_profile is None:
                raise OmnigentError(
                    f"Inline MCP server {name!r} auth type 'databricks' "
                    f"requires a 'profile' field",
                    code=ErrorCode.INVALID_INPUT,
                )
            databricks_profile = str(raw_profile)
        servers.append(
            MCPServerConfig(
                name=name,
                transport=transport,
                # str() guards against non-string YAML scalars (int, bool, etc.)
                description=str(raw_desc)
                if (raw_desc := val.get("description")) is not None
                else None,
                url=str(url) if url is not None else None,
                command=str(command) if command is not None else None,
                args=args,
                headers=headers,
                env=env,
                databricks_profile=databricks_profile,
            )
        )
    return servers


def _discover_mcp_servers(
    mcp_dir: Path,
    *,
    expand_env: bool = True,
) -> list[MCPServerConfig]:
    """
    Discover and parse all MCP server configs under
    ``tools/mcp/``.

    Each ``.yaml`` file in the directory is parsed into an
    :class:`MCPServerConfig`.

    :param mcp_dir: Path to the ``tools/mcp/`` directory, e.g.
        ``root / "tools" / "mcp"``.
    :param expand_env: Whether to expand ``${VAR}`` references in
        headers. ``False`` keeps literals as-is.
    :returns: A sorted list of parsed :class:`MCPServerConfig`
        objects. Returns an empty list if *mcp_dir* does not
        exist.
    :raises OmnigentError: If any YAML file is malformed or
        missing required fields (``name``, ``transport``).
    """
    if not mcp_dir.is_dir():
        return []
    servers: list[MCPServerConfig] = []
    for yaml_file in sorted(mcp_dir.glob("*.yaml")):
        raw = yaml.safe_load(yaml_file.read_text())
        if not isinstance(raw, dict):
            raise OmnigentError(
                f"MCP config must be a YAML mapping: {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
        name = raw.get("name")
        if name is None:
            raise OmnigentError(
                f"MCP config missing required field 'name': {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
        transport = raw.get("transport")
        if transport is None:
            raise OmnigentError(
                f"MCP config missing required field 'transport': {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
        transport_str = str(transport)
        if transport_str == "http":
            servers.append(_parse_http_mcp_server(name, raw, yaml_file, expand_env=expand_env))
        elif transport_str == "stdio":
            servers.append(_parse_stdio_mcp_server(name, raw, yaml_file, expand_env=expand_env))
        else:
            raise OmnigentError(
                f"MCP server {name!r} uses unsupported transport "
                f"{transport!r} — must be 'http' or 'stdio': {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
    return servers


def _parse_http_mcp_server(
    name: object,
    raw: dict[str, Any],  # type: ignore[explicit-any]
    yaml_file: Path,
    *,
    expand_env: bool,
) -> MCPServerConfig:
    """
    Parse an HTTP (SSE) MCP server YAML into an :class:`MCPServerConfig`.

    HTTP transport requires ``url``; ``headers`` is optional and
    expanded via :func:`expand_env_vars` when *expand_env* is True.
    Stdio-only fields (``command``, ``args``, ``env``, ``sandbox``)
    are rejected loud — mixing transports silently would hide bugs
    in the YAML.

    :param name: The ``name`` field from the YAML (already validated
        non-None by the caller), e.g. ``"github"``.
    :param raw: Parsed YAML mapping for the MCP file, e.g.
        ``{"name": "github", "transport": "http", "url": "..."}``.
    :param yaml_file: Path to the source file — used in error messages.
    :param expand_env: Whether to expand ``${VAR}`` references in
        ``headers``.
    :returns: A fully populated :class:`MCPServerConfig` with
        ``transport == "http"``.
    :raises OmnigentError: If ``url`` is missing or a stdio-only
        field was supplied.
    """
    _reject_wrong_transport_keys(
        name,
        raw,
        yaml_file,
        disallowed=("command", "args", "env", "sandbox"),
        transport_name="http",
    )
    url = raw.get("url")
    if url is None:
        raise OmnigentError(
            f"MCP server {name!r} missing required field 'url': {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    return MCPServerConfig(
        name=str(name),
        transport="http",
        url=str(url),
        headers=(
            expand_env_vars(raw.get("headers", {})) if expand_env else raw.get("headers", {})
        ),
        description=raw.get("description"),
        timeout=int(raw["timeout"]) if "timeout" in raw else None,
        retry=_parse_retry(raw["retry"]) if "retry" in raw else None,
    )


def _parse_stdio_mcp_server(
    name: object,
    raw: dict[str, Any],  # type: ignore[explicit-any]
    yaml_file: Path,
    *,
    expand_env: bool,
) -> MCPServerConfig:
    """
    Parse a stdio MCP server YAML into an :class:`MCPServerConfig`.

    Stdio transport requires ``command``; ``args`` and ``env`` are
    optional (default empty). ``sandbox`` defaults to ``True`` — the
    subprocess is srt-wrapped when possible. HTTP-only fields
    (``url``, ``headers``) are rejected loud.

    Environment values are expanded when *expand_env* is True so
    YAML like ``env: {GITHUB_TOKEN: \"${GITHUB_TOKEN}\"}`` resolves
    at parse time. ``args`` are NOT expanded — they're treated as
    a literal argv (consistent with how :class:`LocalToolInfo`
    treats command args).

    :param name: The ``name`` field from the YAML (already validated
        non-None by the caller).
    :param raw: Parsed YAML mapping, e.g.
        ``{"name": "github", "transport": "stdio", "command": "npx",
        "args": ["-y", "..."], "env": {"GITHUB_TOKEN": "${GH_TOKEN}"}}``.
    :param yaml_file: Path to the source file — used in error messages.
    :param expand_env: Whether to expand ``${VAR}`` references in
        ``env``.
    :returns: A fully populated :class:`MCPServerConfig` with
        ``transport == "stdio"``.
    :raises OmnigentError: If ``command`` is missing, ``args`` is
        not a list, ``env`` is not a mapping, or an HTTP-only field
        was supplied.
    """
    _reject_wrong_transport_keys(
        name,
        raw,
        yaml_file,
        disallowed=("url", "headers"),
        transport_name="stdio",
    )
    command = raw.get("command")
    if command is None:
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') missing required field "
            f"'command': {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_args = raw.get("args", [])
    if not isinstance(raw_args, list):
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') 'args' must be a list: {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_env = raw.get("env", {})
    if not isinstance(raw_env, dict):
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') 'env' must be a mapping: {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    env = expand_env_vars(raw_env) if expand_env else raw_env
    if "sandbox" in raw:
        # Step 7: ``sandbox: <bool>`` was an AP-only no-op that
        # wrapped the stdio spawn with ``srt``. srt's default
        # policy blocks outbound network, which broke every
        # useful MCP server, so the field is gone. Reject loud
        # so authors who copy old YAMLs see the change instead
        # of a silently-ignored key. Future per-MCP sandboxing
        # will use a different schema (per-host outbound
        # allowlists) routed through the environments primitive.
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') 'sandbox' field "
            f"was removed in step 7 of the harness contract migration: "
            f"{yaml_file}. The previous default (srt-wrap) blocked "
            f"outbound network and broke every useful MCP. Drop the "
            f"key from the YAML; future sandboxing will use a "
            f"per-MCP outbound-host allowlist with a different schema.",
            code=ErrorCode.INVALID_INPUT,
        )
    return MCPServerConfig(
        name=str(name),
        transport="stdio",
        command=str(command),
        args=[str(a) for a in raw_args],
        env={str(k): str(v) for k, v in env.items()},
        description=raw.get("description"),
        timeout=int(raw["timeout"]) if "timeout" in raw else None,
        retry=_parse_retry(raw["retry"]) if "retry" in raw else None,
    )


def _reject_wrong_transport_keys(
    name: object,
    raw: dict[str, Any],  # type: ignore[explicit-any]
    yaml_file: Path,
    *,
    disallowed: tuple[str, ...],
    transport_name: str,
) -> None:
    """
    Fail loud if an MCP YAML mixes fields from the wrong transport.

    E.g. ``transport: http`` with a ``command:`` key, or
    ``transport: stdio`` with a ``url:`` key — both silently-ignored
    shapes would hide authoring bugs. Name every offending key in
    the error so the author can clean the YAML in one pass.

    :param name: The MCP server's ``name`` field, used in the error
        message.
    :param raw: Parsed YAML mapping.
    :param yaml_file: Path to the source file — used in error messages.
    :param disallowed: Tuple of keys that MUST NOT appear for this
        transport, e.g. ``("url", "headers")`` for stdio.
    :param transport_name: Human-readable transport label for the
        error message, e.g. ``"stdio"``.
    :raises OmnigentError: When any *disallowed* key is present
        in *raw*.
    """
    offenders = [k for k in disallowed if k in raw]
    if offenders:
        raise OmnigentError(
            f"MCP server {name!r} (transport={transport_name!r}) has "
            f"wrong-transport field(s) {offenders!r}: {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )


def _discover_local_tools(
    tools_dir: Path,
) -> list[LocalToolInfo]:
    """
    Discover local tool files under ``tools/python/`` and
    ``tools/typescript/``.

    Tool names are derived from the file stem directly (e.g.
    ``arxiv_search.py`` becomes ``"arxiv_search"``). Underscores
    are preserved — the tool name regex requires
    ``[a-zA-Z0-9_-]``.

    :param tools_dir: Path to the ``tools/`` directory, e.g.
        ``root / "tools"``.
    :returns: A sorted list of :class:`LocalToolInfo` objects
        covering both Python and TypeScript tools.
    """
    tools: list[LocalToolInfo] = []
    for language, subdir, ext in [
        ("python", "python", ".py"),
        ("typescript", "typescript", ".ts"),
    ]:
        lang_dir = tools_dir / subdir
        if not lang_dir.is_dir():
            continue
        for tool_file in sorted(lang_dir.glob(f"*{ext}")):
            tool_name = tool_file.stem
            rel_path = str(tool_file.relative_to(tools_dir.parent))
            tools.append(LocalToolInfo(name=tool_name, path=rel_path, language=language))
    return tools


def _discover_sub_agents(
    agents_dir: Path,
    *,
    expand_env: bool = True,
) -> list[AgentSpec]:
    """
    Recursively discover and parse sub-agents under ``agents/``.

    Each subdirectory containing a ``config.yaml`` is parsed via
    :func:`parse`, producing a nested :class:`AgentSpec`.

    :param agents_dir: Path to the ``agents/`` directory, e.g.
        ``root / "agents"``.
    :param expand_env: Whether to expand ``${VAR}`` references.
        Propagated to :func:`parse` for each sub-agent.
    :returns: A sorted list of recursively parsed
        :class:`AgentSpec` objects. Returns an empty list if
        *agents_dir* does not exist.
    """
    if not agents_dir.is_dir():
        return []
    sub_agents: list[AgentSpec] = []
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        config_yaml = agent_dir / "config.yaml"
        if not config_yaml.exists():
            continue
        sub_agents.append(parse(agent_dir, expand_env=expand_env))
    return sub_agents


# ── Guardrails / policy parsers (POLICIES.md §3.3) ───────────
#
# Per POLICIES.md §13, most policy-spec errors fail LOUD at
# spec load — these helpers raise ``OmnigentError`` on
# malformed input rather than silently coercing to defaults.
# The exception is ``_parse_condition``, which permissively
# coerces scalar / list values to strings (matching omnigent
# parity for label values — see §14 of the audit).


def _parse_guardrails(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> GuardrailsSpec | None:
    """
    Parse the ``guardrails:`` block into a :class:`GuardrailsSpec`.

    Returns ``None`` when the block is absent entirely — the
    runtime builds a no-op policy engine in that case
    (POLICIES.md §10 zero-policy case).

    :param raw: The ``guardrails:`` mapping from config.yaml,
        or ``None`` when the block was absent. Example:
        ``{"labels": {"integrity": {"initial": "1",
        "values": ["0", "1"], "monotonic": "decreasing"}},
        "policies": {"block_canada_input": {"type": "prompt",
        ...}}, "ask_timeout": 30}``.
    :param expand_env: Whether to expand ``${VAR}`` references
        in any nested ``llm.connection`` blocks (PromptPolicy
        LLM overrides). Propagated to :func:`_parse_llm`.
    :returns: A populated :class:`GuardrailsSpec`, or ``None``
        when *raw* is ``None``.
    :raises OmnigentError: On any spec-load validation
        failure (unknown phases, empty ``on:`` lists, invalid
        label defs, bad policy types, etc.).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"guardrails: must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return GuardrailsSpec(
        labels=_parse_label_defs(raw.get("labels")),
        policies=_parse_policies(raw.get("policies"), expand_env=expand_env),
        ask_timeout=_parse_guardrails_ask_timeout(
            raw.get("ask_timeout", DEFAULT_ASK_TIMEOUT),
        ),
    )


def _parse_guardrails_ask_timeout(raw: Any) -> int:
    """
    Validate and coerce the spec-wide ``ask_timeout`` value.

    Accepts an integer (or string that parses as one);
    rejects ``<= 0`` at spec load per POLICIES.md §13. The
    ambiguity between "instant DENY" and "wait forever"
    drove the strict > 0 rule — both intents have explicit
    paths (omit ASK from action list; use a large finite
    number).

    :param raw: Raw ``guardrails.ask_timeout:`` value.
    :returns: Validated timeout in seconds.
    :raises OmnigentError: On non-integer or non-positive
        values.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise OmnigentError(
            f"guardrails.ask_timeout must be an integer, got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if value <= 0:
        raise OmnigentError(
            "guardrails.ask_timeout must be > 0 "
            "(omit ASK from policy action list for instant-DENY; "
            "use large finite values for long waits)",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


def _parse_label_defs(
    raw: dict[str, Any] | None,
) -> dict[str, LabelDef] | None:
    """
    Parse the ``guardrails.labels:`` block into a dict of
    :class:`LabelDef` by key.

    Accepts three YAML shapes per POLICIES.md §3.1:

    - Bare string: ``integrity: "1"`` → schemaless with
      ``initial="1"``.
    - Dict (schema'd with initial):
      ``{initial: "1", values: [...], monotonic: ...}``.
    - Dict (schema'd without initial):
      ``{values: [...], monotonic: ...}``.

    :param raw: The ``labels:`` mapping, or ``None``.
    :returns: Dict mapping each label key to its
        :class:`LabelDef`. ``None`` when *raw* is ``None``.
    :raises OmnigentError: On malformed entries — empty
        dict, ``initial`` not in ``values``, unknown
        ``monotonic`` direction, etc.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"guardrails.labels: must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    defs: dict[str, LabelDef] = {}
    for key, entry in raw.items():
        defs[str(key)] = _parse_single_label_def(str(key), entry)
    return defs


def _parse_single_label_def(key: str, entry: Any) -> LabelDef:
    """
    Parse one label definition entry.

    :param key: The label key, used in error messages, e.g.
        ``"integrity"``.
    :param entry: Either a string (shorthand: value becomes
        ``initial``) or a dict with one or more of
        ``initial``, ``values``, ``monotonic``.
    :returns: A populated :class:`LabelDef`.
    :raises OmnigentError: On any malformed value.
    """
    # Bare-string shorthand: `integrity: "1"` → initial only.
    if isinstance(entry, str):
        return LabelDef(initial=entry)
    if isinstance(entry, bool) or entry is None or isinstance(entry, int | float):
        # Coerce scalar to string for shorthand form. YAML
        # authors often write `: 1` expecting "1"; coercing
        # matches the condition-value coercion policy elsewhere.
        return LabelDef(initial=str(entry) if entry is not None else None)
    if not isinstance(entry, dict):
        raise OmnigentError(
            f"label {key!r} must be a string or mapping, got {type(entry).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not entry:
        # Empty-dict typo guard — matches POLICIES.md §13.
        raise OmnigentError(
            f"label {key!r} declares an empty dict — must contain at "
            f"least one of `initial`, `values`, or `monotonic`",
            code=ErrorCode.INVALID_INPUT,
        )
    initial = _coerce_label_initial(entry.get("initial"))
    values = _coerce_label_values(key, entry.get("values"))
    monotonic = _coerce_label_monotonic(key, entry.get("monotonic"))
    _validate_label_def_cross_fields(key, initial, values, monotonic)
    return LabelDef(initial=initial, values=values, monotonic=monotonic)


def _coerce_label_initial(raw: Any) -> str | None:
    """Coerce an ``initial:`` value to ``str | None``."""
    return None if raw is None else str(raw)


def _coerce_label_values(key: str, raw: Any) -> list[str] | None:
    """
    Coerce a ``values:`` list to ``list[str]`` or ``None``.

    :param key: Label key, for error messages.
    :param raw: Raw ``values:`` value from YAML.
    :returns: Every element str-coerced; ``None`` when
        *raw* is ``None``.
    :raises OmnigentError: When *raw* is a non-list
        non-None value.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"label {key!r}: `values` must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return [str(v) for v in raw]


def _coerce_label_monotonic(
    key: str,
    raw: Any,
) -> Literal["increasing", "decreasing"] | None:
    """
    Validate a ``monotonic:`` direction.

    :param key: Label key, for error messages.
    :param raw: Raw ``monotonic:`` value from YAML — must
        be ``"increasing"``, ``"decreasing"``, or absent.
    :returns: The validated direction, or ``None`` when
        *raw* is ``None``.
    :raises OmnigentError: On any other value.
    """
    if raw is None:
        return None
    if raw == "increasing":
        return "increasing"
    if raw == "decreasing":
        return "decreasing"
    raise OmnigentError(
        f"label {key!r}: `monotonic` must be 'increasing' or 'decreasing', got {raw!r}",
        code=ErrorCode.INVALID_INPUT,
    )


def _validate_label_def_cross_fields(
    key: str,
    initial: str | None,
    values: list[str] | None,
    monotonic: Literal["increasing", "decreasing"] | None,
) -> None:
    """
    Enforce cross-field constraints on a :class:`LabelDef`.

    Per POLICIES.md §13:

    - ``monotonic`` requires ``values`` (no positions to
      order without them).
    - When both ``initial`` and ``values`` are declared,
      ``initial`` must be in ``values``.

    :param key: Label key, for error messages.
    :param initial: Pre-coerced initial value.
    :param values: Pre-coerced values list.
    :param monotonic: Pre-validated direction.
    :raises OmnigentError: On any cross-field violation.
    """
    if monotonic is not None and values is None:
        raise OmnigentError(
            f"label {key!r}: `monotonic` requires a `values` list to order against",
            code=ErrorCode.INVALID_INPUT,
        )
    if initial is not None and values is not None and initial not in values:
        raise OmnigentError(
            f"label {key!r}: `initial` value {initial!r} is not in declared `values` {values!r}",
            code=ErrorCode.INVALID_INPUT,
        )


def _parse_policies(
    raw: dict[str, Any] | list[Any] | None,
    *,
    expand_env: bool = True,
) -> list[PolicySpec] | None:
    """
    Parse the ``guardrails.policies:`` block.

    YAML uses a mapping keyed by policy name (preserving
    YAML declaration order, which the engine relies on per
    POLICIES.md §4). Returns a list of
    :class:`PolicySpec` instances in that order.

    :param raw: The ``policies:`` mapping, or ``None``.
    :param expand_env: Propagated to
        :func:`_parse_llm` for any PromptPolicy ``llm:``
        overrides.
    :returns: List of policy specs, or ``None`` when *raw*
        is ``None``.
    :raises OmnigentError: On any malformed policy entry.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"guardrails.policies: must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    policies: list[PolicySpec] = []
    for name, entry in raw.items():
        policies.append(
            _parse_policy_spec(str(name), entry, expand_env=expand_env),
        )
    return policies


def _parse_policy_spec(
    name: str,
    data: Any,
    *,
    expand_env: bool = True,
) -> PolicySpec:
    """
    Parse one policy's YAML block into the appropriate
    :class:`PolicySpec` subclass.

    Dispatches on the ``type:`` discriminator
    (``"function"``, ``"prompt"``, or ``"label"``).

    :param name: YAML key for this policy, used in error
        messages and recorded on the spec.
    :param data: Raw mapping from YAML (the value beneath
        ``policies.<name>:``).
    :param expand_env: Propagated for any nested ``llm:``
        connection overrides.
    :returns: A concrete ``PolicySpec`` subclass instance.
    :raises OmnigentError: On malformed data or unknown
        policy type.
    """
    del expand_env  # Was used by _parse_prompt_policy (removed).
    if not isinstance(data, dict):
        raise OmnigentError(
            f"policy {name!r}: must be a mapping, got {type(data).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    policy_type = data.get("type")
    if policy_type is None:
        raise OmnigentError(
            f"policy {name!r}: missing required field `type` (must be 'function')",
            code=ErrorCode.INVALID_INPUT,
        )
    if policy_type == "prompt":
        raise OmnigentError(
            f"policy {name!r}: type 'prompt' is no longer supported. "
            f"Use type 'function' with handler "
            f"'omnigent.policies.builtins.prompt.prompt_policy' instead.",
            code=ErrorCode.INVALID_INPUT,
        )
    base_kwargs = _parse_policy_base_fields(name, data, is_function=policy_type == "function")
    if policy_type == "function":
        return _parse_function_policy(name, data, base_kwargs)
    raise OmnigentError(
        f"policy {name!r}: unknown type {policy_type!r} (must be 'function')",
        code=ErrorCode.INVALID_INPUT,
    )


def _parse_policy_base_fields(
    name: str,
    data: dict[str, Any],
    *,
    is_function: bool = False,
) -> dict[str, Any]:
    """
    Parse the fields every policy type shares.

    Factored out of ``_parse_policy_spec`` so the dispatch
    function stays small. Fields: ``name``, ``on`` (with
    the ``[request, response]`` default per POLICIES.md §3.1),
    ``condition``, and per-policy ``ask_timeout`` override.

    For ``type: function`` policies (``is_function=True``) the
    ``on`` field is ignored — the callable self-selects which
    events to handle by returning ALLOW for events it doesn't act on.

    :param name: Enclosing policy name.
    :param data: Raw YAML mapping for this policy.
    :param is_function: ``True`` when parsing a ``type: function``
        policy. Ignores the ``on:`` field and sets ``on=None``.
    :returns: Kwargs dict ready to splat into any
        :class:`PolicySpec` subclass constructor.
    """
    if is_function:
        # ``on:`` is ignored for function policies — the callable self-selects
        # which events to handle by returning ALLOW for events it doesn't act on.
        on_value = None
    else:
        on_value = _parse_on(data.get("on", ["request", "response"]), policy_name=name)
    return {
        "name": name,
        "on": on_value,
        "condition": _parse_condition(data.get("condition"), policy_name=name),
        "ask_timeout": _parse_policy_ask_timeout(
            data.get("ask_timeout"),
            policy_name=name,
        ),
    }


def _parse_function_policy(
    name: str,
    data: dict[str, Any],
    base_kwargs: dict[str, Any],
) -> FunctionPolicySpec:
    """
    Parse a ``type: function`` policy block.

    :param name: Enclosing policy name (error messages +
        recorded on the spec).
    :param data: Raw YAML mapping for this policy.
    :param base_kwargs: Pre-parsed fields shared across
        policy types (``name``, ``on``, ``condition``,
        ``ask_timeout``).
    :returns: A populated :class:`FunctionPolicySpec`.
    :raises OmnigentError: On missing ``function:`` field
        or malformed ``action`` / ``set_labels`` values.
    """
    # Accept both ``function:`` and ``handler:`` for the callable path.
    # ``handler`` is the proto/service-policies convention; ``function``
    # is the original omnigent YAML convention.
    function_raw = data.get("function") or data.get("handler")
    if function_raw is None:
        raise OmnigentError(
            f"policy {name!r}: `function` policies require a `function:` or `handler:` field",
            code=ErrorCode.INVALID_INPUT,
        )
    action = _parse_action_list(data["action"], policy_name=name) if "action" in data else None
    set_labels = (
        _parse_writable_labels(data["set_labels"], policy_name=name)
        if "set_labels" in data
        else None
    )
    config = data.get("config")
    if config is not None and not isinstance(config, dict):
        raise OmnigentError(
            f"policy {name!r}: 'config' must be a dict, got {type(config).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return FunctionPolicySpec(
        **base_kwargs,
        function=_parse_function_ref(function_raw, policy_name=name),
        action=action,
        set_labels=set_labels,
        config=config,
    )


def _parse_on(
    raw: Any,
    *,
    policy_name: str,
) -> list[PhaseSelector]:
    """
    Parse a policy's ``on:`` list into :class:`PhaseSelector`
    entries.

    YAML shapes:
    - ``"request"`` → wildcard selector for the REQUEST phase.
    - ``"tool_call:web_search"`` → TOOL_CALL narrowed to
      one tool name.

    Tool-name narrowing is rejected on REQUEST / RESPONSE phases
    (only meaningful for tool_call / tool_result).

    :param raw: The ``on:`` value from YAML. Must be a
        non-empty list of strings.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: List of :class:`PhaseSelector` entries, one
        per YAML list element.
    :raises OmnigentError: On empty list, unknown phase,
        or tool-narrowing on a non-tool phase.
    """
    if not isinstance(raw, list):
        raise OmnigentError(
            f"policy {policy_name!r}: `on:` must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        # POLICIES.md §13: empty `on:` creates a policy that
        # never fires — reject at spec load.
        raise OmnigentError(
            f"policy {policy_name!r}: `on:` must contain at least one "
            f"phase selector (empty list would create a policy that "
            f"never fires)",
            code=ErrorCode.INVALID_INPUT,
        )
    return [_parse_on_entry(entry, policy_name=policy_name) for entry in raw]


def _parse_on_entry(
    entry: Any,
    *,
    policy_name: str,
) -> PhaseSelector:
    """
    Parse one entry of a policy's ``on:`` list.

    Handles both forms: bare ``"<phase>"`` (wildcard) and
    ``"<phase>:<tool_name>"`` (tool-narrowed). Tool narrowing
    is rejected on phases other than TOOL_CALL / TOOL_RESULT.

    :param entry: One YAML list element — must be a string.
    :param policy_name: Enclosing policy name, used in error
        messages.
    :returns: A populated :class:`PhaseSelector`.
    :raises OmnigentError: On non-string entry, empty
        tool-name suffix, unknown phase, or tool narrowing
        on a non-tool phase.
    """
    if not isinstance(entry, str):
        raise OmnigentError(
            f"policy {policy_name!r}: `on:` entries must be strings, got {type(entry).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if ":" not in entry:
        return PhaseSelector(phase=_resolve_phase(entry, entry, policy_name=policy_name))
    phase_str, tool_name = entry.split(":", 1)
    if not tool_name:
        raise OmnigentError(
            f"policy {policy_name!r}: empty tool name in on-selector {entry!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    phase = _resolve_phase(phase_str, entry, policy_name=policy_name)
    if phase not in (Phase.TOOL_CALL, Phase.TOOL_RESULT):
        raise OmnigentError(
            f"policy {policy_name!r}: phase {phase.value!r} "
            f"cannot be narrowed by tool name; tool filters "
            f"only apply to tool_call / tool_result",
            code=ErrorCode.INVALID_INPUT,
        )
    return PhaseSelector(phase=phase, tool_name=tool_name)


def _resolve_phase(
    phase_str: str,
    context: str,
    *,
    policy_name: str,
) -> Phase:
    """
    Resolve a phase-string into a :class:`Phase` enum.

    :param phase_str: The phase part of the selector
        (before any ``:``), e.g. ``"tool_call"``.
    :param context: Full on-selector value, used verbatim in
        the error message so the author can see which
        element failed, e.g. ``"tool_call:web_search"``.
    :param policy_name: Enclosing policy name, for error
        messages.
    :returns: The resolved :class:`Phase`.
    :raises OmnigentError: When *phase_str* is not a
        valid phase.
    """
    try:
        return Phase(phase_str)
    except ValueError as exc:
        raise OmnigentError(
            f"policy {policy_name!r}: unknown phase {phase_str!r} in {context!r}"
            if context != phase_str
            else f"policy {policy_name!r}: unknown phase {phase_str!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _parse_condition(
    raw: Any,
    *,
    policy_name: str,
) -> dict[str, str | list[str]] | None:
    """
    Parse a policy's ``condition:`` label-gate.

    Values are coerced to strings — label storage is always
    string-valued, and a YAML author writing
    ``condition: {integrity: 0}`` (unquoted int) would
    otherwise produce a silent runtime mismatch against the
    stored ``"0"``. The coercion matches omnigent parity
    for label values.

    :param raw: The ``condition:`` value from YAML, or
        ``None`` / absent.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: Dict mapping key → string value or list of
        string values. ``None`` when *raw* is absent OR when
        *raw* is an empty dict — both mean "always match."
    :raises OmnigentError: On a non-dict value.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"policy {policy_name!r}: `condition:` must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        # Empty condition matches everything — equivalent to
        # omitting the field. Treated identically by returning
        # ``None`` here so downstream label-gate evaluation
        # takes the always-match short-circuit. (Earlier
        # revisions rejected ``{}`` as a typo guard; the guard
        # produced false positives on policies whose author
        # intended "match any labels, filter only by ``on:``".)
        return None
    coerced: dict[str, str | list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            coerced[str(key)] = [str(v) for v in value]
        else:
            coerced[str(key)] = str(value)
    return coerced


def _parse_action_list(
    raw: Any,
    *,
    policy_name: str,
) -> list[PolicyAction]:
    """
    Parse a policy's ``action:`` whitelist into a list of
    :class:`PolicyAction` enums.

    Accepts a bare string (single-element list sugar) or a
    list of strings. Validates each entry against the enum.

    :param raw: The ``action:`` value from YAML.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: List of :class:`PolicyAction` values.
    :raises OmnigentError: On empty list or unknown
        action value.
    """
    if isinstance(raw, str):
        strings = [raw]
    elif isinstance(raw, list):
        strings = [str(s) for s in raw]
    else:
        raise OmnigentError(
            f"policy {policy_name!r}: `action:` must be a string or "
            f"list of strings, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not strings:
        raise OmnigentError(
            f"policy {policy_name!r}: `action:` list must be non-empty",
            code=ErrorCode.INVALID_INPUT,
        )
    actions: list[PolicyAction] = []
    for s in strings:
        try:
            actions.append(PolicyAction(s))
        except ValueError as exc:
            raise OmnigentError(
                f"policy {policy_name!r}: invalid action {s!r} "
                f"(must be one of 'allow', 'ask', 'deny')",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    return actions


def _parse_writable_labels(
    raw: Any,
    *,
    policy_name: str,
) -> list[str] | None:
    """
    Parse a policy's ``set_labels:`` whitelist (list form —
    used on PromptPolicy and FunctionPolicy).

    :param raw: The ``set_labels:`` list of allowed label
        keys (or ``None`` / absent).
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: List of allowed label keys, or ``None`` when
        *raw* is absent.
    :raises OmnigentError: When *raw* is not a list.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"policy {policy_name!r}: `set_labels:` must be a list "
            f"of label keys, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return [str(k) for k in raw]


def _parse_function_ref(
    raw: Any,
    *,
    policy_name: str,
) -> FunctionRef:
    """
    Parse a ``function:`` YAML value into a :class:`FunctionRef`.

    Two accepted shapes:

    - Bare string: dotted import path of the evaluator
      callable.
    - Dict: ``{path: ..., arguments: {...}}`` — path resolves
      to a factory called with ``arguments`` kwargs at
      workflow start.

    :param raw: The raw ``function:`` value from YAML.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: A populated :class:`FunctionRef`.
    :raises OmnigentError: On malformed shape — non-string
        path, missing path in dict form, non-dict
        ``arguments``.
    """
    if isinstance(raw, str):
        if not raw:
            raise OmnigentError(
                f"policy {policy_name!r}: `function:` path must be non-empty",
                code=ErrorCode.INVALID_INPUT,
            )
        return FunctionRef(path=raw, arguments=None)
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"policy {policy_name!r}: `function:` must be a dotted-path "
            f"string or a dict with {{path, arguments}}, got "
            f"{type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise OmnigentError(
            f"policy {policy_name!r}: `function.path` must be a non-empty dotted-path string",
            code=ErrorCode.INVALID_INPUT,
        )
    args = raw.get("arguments")
    if args is not None and not isinstance(args, dict):
        raise OmnigentError(
            f"policy {policy_name!r}: `function.arguments` must be a "
            f"mapping (or omitted), got {type(args).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return FunctionRef(path=path, arguments=args)


def _parse_policy_ask_timeout(
    raw: Any,
    *,
    policy_name: str,
) -> int | None:
    """
    Parse a per-policy ``ask_timeout:`` override.

    ``None`` / absent = fall back to the guardrails-level
    default. Values ``<= 0`` are rejected (POLICIES.md §13).

    :param raw: The ``ask_timeout:`` value from YAML.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: Integer override in seconds, or ``None`` when
        *raw* is absent.
    :raises OmnigentError: On non-integer or non-positive
        value.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise OmnigentError(
            f"policy {policy_name!r}: `ask_timeout` must be an integer, got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if value <= 0:
        raise OmnigentError(
            f"policy {policy_name!r}: `ask_timeout` must be > 0 "
            f"(omit ASK from the policy's action list for instant-DENY)",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


def parse_default_policies(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> list[PolicySpec]:
    """
    Parse the ``policies:`` mapping from the server ``--config``
    YAML into a list of :class:`PolicySpec` instances.

    The YAML shape is a mapping keyed by policy name — the same grammar
    as ``guardrails.policies:`` in an agent spec:

    .. code-block:: yaml

        policies:
          admin__audit_tool_calls:
            type: function
            function: myorg.policies.audit
          admin__deny_pii_output:
            type: prompt
            on: [response]
            action: [allow, deny]
            prompt: "Deny if the response contains PII..."

    For ``type: function`` policies the ``on:`` field is ignored —
    the callable self-selects which phases to act on.

    Returns an empty list when *raw* is ``None`` or an empty mapping —
    the server starts up with no default policies in that case.

    :param raw: The ``policies:`` value from the server config
        YAML, e.g. ``{"admin__audit": {"type": "function",
        "function": "myorg.policies.audit"}}``. ``None`` when the key
        is absent.
    :param expand_env: Whether to expand ``${VAR}`` references in any
        nested ``llm.connection`` blocks (PromptPolicy LLM overrides).
        ``True`` for production; ``False`` for validation contexts
        where env vars may not be set.
    :returns: Ordered list of :class:`PolicySpec` instances ready for
        the policy engine. Empty list when *raw* is ``None`` or ``{}``.
    :raises OmnigentError: On any malformed policy entry — unknown
        type, missing required field, invalid phase selector, etc.
    """
    if not raw:
        return []
    return _parse_policies(raw, expand_env=expand_env) or []


def parse_server_llm(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> LLMConfig | None:
    """
    Parse the ``llm:`` block from the server ``--config`` YAML.

    Delegates to :func:`_parse_llm` — same grammar as the agent-level
    ``llm:`` block. Exposed as a public entry point so the CLI can
    call it without reaching into parser internals.

    :param raw: The ``llm:`` value from the server config YAML,
        e.g. ``{"model": "openai/gpt-4o-mini", "connection": {"api_key": "..."}}``.
        ``None`` when the key is absent.
    :param expand_env: Whether to expand ``${VAR}`` references in
        the connection block. ``True`` for production; ``False``
        for validation contexts where env vars may not be set.
    :returns: A :class:`LLMConfig` or ``None``.
    """
    return _parse_llm(raw, expand_env=expand_env)
