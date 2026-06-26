"""Agent image spec: parsing, validation, and safe extraction."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from omnigent.errors import ErrorCode, OmnigentError

# Omnigent compat: imported surgically from a dedicated module so
# the integration's tech debt is removable in one shot. See
# omnigent/spec/_omnigent_compat.py.
from omnigent.spec._omnigent_compat import (
    diagnose_yaml_rejection,
    is_omnigent_yaml,
    load_omnigent_yaml,
)
from omnigent.spec.parser import expand_env_vars, parse, parse_default_policies, parse_server_llm
from omnigent.spec.tar_utils import ExtractionError, extract_safe
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    DEFAULT_POLICY_CLASSIFIER_TIMEOUT,
    AgentSpec,
    ApiKeyAuth,
    BuiltinToolConfig,
    DatabricksAuth,
    ExecutorAuth,
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
    SkillSpec,
    ToolRuntime,
    ToolsConfig,
)
from omnigent.spec.validator import ValidationResult, validate

_logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ASK_TIMEOUT",
    "DEFAULT_POLICY_CLASSIFIER_TIMEOUT",
    "AgentSpec",
    "ApiKeyAuth",
    "BuiltinToolConfig",
    "DatabricksAuth",
    "ExecutorAuth",
    "ExecutorSpec",
    "ExtractionError",
    "FunctionPolicySpec",
    "FunctionRef",
    "GuardrailsSpec",
    "InteractionConfig",
    "LLMConfig",
    "LabelDef",
    "LocalToolInfo",
    "MCPServerConfig",
    "ModalityConfig",
    "Phase",
    "PhaseSelector",
    "PolicyAction",
    "PolicySpec",
    "ProviderAuth",
    "RetryPolicy",
    "SkillSpec",
    "ToolRuntime",
    "ToolsConfig",
    "ValidationResult",
    "expand_env_vars",
    "extract_safe",
    "load",
    "materialize_bundle",
    "parse",
    "parse_default_policies",
    "parse_server_llm",
    "validate",
]


def materialize_bundle(source: Path, dest: Path) -> Path:
    """
    Copy a spec source into *dest* as a uniform bundle directory.

    Agent-plane accepts two source shapes: an agent-image directory
    (``config.yaml`` + bundled assets) or a standalone omnigent
    YAML file. Downstream code (``_preregister_agent``,
    ``_prepare_omnigent_yaml_bundle``) always wants to operate on a
    directory it can tar, mutate, or hand to the in-process
    omnigent server.

    Taking the file-vs-directory branch once — here — means every
    caller downstream is uniform: "materialize, then operate on the
    returned path as a directory." No caller has to reinspect the
    input shape.

    :param source: The spec source. Either a directory containing
        ``config.yaml`` (standard omnigent shape) or a standalone
        omnigent YAML file (e.g.
        ``examples/coding_supervisor.yaml``). Must exist.
    :param dest: Destination directory to populate. Created if it
        does not exist; may be empty or already contain the copied
        contents from a prior call (``shutil.copytree`` is invoked
        with ``dirs_exist_ok=True``).
    :returns: *dest*, always as a populated directory. For the
        directory case the contents are a recursive copy of
        *source*. For the file case the YAML is placed at the root
        of *dest* so
        :func:`omnigent.spec._find_omnigent_yaml_in_dir` picks
        it up on a subsequent :func:`load` call.
    :raises FileNotFoundError: If *source* does not exist.
    """
    if source.is_dir():
        shutil.copytree(source, dest, dirs_exist_ok=True)
        return dest
    if source.is_file():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest / source.name)
        return dest
    raise FileNotFoundError(f"source not found: {source}")


def _find_omnigent_yaml_in_dir(root: Path) -> Path | None:
    """
    Return the omnigent YAML inside *root* when the directory is
    a single-file omnigent bundle, or ``None`` otherwise.

    A directory qualifies as a single-file omnigent bundle when
    there is no ``config.yaml`` at the root and exactly one file
    at the root is recognised by :func:`is_omnigent_yaml`. Any
    other shape (``config.yaml`` present, zero or multiple omnigent
    YAMLs, YAMLs in subdirectories) returns ``None`` and the caller
    falls through to the standard omnigent directory-parse path.

    :param root: Path to an extracted bundle directory.
    :returns: The matched omnigent YAML path, or ``None`` if the
        directory is not a single-file omnigent bundle.
    """
    if (root / "config.yaml").exists():
        return None
    matches = [p for p in root.iterdir() if p.is_file() and is_omnigent_yaml(p)]
    if len(matches) == 1:
        return matches[0]
    return None


def load(
    source: Path | bytes,
    *,
    dest: Path | None = None,
    expand_env: bool = True,
    enforce_handler_allowlist: bool = False,
    prune_invalid_sub_agents: bool = False,
) -> AgentSpec:
    """
    Load an agent spec from a directory, tarball path, or raw
    bytes.

    If *source* is a directory, parse and validate it directly.
    If *source* is a file path (tarball or omnigent YAML) or raw
    bytes, dispatch accordingly.

    :param source: Path to an agent image directory, ``.tar.gz``
        bundle, omnigent single-file YAML, or raw tarball bytes
        (e.g. from an HTTP upload).
    :param dest: Extraction destination -- required when *source*
        is a tarball or bytes, ignored when *source* is a directory
        or omnigent YAML.
    :param expand_env: Whether to expand ``${VAR}`` references in
        connection blocks, MCP headers, and MCP ``env`` against the
        current process environment. ``True`` (the default) is for
        operator-authored specs whose author is the process owner
        (local ``omnigent run``, ``--agent`` preregistration). It
        MUST be ``False`` for tenant-supplied / HTTP-uploaded
        bundles: expanding their ``${VAR}`` against the server or
        runner process environment leaks server-side secrets into a
        spec-controlled MCP/LLM connection. See
        ``.claude/skills/code-review/security-guidelines.md``.
    :param enforce_handler_allowlist: When ``True``, reject any
        ``type: function`` policy whose handler dotted path is not a
        registered policy handler. This is the guard for the
        untrusted agent-bundle upload path — set by
        :func:`omnigent.server.bundles.validate_agent_bundle`. It is
        applied for both bundle shapes: the omnigent single-file YAML
        path (before the inner loader resolves/calls the handler at
        parse time) and the ``config.yaml`` path (post-parse, since
        that parser does not resolve handlers). Defaults to ``False``
        so trusted spec loading (local ``omnigent run``, operator
        configs) keeps supporting custom handlers.
    :param prune_invalid_sub_agents: When ``True``, a sub-agent that
        fails validation is **dropped** from the spec (removed from
        ``sub_agents`` and from any parent's ``tools.agents``
        reference) and loading continues, instead of failing the whole
        load. A WARNING is logged for each dropped sub-agent. The root
        agent must still validate — a genuine root-level error always
        raises. This is the backwards-compatibility guard for the
        **execution** paths (runner spec resolution, server
        :class:`~omnigent.runtime.agent_cache.AgentCache`): a bundle
        reaching those paths was already validated by the server that
        produced it, so a sub-agent that fails *here* means this client
        is older than that server and can't run that sub-agent (e.g. it
        names a harness this version doesn't know). Dropping the
        sub-agent lets the parent agent launch with the capabilities
        this client *does* support, rather than the whole agent failing
        to start. Defaults to ``False`` so authoring/upload paths
        (``omnigent run``,
        :func:`omnigent.server.bundles.validate_agent_bundle`) stay
        strict and surface real authoring mistakes to the author.
    :returns: A validated :class:`AgentSpec`.
    :raises OmnigentError: If the spec fails validation, if a policy
        names an unregistered handler under
        *enforce_handler_allowlist*, or if *source* is a tarball/bytes
        and *dest* is not provided.
    :raises FileNotFoundError: If *source* is a :class:`Path` that
        does not exist, or if the extracted directory is missing
        ``config.yaml``.
    :raises ExtractionError: If the tarball fails safety checks.
    """
    if isinstance(source, bytes):
        if dest is None:
            raise OmnigentError(
                "dest is required when loading from bytes",
                code=ErrorCode.INVALID_INPUT,
            )
        extract_safe(source, dest)
        root = dest
    elif source.is_dir():
        root = source
    elif source.is_file():
        # Omnigent single-file YAML dispatch — see
        # omnigent.spec._omnigent_compat. Tech-debt aside;
        # remove this branch when omnigent compat ends.
        if is_omnigent_yaml(source):
            return load_omnigent_yaml(
                source,
                enforce_handler_allowlist=enforce_handler_allowlist,
                prune_invalid_sub_agents=prune_invalid_sub_agents,
            )
        if source.suffix.lower() in {".yaml", ".yml"}:
            # The path is a YAML file but failed the omnigent check
            # (missing required key, ``spec_version`` set, malformed,
            # etc.). Falling through to the tarball-extraction branch
            # would surface the misleading "dest is required when
            # loading from a tarball" — the file's a YAML, not a
            # tarball. Diagnose the actual reason and surface it.
            reason = diagnose_yaml_rejection(source)
            raise OmnigentError(
                f"{source}: not a valid omnigent YAML — {reason}",
                code=ErrorCode.INVALID_INPUT,
            )
        if dest is None:
            raise OmnigentError(
                "dest is required when loading from a tarball",
                code=ErrorCode.INVALID_INPUT,
            )
        extract_safe(source, dest)
        root = dest
    else:
        raise FileNotFoundError(f"source not found: {source}")

    # Omnigent single-file YAML dispatch (extracted-bundle variant)
    # — when a bundle (directory, tarball, or raw bytes) resolves to
    # a root that contains exactly one omnigent YAML and no
    # ``config.yaml``, route to the omnigent adapter. This is the
    # shape produced by ``omnigent server --agent`` when it wraps a
    # YAML into a single-file tarball for the agent store. See
    # omnigent/spec/_omnigent_compat.py for the tech-debt
    # ownership; remove when omnigent compat ends.
    # The omnigent single-file path (load_omnigent_yaml) copies
    # headers / connection verbatim and never expands ``${VAR}``
    # against the process env, so it is safe regardless of
    # *expand_env* and the flag does not apply.
    candidate = _find_omnigent_yaml_in_dir(root)
    if candidate is not None:
        return load_omnigent_yaml(
            candidate,
            enforce_handler_allowlist=enforce_handler_allowlist,
            prune_invalid_sub_agents=prune_invalid_sub_agents,
        )

    spec = parse(root, expand_env=expand_env)
    if prune_invalid_sub_agents:
        _prune_invalid_sub_agents(spec)
    result = validate(spec)
    if not result.valid:
        errors = "; ".join(f"{e.path}: {e.message}" for e in result.errors)
        raise OmnigentError(
            f"invalid agent spec: {errors}",
            code=ErrorCode.INVALID_INPUT,
        )
    if enforce_handler_allowlist:
        # config.yaml path: the parser stores handler dotted paths as
        # strings without resolving them (resolution happens later at
        # engine build), so a post-parse scan is safe and sufficient —
        # it stops an unregistered handler from ever being stored/run.
        # The single-file omnigent YAML path is guarded earlier, inside
        # the loader, because that loader executes factories at parse.
        _reject_unregistered_spec_policy_handlers(spec)
        _reject_server_callable_tools(spec)
    return spec


def _prune_invalid_sub_agents(spec: AgentSpec) -> list[str]:
    """Drop sub-agents that fail validation so the parent can still load.

    Walks *spec*'s sub-agent tree depth-first and removes any
    sub-agent whose own subtree fails :func:`validate`, mutating
    *spec* in place: the failing sub-agent is removed from
    ``sub_agents`` and its name (if any) is removed from the parent's
    ``tools.agents`` reference list so the dangling reference does not
    itself fail validation. A WARNING is logged for each drop.

    Depth-first ordering matters: a child's invalid *grandchild* is
    pruned before the child is judged, so a single bad leaf does not
    take out an otherwise-valid sub-tree above it.

    This is the mechanism behind ``load(..., prune_invalid_sub_agents=
    True)`` — see :func:`load` for when and why it is enabled (the
    execution paths only). It deliberately does **not** touch the root
    spec: if the root itself is invalid, the caller's subsequent
    :func:`validate` still fails loud.

    :param spec: The spec to prune in place.
    :returns: The names (or ``"<unnamed>"``) of every sub-agent dropped
        anywhere in the tree, parent-most first within each level.
    """
    dropped: list[str] = []
    surviving: list[AgentSpec] = []
    for sa in spec.sub_agents:
        # Prune this child's own invalid descendants before deciding
        # whether the child itself survives.
        dropped.extend(_prune_invalid_sub_agents(sa))
        sa_result = validate(sa)
        if sa_result.valid:
            surviving.append(sa)
            continue
        name = sa.name or "<unnamed>"
        errors = "; ".join(f"{e.path}: {e.message}" for e in sa_result.errors)
        _logger.warning(
            "Dropping sub-agent %r from agent %r: it failed validation on this "
            "client and will be unavailable. This usually means the spec was "
            "produced by a newer Omnigent server with a feature this client does "
            "not support (e.g. a harness it does not recognize) — upgrade this "
            "client to use it. Validation errors: %s",
            name,
            spec.name or "<root>",
            errors,
        )
        dropped.append(name)
        # Remove the now-dangling reference so the parent still validates.
        if sa.name is not None and sa.name in spec.tools.agents:
            spec.tools.agents.remove(sa.name)
    spec.sub_agents = surviving
    return dropped


def _reject_unregistered_spec_policy_handlers(spec: AgentSpec) -> None:
    """Reject function policies whose handler is not registered.

    Scans a parsed :class:`AgentSpec`'s guardrail policies for
    :class:`~omnigent.spec.types.FunctionPolicySpec` entries whose
    ``function.path`` is not in the policy registry. Recurses into
    ``sub_agents`` — the ``config.yaml`` parser discovers child agents
    from ``agents/`` subdirectories, each with its own ``guardrails``,
    and those handlers are resolved + called at engine build just like
    the root's, so a clean root with a malicious sub-agent would
    otherwise bypass the upload allowlist. Used on the untrusted
    agent-bundle upload path only (see :func:`load`).

    :param spec: The parsed agent spec (or sub-agent) to scan.
    :raises OmnigentError: If a function policy names an unregistered
        handler, e.g. ``"subprocess.Popen"``.
    """
    from omnigent.policies.registry import is_registered_handler

    guardrails = spec.guardrails
    if guardrails is not None:
        for policy in guardrails.policies or []:
            if (
                isinstance(policy, FunctionPolicySpec)
                and policy.function is not None
                and not is_registered_handler(policy.function.path)
            ):
                raise OmnigentError(
                    f"Policy {policy.name!r}: handler {policy.function.path!r} is not a "
                    f"registered policy handler. Uploaded agent bundles may only use "
                    f"handlers from the policy registry; a server admin must add custom "
                    f"handlers via the 'policy_modules' config.",
                    code=ErrorCode.INVALID_INPUT,
                )
    for sub_agent in spec.sub_agents:
        _reject_unregistered_spec_policy_handlers(sub_agent)


def _reject_server_callable_tools(spec: AgentSpec) -> None:
    """Reject server-runtime ``callable:`` tools in an uploaded bundle.

    Server-runtime tools import + execute an arbitrary dotted path
    (GHSA-756x-9hf6-q4h4), so they are a trusted/operator-only feature.
    Used on the untrusted upload path only. Recurses into ``sub_agents``.

    :param spec: The parsed agent spec (or sub-agent) to scan.
    :raises OmnigentError: If a server-runtime tool declares a callable.
    """
    for tool in spec.local_tools or []:
        if tool.runtime == ToolRuntime.SERVER and tool.path:
            raise OmnigentError(
                f"Tool {tool.name!r}: server-runtime callable {tool.path!r} is not "
                f"allowed in an uploaded agent bundle. Use a 'client' runtime tool "
                f"or a Unity Catalog function; server-side callables require a "
                f"trusted (single-user/local) server.",
                code=ErrorCode.INVALID_INPUT,
            )
    for sub_agent in spec.sub_agents:
        _reject_server_callable_tools(sub_agent)
