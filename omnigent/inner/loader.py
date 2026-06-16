"""YAML / dict loader for AgentDef."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeAlias

import yaml

from .datamodel import (
    AgentDef,
    ExecutorSpec,
    MemoryConfig,
    OSEnvSandboxSpec,
    OSEnvSpec,
    ParamDef,
    TerminalEnvSpec,
)
from .policies import (
    FunctionPolicy,
    Policy,
    PromptPolicy,
)
from .tools import (
    AgentTool,
    CancellableFunctionTool,
    FunctionTool,
    HandoffTool,
    InheritedTool,
    MCPTool,
    SelfAgentTool,
    SkillTool,
    Tool,
)

# Config-dict shape from agent YAML files. The schema is a nested, open-ended
# tree (tools, policies, executor, os_env, sandbox — each with variant-typed
# children); the loader validates shape field-by-field via isinstance checks,
# so a TypedDict would duplicate that validation without buying us more safety.
YamlData: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Dynamically-loaded Python callables resolved from `callable: mod.func`
# paths in YAML; the loader has no way to know their signatures.
DynamicCallable: TypeAlias = Callable[..., object]  # type: ignore[explicit-any]


class _OmnigentYamlLoader(yaml.SafeLoader):
    """YAML loader with YAML 1.2-style booleans.

    PyYAML's default YAML 1.1 resolver treats unquoted keys like ``on`` as a
    boolean. That breaks policy definitions such as ``on: [tool_call]``.
    Keep ``true``/``false`` boolean parsing, but stop treating ``on``/``off``
    and similar legacy literals as booleans.
    """


_OmnigentYamlLoader.yaml_implicit_resolvers = {
    key: value[:] for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for key, resolvers in list(_OmnigentYamlLoader.yaml_implicit_resolvers.items()):
    _OmnigentYamlLoader.yaml_implicit_resolvers[key] = [
        (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
# types-PyYAML declares add_implicit_resolver without return annotations.
_OmnigentYamlLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_agent_def(
    path_or_dict: str | Path | YamlData,
    *,
    enforce_handler_allowlist: bool = False,
) -> AgentDef:
    """Load an AgentDef from a YAML file path or a raw dict.

    When *path_or_dict* is a path, the YAML's ``instructions:``
    field (if present) is resolved relative to the YAML's parent
    directory: a value that names an existing file gets its
    contents read in, and any other string is treated as inline
    text. When *path_or_dict* is a raw dict, the same string is
    treated as inline text — there is no anchor for path
    resolution. See :func:`_resolve_instructions` for the rules.

    :param path_or_dict: A YAML file path or already-parsed dict.
    :param enforce_handler_allowlist: When ``True``, reject any
        ``type: function`` policy whose ``handler:`` / ``callable:``
        dotted path is not a registered policy handler, *before*
        ``_parse_agent_def`` resolves and (for factory policies)
        **calls** it. This is the guard for the untrusted
        agent-bundle upload path: ``omnigent.spec.load`` routes a
        single-file omnigent YAML bundle here during
        ``validate_agent_bundle``, and the loader executes policy
        factories at parse time, so an uploaded
        ``handler: subprocess.Popen`` would otherwise run during
        validation. Defaults to ``False`` so trusted callers (local
        ``omnigent run``, operator specs, the CLI) keep working with
        custom handlers — the operator already has code execution, so
        the restriction would add no security there.
    """
    if isinstance(path_or_dict, (str, Path)):
        path = Path(path_or_dict)
        with open(path) as f:
            data = yaml.load(f, Loader=_OmnigentYamlLoader)
        instructions_root: Path | None = path.parent
    else:
        data = path_or_dict
        instructions_root = None
    if enforce_handler_allowlist:
        _reject_unregistered_policy_handlers(data)
    return _parse_agent_def(data, instructions_root=instructions_root)


def _reject_unregistered_policy_handlers(data: YamlData) -> None:
    """Reject ``type: function`` policies whose handler is not registered.

    Scans the raw YAML ``policies:`` mapping for handler dotted paths
    that are not in the policy registry and raises before any import or
    factory call. Tool ``callable:`` paths are intentionally *not*
    scanned — they are a separate surface and are not invoked at parse
    time. See :func:`load_agent_def` for why this only runs on the
    untrusted bundle-upload path.

    :param data: The raw agent YAML dict (pre-parse). Non-dict input
        (malformed YAML) is ignored here and left for the parser to
        reject.
    :raises ValueError: If a function policy names an unregistered
        handler, e.g. ``"subprocess.Popen"``.
    """
    from omnigent.policies.registry import is_registered_handler

    if not isinstance(data, dict):
        return
    policies = data.get("policies")
    if not isinstance(policies, dict):
        return
    for pname, pdata in policies.items():
        if not isinstance(pdata, dict):
            continue
        if pdata.get("type", "function") != "function":
            continue
        handler = pdata.get("handler") or pdata.get("callable")
        if isinstance(handler, str) and not is_registered_handler(handler):
            raise ValueError(
                f"Policy {pname!r}: handler {handler!r} is not a registered policy "
                f"handler. Uploaded agent bundles may only use handlers from the "
                f"policy registry; a server admin must add custom handlers via the "
                f"'policy_modules' config."
            )


def _read_contained_file(root: Path, value: str) -> str | None:
    """
    Read an *instructions_root*-relative file named by *value*, if contained.

    Mirrors :func:`omnigent.spec.parser._read_contained_file`: resolves
    symlinks and ``..`` and confirms the target stays within *root* before
    reading, so a crafted ``instructions: ../../etc/passwd`` in an uploaded
    bundle cannot read files outside the bundle. Returns ``None`` for a
    non-contained or non-existent path so the caller falls back to treating
    *value* as inline text.

    :param root: Directory the value is anchored to, e.g. the spec file's
        parent directory.
    :param value: The single-line ``instructions:`` value, e.g.
        ``"AGENTS.md"``.
    :returns: The file contents if *value* names a contained file, else
        ``None``.
    """
    candidate = root / value
    try:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root.resolve()) and resolved.is_file():
            return resolved.read_text()
    except OSError:
        # Path too long or invalid characters — fall through to inline text.
        pass
    return None


def _resolve_instructions(
    raw_value: object,
    instructions_root: Path | None,
) -> str | None:
    """
    Resolve the ``instructions:`` field to a system-prompt string.

    Mirrors :func:`omnigent.spec.parser._resolve_instructions`
    so omnigent-flavored YAMLs and native Omnigent YAMLs treat the
    field identically:

    - Single-line value that names an existing file relative to
      *instructions_root*: the file's contents are read.
    - Multi-line value (contains ``\\n``): treated as inline text.
    - File-path-shaped value that doesn't resolve: silently
      treated as inline text. Matches native Omnigent behavior so users
      who type ``instructions: AGENTS.md`` and forget the file
      get the literal string back rather than a misleading error.
    - ``None`` / non-string raw values return ``None``.

    :param raw_value: The raw YAML value at the ``instructions:``
        key, or ``None`` if the key was absent.
    :param instructions_root: Directory to anchor relative path
        lookups. ``None`` skips the file-read attempt and treats
        every value as inline text — used when the loader has no
        on-disk anchor (raw-dict input path).
    :returns: The resolved instruction text, or ``None`` if no
        ``instructions:`` was supplied.
    """
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return None
    if "\n" in raw_value:
        return raw_value
    if instructions_root is not None:
        contained = _read_contained_file(instructions_root, raw_value)
        if contained is not None:
            return contained
    return raw_value


def _parse_agent_def(
    data: YamlData,
    *,
    instructions_root: Path | None = None,
) -> AgentDef:
    agent = AgentDef()
    # ``AgentDef.name`` and ``AgentDef.prompt`` are both ``str | None``;
    # missing YAML keys flow through as ``None``.
    agent.name = data.get("name")
    agent.prompt = data.get("prompt")
    # ``instructions:`` is resolved into the agent's system-prompt
    # text right here (file path → contents, or inline string).
    # Translator code downstream only sees the resolved string
    # in ``agent.instructions`` and the raw user-supplied
    # ``agent.prompt`` — it doesn't have to re-walk the path.
    agent.instructions = _resolve_instructions(data.get("instructions"), instructions_root)
    agent.input_type = data.get("input_type")
    agent.output_type = data.get("output_type")
    if "async_enabled" in data:
        agent.async_enabled = data["async_enabled"]
    elif "async" in data:
        agent.async_enabled = data["async"]
    if "cancellable" in data:
        agent.cancellable = bool(data["cancellable"])
    agent.runtime = data.get("runtime", False)
    agent.timers = data.get("timers", False)
    agent.spawn = data.get("spawn", False)
    agent.os_env = _parse_os_env_spec(data.get("os_env"))

    # Executor
    executor_data = data.get("executor")
    if executor_data:
        agent.executor = _parse_executor_spec(executor_data)

    # Params
    for pname, pdata in data.get("params", {}).items():
        if isinstance(pdata, dict):
            agent.params[pname] = ParamDef(
                type=pdata.get("type", "string"),
                description=pdata.get("description"),
                default=pdata.get("default"),
            )
        else:
            agent.params[pname] = ParamDef(type="string", default=pdata)

    # Tools
    for tname, tdata in data.get("tools", {}).items():
        agent.tools[tname] = _parse_tool(tname, tdata)

    # Policies
    for pname, pdata in data.get("policies", {}).items():
        agent.policies[pname] = _parse_policy(pname, pdata)

    # Memories
    for mname, mdata in data.get("memories", {}).items():
        if isinstance(mdata, dict):
            agent.memories[mname] = MemoryConfig(
                scope=mdata.get("scope", "per_session"),
            )
        else:
            agent.memories[mname] = MemoryConfig()

    # Labels
    agent.labels = {str(k): str(v) for k, v in data.get("labels", {}).items()}

    # ASK timeout
    if "ask_timeout" in data:
        agent.ask_timeout = float(data["ask_timeout"]) if data["ask_timeout"] is not None else None

    # Label schema
    from .datamodel import LabelSchemaRule

    _MONOTONIC_ALIASES = {"up": "max", "down": "min"}
    for ls_name, ls_data in data.get("label_schema", {}).items():
        if isinstance(ls_data, dict):
            raw_monotonic = str(ls_data.get("monotonic", "none"))
            monotonic = _MONOTONIC_ALIASES.get(raw_monotonic, raw_monotonic)
            agent.label_schema[str(ls_name)] = LabelSchemaRule(
                values=[str(v) for v in ls_data.get("values", [])],
                monotonic=monotonic,
            )

    # Policy transparency
    if "policy_transparency" in data:
        agent.policy_transparency = bool(data["policy_transparency"])

    # Terminals
    for term_name, term_data in data.get("terminals", {}).items():
        agent.terminals[str(term_name)] = _parse_terminal_env_spec(term_data)
    # Cross-field validation: a terminal with ``allow_sandbox_override``
    # lets the LLM pass an arbitrary ``sandbox`` arg to
    # ``sys_terminal_launch``. The override only mutates ``sandbox.type``
    # — egress_rules stay on the policy object but an override to
    # ``none`` (an accepted override value) can't enforce them, so the
    # LLM effectively drops the egress allow-list. Reject the
    # combination at parse time rather than silently accept a spec
    # that's only as strong as the LLM lets it be.
    for term_name, term_spec in agent.terminals.items():
        if not term_spec.allow_sandbox_override:
            continue
        effective_sandbox = _effective_terminal_sandbox(term_spec, agent.os_env)
        if effective_sandbox is not None and effective_sandbox.egress_rules:
            raise ValueError(
                f"terminal {term_name!r}: allow_sandbox_override=true is "
                "incompatible with egress_rules on the effective sandbox. "
                "An override of sandbox.type to 'none' "
                "drops hard network enforcement while egress_rules remain "
                "as inert decoration on the policy, so the LLM could "
                "silently bypass the network allow-list. Either remove "
                "allow_sandbox_override or remove egress_rules from the "
                "effective sandbox."
            )

    # Workflow
    agent.workflow = data.get("workflow")

    # Metadata
    agent.metadata = data.get("metadata", {})

    return agent


# ---------------------------------------------------------------------------
# Tool parsing
# ---------------------------------------------------------------------------


def _parse_tool(name: str, data: str | YamlData) -> Tool:
    if isinstance(data, str):
        if data == "inherit":
            return InheritedTool(name=name)
        if data == "self":
            # ``tools.<name>: self`` shorthand: the sub-agent's spec
            # is a clone of the parent's. See
            # :class:`SelfAgentTool` — the translator materializes
            # the sub-spec at translation time.
            return SelfAgentTool(name=name)
        return FunctionTool(name=name, description=data)

    if not isinstance(data, dict):
        return FunctionTool(name=name, description=str(data))

    tool_type = data.get("type", "function")

    if tool_type == "function":
        # Reject typos like ``runtime: clinet`` at load time.
        runtime_raw = data.get("runtime", "server")
        if runtime_raw not in ("server", "client"):
            raise ValueError(
                f"Tool '{name}': invalid runtime {runtime_raw!r}; must be 'server' or 'client'.",
            )
        tool = FunctionTool(
            name=name,
            description=data.get("description"),
            catalog_path=data.get("catalog_path"),
            input_schema=data.get("parameters") or data.get("input_schema"),
            cancellable=bool(data.get("cancellable", False)),
            runtime=runtime_raw,
            warehouse_id=data.get("warehouse_id"),
        )
        callable_path = data.get("callable")
        if callable_path and isinstance(callable_path, str):
            # Catch the conflict before the validator resolves the
            # import, so client-runtime YAMLs never load the module.
            if runtime_raw == "client":
                raise ValueError(
                    f"Tool '{name}': 'runtime: client' tools must not "
                    f"declare a 'callable:'. The SDK consumer provides "
                    f"the implementation at stream-start time; declaring "
                    f"a server-side callable here would silently never "
                    f"run.",
                )
            tool.callable = _resolve_callable(callable_path)
        return tool

    if tool_type == "cancellable_function":
        raw_runner = data.get("runner")
        return CancellableFunctionTool(
            name=name,
            description=data.get("description"),
            input_schema=data.get("parameters") or data.get("input_schema"),
            runner=_resolve_callable(raw_runner) if isinstance(raw_runner, str) else None,
            cancellable=bool(data.get("cancellable", True)),
        )

    if tool_type == "mcp":
        # ``profile`` can be declared directly (``profile: myprof``)
        # or inside an ``auth:`` block (``auth: {type: databricks,
        # profile: myprof}``). The ``auth:`` block is the preferred
        # form — it matches the executor auth shape.
        mcp_profile = data.get("profile")
        raw_auth = data.get("auth")
        if mcp_profile is None and isinstance(raw_auth, dict):
            if str(raw_auth.get("type", "")) == "databricks":
                mcp_profile = raw_auth.get("profile")
        return MCPTool(
            name=name,
            description=data.get("description"),
            url=data.get("url"),
            command=data.get("command"),
            args=data.get("args"),
            env=data.get("env"),
            tools=data.get("tools"),
            tool_name=data.get("tool_name"),
            profile=mcp_profile,
            databricks_server=data.get("databricks_server"),
            headers=data.get("headers"),
        )

    if tool_type == "agent":
        # ``spec: self`` dict form: the sub-agent's spec is a clone
        # of the parent's. Other agent fields (prompt, tools,
        # executor, os_env, ...) would conflict with the cloned
        # parent, so reject them up-front rather than silently
        # ignoring them.
        if data.get("spec") == "self":
            for conflicting in (
                "prompt",
                "tools",
                "executor",
                "os_env",
                "pass_history",
                "pass_histories",
                "max_sessions",
            ):
                if conflicting in data:
                    raise ValueError(
                        f"Tool '{name}': 'spec: self' cannot be combined with "
                        f"'{conflicting}'. The sub-agent's configuration is "
                        f"cloned from the parent — declare a regular "
                        f"'type: agent' tool with explicit fields if you "
                        f"need overrides."
                    )
            return SelfAgentTool(
                name=name,
                description=data.get("description"),
            )

        sub_tools: dict[str, Tool] = {}
        for sname, sdata in data.get("tools", {}).items():
            sub_tools[sname] = _parse_tool(sname, sdata)
        raw_max_sessions = data.get("max_sessions")
        max_sessions: int | None = None
        if raw_max_sessions is not None:
            if not isinstance(raw_max_sessions, int) or isinstance(raw_max_sessions, bool):
                raise ValueError(
                    f"Tool '{name}': 'max_sessions' must be an integer, "
                    f"got {type(raw_max_sessions).__name__!r} ({raw_max_sessions!r})."
                )
            if raw_max_sessions < 1:
                raise ValueError(
                    f"Tool '{name}': 'max_sessions' must be >= 1, got {raw_max_sessions!r}."
                )
            max_sessions = raw_max_sessions
        return AgentTool(
            name=name,
            description=data.get("description"),
            prompt=data.get("prompt"),
            tools=sub_tools,
            executor=_parse_executor_spec(data.get("executor")),
            os_env=(
                data.get("os_env")
                if data.get("os_env") == "inherit"
                else _parse_os_env_spec(data.get("os_env"))
            ),
            pass_history=data.get("pass_history", False),
            pass_histories=data.get("pass_histories"),
            max_sessions=max_sessions,
        )

    if tool_type == "inherit":
        return InheritedTool(name=name)

    if tool_type == "skill":
        # ``content`` is an alias for ``path`` — either is accepted. ``None``
        # when neither is configured; ``SkillTool.path`` is ``str | None``.
        path_value = data.get("content") or data.get("path")
        if path_value is not None and not isinstance(path_value, str):
            raise TypeError(f"Tool {name!r}: skill 'path'/'content' must be a string")
        return SkillTool(
            name=name,
            description=data.get("description"),
            path=path_value,
        )

    if tool_type == "handoff":
        return HandoffTool(
            name=name,
            description=data.get("description"),
            target_agent=data.get("target_agent"),
            pass_history=data.get("pass_history", True),
            bidirectional=data.get("bidirectional", True),
        )

    # Default: treat as function.
    return FunctionTool(name=name, description=data.get("description"))


# ---------------------------------------------------------------------------
# Policy parsing
# ---------------------------------------------------------------------------


def _parse_policy(name: str, data: YamlData | str | bool | None) -> Policy:
    if not isinstance(data, dict):
        return Policy(name=name)

    policy_type = data.get("type", "function")
    # Default ``on`` for function policies is all four phases — the callable
    # self-selects which to act on by returning ALLOW for events it ignores.
    # Prompt and label policies retain their old defaults (see their branches).
    _function_all_phases = ["request", "response", "tool_call", "tool_result"]
    on = data.get(
        "on", _function_all_phases if policy_type == "function" else ["request", "response"]
    )

    if policy_type == "function":
        callable_obj = None
        # Accept both ``handler:`` (new name) and ``callable:`` (legacy alias).
        callable_path = data.get("handler") or data.get("callable")
        if callable_path and isinstance(callable_path, str):
            callable_obj = _resolve_callable(callable_path)
        has_factory_params = "factory_params" in data
        factory_params = data.get("factory_params", {})
        factory: DynamicCallable | None = None
        if has_factory_params and callable_obj is not None:
            factory = callable_obj
            factory_result = callable_obj(**factory_params)
            if not callable(factory_result):
                raise ValueError(
                    f"Policy factory '{callable_path}' returned non-callable: "
                    f"{type(factory_result)}"
                )
            callable_obj = factory_result
        return FunctionPolicy(
            name=name,
            on=on,
            callable=callable_obj,
            # Only carry factory_params when the factory was
            # successfully resolved. If _resolve_callable returned
            # None (import error on the server side), passing
            # factory_params without factory would trip
            # FunctionPolicy.__post_init__'s invariant check.
            factory_params=factory_params if factory is not None else {},
            factory=factory,
        )

    if policy_type == "prompt":
        return PromptPolicy(
            name=name,
            on=on,
            # `None` means no prompt was configured; PromptPolicy.evaluate()
            # fails closed in that case.
            prompt=data.get("prompt"),
            executor=_parse_executor_spec(data.get("executor")),
            allow_set_labels=bool(data.get("allow_set_labels", False)),
            allowed_label_keys=(
                [str(key) for key in data.get("allowed_label_keys", [])]
                if isinstance(data.get("allowed_label_keys"), list)
                else None
            ),
        )

    return Policy(name=name, on=on)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_callable(dotted_path: str) -> DynamicCallable | None:
    """Try to import a dotted Python path like ``mypackage.module.func``.

    Returns ``None`` on failure (we don't want the loader to crash just
    because a function isn't importable in this environment).
    """
    parts = dotted_path.rsplit(".", 1)
    if len(parts) != 2:
        return None
    module_path, attr_name = parts
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, attr_name, None)
    except (ImportError, AttributeError):
        return None


def _parse_executor_spec(data: YamlData | str | bool | None) -> ExecutorSpec | None:
    if not data:
        return None
    if isinstance(data, str):
        return ExecutorSpec(model=data)
    if isinstance(data, dict):
        # ``ExecutorSpec.{model,harness,profile}`` are ``str | None``;
        # missing keys map to ``None`` directly. ``data.get`` happens to
        # already return ``None`` for missing keys, so the assignment
        # flows through unchanged.
        return ExecutorSpec(
            model=data.get("model"),
            harness=data.get("harness"),
            profile=data.get("profile"),
        )
    return None


def _parse_os_env_spec(data: YamlData | str | bool | None) -> OSEnvSpec | None:
    if data is None:
        return None
    if isinstance(data, str):
        return OSEnvSpec(type=data)
    if not isinstance(data, dict):
        raise TypeError("os_env must be a string or mapping")

    sandbox_data = data.get("sandbox")
    sandbox = None
    if sandbox_data is not None:
        sandbox = _parse_os_env_sandbox_spec(sandbox_data)
    fork = bool(data.get("fork", False))
    start_in_scratch = bool(data.get("start_in_scratch", False))
    # Mirror the Omnigent YAML parser's cross-field validation
    # (omnigent/spec/parser.py:_parse_os_env). Keeping the two
    # loaders in lockstep prevents a class of "silently weakens
    # the sandbox on the legacy path" bug — an operator who used
    # ``load_agent_def`` would otherwise get past parse time with
    # a spec that ``create_os_environment`` later rejects (or, worse,
    # accepts with weaker semantics than the Omnigent parser would have
    # allowed).
    if start_in_scratch and fork:
        raise ValueError(
            "os_env.start_in_scratch and os_env.fork are mutually exclusive: "
            "fork already provides a writable workspace by copying cwd"
        )
    if start_in_scratch and sandbox is not None and sandbox.type == "none":
        raise ValueError(
            "os_env.start_in_scratch requires an active sandbox; "
            "sandbox.type=none does not create a scratch tmpdir"
        )
    return OSEnvSpec(
        type=data.get("type", "caller_process"),
        cwd=data.get("cwd"),
        sandbox=sandbox,
        fork=fork,
        start_in_scratch=start_in_scratch,
    )


def _effective_terminal_sandbox(
    term_spec: TerminalEnvSpec, agent_os_env: OSEnvSpec | None
) -> OSEnvSandboxSpec | None:
    """Return the sandbox a terminal would end up with at launch time.

    Mirrors the runtime resolution in
    :func:`omnigent.inner.terminal.build_terminal_os_env_spec`: if the
    terminal has its own ``os_env`` spec, that wins; if it inherits
    (``os_env: inherit`` or omitted), the agent's ``os_env`` is used.
    Used at parse time for cross-field validation that needs both
    halves of the spec.
    """
    if isinstance(term_spec.os_env, OSEnvSpec):
        return term_spec.os_env.sandbox
    if agent_os_env is not None:
        return agent_os_env.sandbox
    return None


def _parse_terminal_env_spec(data: YamlData | str | bool | None) -> TerminalEnvSpec:
    if isinstance(data, str):
        return TerminalEnvSpec(command=data)
    if not isinstance(data, dict):
        raise TypeError("terminal spec must be a string or mapping")

    os_env_val = data.get("os_env")
    os_env: OSEnvSpec | str | None = None
    if os_env_val == "inherit":
        os_env = "inherit"
    elif os_env_val is not None:
        os_env = _parse_os_env_spec(os_env_val)

    env_val = data.get("env", {})
    if not isinstance(env_val, dict):
        raise TypeError("terminal 'env' must be a mapping of string -> string")
    env = {str(k): str(v) for k, v in env_val.items()}

    return TerminalEnvSpec(
        command=data.get("command", "bash"),
        args=list(data.get("args", [])),
        env=env,
        os_env=os_env,
        allow_cwd_override=bool(data.get("allow_cwd_override", False)),
        allow_sandbox_override=bool(data.get("allow_sandbox_override", False)),
        log_file=data.get("log_file"),
        scrollback=int(data.get("scrollback", 10000)),
        session_prefix=data.get("session_prefix", "omni_"),
    )


def _parse_os_env_sandbox_spec(data: YamlData | str | bool | None) -> OSEnvSandboxSpec:
    if isinstance(data, str):
        return OSEnvSandboxSpec(type=data)
    if data is False:
        return OSEnvSandboxSpec(type="none")
    if not isinstance(data, dict):
        raise TypeError("os_env.sandbox must be a string, false, or mapping")
    raw_type = data.get("type")
    if raw_type is None:
        # No ``type:`` field -- resolve via the platform default
        # (same behavior as the Omnigent YAML parser, kept in sync so legacy
        # and Omnigent loaders agree on what an "untyped" sandbox block means).
        from .sandbox import _default_sandbox_for_platform

        sandbox_type = _default_sandbox_for_platform().type
    else:
        sandbox_type = raw_type
    egress_rules = data.get("egress_rules")
    # Mirror the Omnigent parser's hard reject of ``egress_rules`` paired with
    # a backend that cannot enforce them at spawn time. Without this
    # check the loader would happily accept a YAML that declares an
    # egress allow-list on ``none``, where the
    # MITM proxy is never wired up — the rules would sit in the spec
    # object as inert decoration while ``curl`` reaches the open
    # internet, looking exactly like the bug class fixed in
    # ``terminal._clone_sandbox_spec``.
    if egress_rules and sandbox_type not in ("linux_bwrap", "darwin_seatbelt"):
        raise ValueError(
            "os_env.sandbox.egress_rules requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) for hard "
            "network enforcement: those backends restrict network access "
            "at spawn time so the MITM proxy is the only egress path. "
            f"Got sandbox.type={sandbox_type!r}."
        )
    allow_private = data.get("egress_allow_private_destinations", False)
    if not isinstance(allow_private, bool):
        raise TypeError(
            "os_env.sandbox.egress_allow_private_destinations must be a "
            f"boolean, got {type(allow_private).__name__}"
        )
    # Secretless credential proxy. Reuse the single canonical parser
    # (``omnigent.spec.parser._parse_credential_proxy``) rather than a
    # second copy so the single-file omnigent-YAML path and the
    # bundle/config.yaml path can never drift — a duplicated parser here
    # is exactly what silently dropped ``credential_proxy`` on this path
    # before. Lazy-imported to avoid an import-time cycle with the spec
    # layer (which imports inner.datamodel). The two cross-field guards
    # below mirror the spec parser so an inert credential_proxy (no
    # hardened backend / no egress rules) is rejected on both paths.
    from omnigent.spec.parser import (
        _credential_proxy_macos_unsupported_reason,
        _parse_credential_proxy,
    )

    credential_proxy = _parse_credential_proxy(data.get("credential_proxy"))
    if credential_proxy is not None and sandbox_type not in ("linux_bwrap", "darwin_seatbelt"):
        raise ValueError(
            "os_env.sandbox.credential_proxy requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) so credentials are "
            "bound to a hardened helper boundary. "
            f"Got sandbox.type={sandbox_type!r}."
        )
    if credential_proxy is not None and not egress_rules:
        raise ValueError(
            "os_env.sandbox.credential_proxy requires os_env.sandbox.egress_rules: "
            "the MITM egress proxy is what swaps the synthetic placeholder for the "
            "real credential and rejects placeholder leaks, so it must be active."
        )
    macos_reason = _credential_proxy_macos_unsupported_reason(credential_proxy, sandbox_type)
    if macos_reason is not None:
        raise ValueError(macos_reason)
    # Defer the absent-field defaults to the dataclass so there is a single
    # source of truth: re-stating literals here (e.g. ``"warn"``, ``50000``)
    # silently drifts the moment the OSEnvSandboxSpec defaults change. An
    # explicit YAML ``null`` is treated the same as a missing key.
    fields = OSEnvSandboxSpec.__dataclass_fields__
    max_entries_raw = data.get("cwd_hidden_scan_max_entries")
    overflow_raw = data.get("cwd_hidden_scan_overflow")
    return OSEnvSandboxSpec(
        type=sandbox_type,
        read_paths=data.get("read_paths"),
        write_paths=(
            list(data["write_paths"])
            if "write_paths" in data and data.get("write_paths") is not None
            else None
        ),
        write_files=(
            list(data["write_files"])
            if "write_files" in data and data.get("write_files") is not None
            else None
        ),
        allow_network=data.get("allow_network", True),
        cwd_allow_hidden=data.get("cwd_allow_hidden"),
        cwd_hidden_scan_max_entries=(
            int(max_entries_raw)
            if max_entries_raw is not None
            else fields["cwd_hidden_scan_max_entries"].default
        ),
        cwd_hidden_scan_overflow=(
            str(overflow_raw)
            if overflow_raw is not None
            else fields["cwd_hidden_scan_overflow"].default
        ),
        env_passthrough=data.get("env_passthrough"),
        egress_rules=egress_rules,
        egress_allow_private_destinations=allow_private,
        credential_proxy=credential_proxy,
    )


def load_agent_def_from_path(path_str: str) -> AgentDef:
    """
    Resolve a CLI target path to an :class:`AgentDef`.

    Supports three shapes:

    - ``foo.yaml`` (single-file omnigent YAML) — parsed directly.
    - ``my-agent/`` (omnigent AGENTSPEC directory with
      ``config.yaml``, or a directory containing exactly one
      omnigent YAML at its root).
    - ``my-agent.tar.gz`` (tarball bundle) — extracted, then same
      directory dispatch.
    """
    path = Path(path_str)
    if path.is_file() and path.suffix not in {".tgz", ".gz", ".tar"}:
        return load_agent_def(path_str)

    if not (path.is_dir() or path.is_file()):
        raise FileNotFoundError(f"agent spec not found: {path}")

    import tempfile

    from omnigent.spec import _find_omnigent_yaml_in_dir

    if path.is_dir():
        resolved_dir = path
        cleanup: tempfile.TemporaryDirectory[str] | None = None
    else:
        from omnigent.spec.tar_utils import extract_safe

        cleanup = tempfile.TemporaryDirectory(prefix="omnigent-bundle-")
        resolved_dir = Path(cleanup.name)
        extract_safe(path.read_bytes(), resolved_dir)

    try:
        config_yaml = resolved_dir / "config.yaml"
        if config_yaml.is_file():
            from omnigent.spec import load as load_agent_spec
            from omnigent.spec.omnigent import (
                agent_spec_to_agent_def,
            )

            agent_spec = load_agent_spec(resolved_dir)
            return agent_spec_to_agent_def(agent_spec)
        single = _find_omnigent_yaml_in_dir(resolved_dir)
        if single is None:
            raise FileNotFoundError(
                f"{path}: directory has neither a config.yaml "
                f"nor a single-file omnigent YAML at its root"
            )
        return load_agent_def(str(single))
    finally:
        if cleanup is not None:
            cleanup.cleanup()
