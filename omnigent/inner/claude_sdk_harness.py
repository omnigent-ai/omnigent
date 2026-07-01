"""
``harness: claude-sdk`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"claude-sdk"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.claude_sdk_executor.ClaudeSDKExecutor`
configured from env vars the parent process sets before spawning.

V1 config-flow limitation (documented in the design doc's
Autonomous decisions section): config arrives via env vars rather
than per-spec data on the request body. Omnigent sets these env vars
in its own process before calling
:meth:`HarnessProcessManager.get_client`; the spawned subprocess
inherits them. Single-config-per-AP-process; multi-spec
deployments need a per-spawn env override (follow-up).

Env vars read at startup:

- ``HARNESS_CLAUDE_SDK_MODEL``: model identifier, e.g.
  ``"databricks-claude-opus-4-6"``. ``None`` falls back to
  Claude SDK's own default.
- ``HARNESS_CLAUDE_SDK_GATEWAY``: ``"1"`` / ``"true"`` to route
  through a vendor-neutral gateway (base URL + bearer-token
  command + model). The Databricks AI gateway is one producer of
  this transport; generic ``key`` / ``gateway`` providers are
  another. Otherwise the SDK uses its built-in API path.
- ``HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE``: Databricks-specific
  ``~/.databrickscfg`` profile name, used by the executor for
  Databricks credential resolution / token refresh when the
  gateway transport was fed from a Databricks profile, e.g.
  ``"<your-profile>"``.
- ``HARNESS_CLAUDE_SDK_CWD``: working directory the SDK launches
  the Claude CLI in. ``None`` falls back to the subprocess's
  inherited cwd.
- ``HARNESS_CLAUDE_SDK_PERMISSION_MODE``: SDK permission mode
  (``"auto"``, ``"bypassPermissions"``, ``"acceptEdits"``,
  ``"plan"``, ``"dontAsk"``, ``"default"``). Defaults to
  ``"auto"`` so the agent runs autonomously with background
  safety checks.
- ``HARNESS_CLAUDE_SDK_OS_ENV``: JSON-encoded :class:`OSEnvSpec`
  (from :func:`dataclasses.asdict`) controlling the SDK's
  native OS-tool exposure. When set, the inner executor builds
  the OSEnvSpec from this payload and the SDK exposes
  ``Bash/Read/Edit/Write/Glob/Grep`` to the LLM. When unset,
  the wrap falls back to a default
  ``OSEnvSpec(type="caller_process", sandbox=type="none")`` so
  Omnigent mode parity with the legacy non-AP path holds for
  specs that don't declare an ``os_env:`` block.
- ``HARNESS_CLAUDE_SDK_RETRY_POLICY``: JSON-encoded
  :class:`RetryPolicy` (from :func:`dataclasses.asdict`)
  carrying the spec's ``llm.retry`` budget. When set, the
  inner ``ClaudeSDKExecutor`` constructs the policy from this
  payload and threads ``policy.claude_cli.env()`` through to
  the Claude CLI subprocess (``ANTHROPIC_MAX_RETRIES``,
  ``ANTHROPIC_REQUEST_TIMEOUT_SECONDS``). When unset, the
  executor's default ``RetryPolicy()`` applies — matches AP's
  ``_serialize_retry_policy`` "omit on default" optimization.
  Phase 1f of ``designs/RETRY_ACROSS_HARNESSES.md``.
- ``HARNESS_CLAUDE_SDK_SKILLS_FILTER``: JSON-encoded
  ``str | list[str]`` carrying ``spec.skills_filter``. When
  unset, falls back to ``"all"`` (the SDK's default). Plumbed
  end-to-end so ``skills: none`` in the spec actually produces
  ``ClaudeAgentOptions(skills=[], setting_sources=[])`` — the
  pair that suppresses both host-discovered (user/project)
  skills and the SDK's auto-default of
  ``setting_sources=["user","project"]``.
- ``HARNESS_CLAUDE_SDK_BUNDLE_DIR``: Absolute path to the
  agent bundle's extracted root. When set, the inner executor
  passes ``plugins=[{"type": "local", "path": <bundle_dir>}]``
  to the SDK so any ``<bundle>/skills/<name>/SKILL.md`` files
  surface as agent-bundled skills (regardless of
  ``skills_filter``). Unset for agents without a bundled-skills
  directory.
- ``HARNESS_CLAUDE_SDK_AGENT_NAME``: Agent display name. Used
  when ``HARNESS_CLAUDE_SDK_BUNDLE_DIR`` is set to write the
  bundle's ``.claude-plugin/plugin.json`` manifest with a
  stable plugin name (so bundled skills show as
  ``<agent>:<skill>`` rather than the bundle's tmpdir
  basename).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.spec.types import RetryPolicy

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. See
# the module docstring for semantics. Centralizing as constants
# so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_CLAUDE_SDK_MODEL"
_ENV_GATEWAY = "HARNESS_CLAUDE_SDK_GATEWAY"
_ENV_DATABRICKS_PROFILE = "HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"
_ENV_GATEWAY_HOST = "HARNESS_CLAUDE_SDK_GATEWAY_HOST"
_ENV_CWD = "HARNESS_CLAUDE_SDK_CWD"
_ENV_PERMISSION_MODE = "HARNESS_CLAUDE_SDK_PERMISSION_MODE"
_ENV_OS_ENV = "HARNESS_CLAUDE_SDK_OS_ENV"
_ENV_RETRY_POLICY = "HARNESS_CLAUDE_SDK_RETRY_POLICY"
_ENV_SKILLS_FILTER = "HARNESS_CLAUDE_SDK_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_CLAUDE_SDK_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_CLAUDE_SDK_AGENT_NAME"
_ENV_GATEWAY_BASE_URL = "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"
_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS = "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS"
# Shell command the Claude CLI invokes to retrieve a bearer token.
# Set by the Omnigent workflow layer when executor.auth: {type: api_key, …} is
# declared.  Passed into ClaudeSDKExecutor.api_key_helper so it reaches
# settings.apiKeyHelper at turn time (not read from os.environ — the
# executor strips ANTHROPIC_API_KEY before connecting to avoid subscription
# auth being bypassed).
_ENV_API_KEY_HELPER = "HARNESS_CLAUDE_SDK_API_KEY_HELPER"

# Default permission mode for the Claude SDK. ``"auto"`` auto-approves
# tool calls with background safety checks that verify actions align
# with the request.
_DEFAULT_PERMISSION_MODE = "auto"


def _resolve_os_env() -> OSEnvSpec:
    """
    Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Reads :data:`_ENV_OS_ENV` and decodes the JSON-encoded dict
    Omnigent serialized via :func:`dataclasses.asdict` on its
    :class:`OSEnvSpec`. When the env var is missing or
    malformed, falls back to ``caller_process + sandbox=none``
    so the SDK still exposes the natives — leaving them off by
    default would surprise users migrating from the legacy
    non-AP path, where the natives are visible without
    any explicit opt-in.

    :returns: An :class:`OSEnvSpec` to hand to
        :class:`ClaudeSDKExecutor`.
    """
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env",
                _ENV_OS_ENV,
                exc,
            )
            payload = None
        if isinstance(payload, dict):
            sandbox_payload = payload.get("sandbox")
            sandbox = (
                OSEnvSandboxSpec(**sandbox_payload) if isinstance(sandbox_payload, dict) else None
            )
            return OSEnvSpec(
                type=str(payload.get("type", "caller_process")),
                cwd=payload.get("cwd"),
                sandbox=sandbox,
                fork=bool(payload.get("fork", False)),
            )
    # Default: enable natives, no sandbox. Matches the simplest
    # working config; operators who want real sandbox enforcement
    # configure ``os_env.sandbox`` explicitly in the spec.
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _resolve_retry_policy() -> RetryPolicy:
    """
    Resolve the inner-executor :class:`RetryPolicy` from env config.

    Reads :data:`_ENV_RETRY_POLICY` and delegates to
    :meth:`RetryPolicy.from_json` for the round-trip. Falls
    back to ``RetryPolicy()`` (defaults) when the env var is
    missing — Omnigent omits the env var when ``llm.retry`` matches
    defaults, so this is the common path.

    Validation/parse errors are logged and demoted to the
    default policy rather than propagating, since a malformed
    retry payload shouldn't prevent the harness from booting —
    we'd rather use the conservative default and surface the
    bad config in logs than fail to start. Phase 1f of
    ``designs/RETRY_ACROSS_HARNESSES.md``.

    :returns: A :class:`RetryPolicy` to hand to
        :class:`ClaudeSDKExecutor`.
    """
    raw = os.environ.get(_ENV_RETRY_POLICY, "").strip()
    if not raw:
        return RetryPolicy()
    try:
        return RetryPolicy.from_json(raw)
    except ValueError as exc:
        _logger.warning(
            "%s could not be parsed (%s); falling back to default RetryPolicy",
            _ENV_RETRY_POLICY,
            exc,
        )
        return RetryPolicy()


def _resolve_skills_filter() -> str | list[str]:
    """
    Resolve the inner-executor ``skills_filter`` from env config.

    Reads :data:`_ENV_SKILLS_FILTER` and decodes the JSON-encoded
    ``str | list[str]`` (``"all"``, ``"none"``, or a list of skill
    names). When the env var is missing or malformed, falls back
    to ``"all"`` — the SDK's default behavior of loading every
    host-discovered skill.

    :returns: ``"all"``, ``"none"``, or a list of skill names.
    """
    raw = os.environ.get(_ENV_SKILLS_FILTER, "").strip()
    if not raw:
        return "all"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "%s is not valid JSON (%s); falling back to 'all'",
            _ENV_SKILLS_FILTER,
            exc,
        )
        return "all"
    if isinstance(decoded, str) and decoded in ("all", "none"):
        return decoded
    if isinstance(decoded, list) and all(isinstance(s, str) for s in decoded):
        return decoded
    _logger.warning(
        "%s decoded to unsupported shape %r; falling back to 'all'",
        _ENV_SKILLS_FILTER,
        decoded,
    )
    return "all"


def _build_claude_sdk_executor() -> Executor:
    """
    Construct a :class:`ClaudeSDKExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first
    turn. Heavyweight init (CLI discovery, eager Databricks
    credential resolution) happens at this point — operators
    see the failure surface as a startup error on the first
    request, not at FastAPI app boot.

    :returns: A configured :class:`ClaudeSDKExecutor` instance.
    :raises OSError: If ``HARNESS_CLAUDE_SDK_GATEWAY`` is set
        but credentials are missing — the inner executor's
        constructor fails loud (matches its existing behavior).
    """
    gateway_raw = os.environ.get(_ENV_GATEWAY, "").strip().lower()
    gateway = gateway_raw in ("1", "true", "yes")
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    agent_name_raw = os.environ.get(_ENV_AGENT_NAME, "").strip()
    agent_name = agent_name_raw or None
    return ClaudeSDKExecutor(
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE"),
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL),
        permission_mode=os.environ.get(_ENV_PERMISSION_MODE, _DEFAULT_PERMISSION_MODE),
        gateway=gateway,
        databricks_profile=os.environ.get(_ENV_DATABRICKS_PROFILE),
        gateway_host=os.environ.get(_ENV_GATEWAY_HOST) or None,
        base_url_override=os.environ.get(_ENV_GATEWAY_BASE_URL) or None,
        gateway_auth_command=os.environ.get(_ENV_GATEWAY_AUTH_COMMAND) or None,
        gateway_auth_refresh_interval_ms=os.environ.get(_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS)
        or None,
        retry_policy=_resolve_retry_policy(),
        bundle_dir=bundle_dir,
        agent_name=agent_name,
        skills_filter=_resolve_skills_filter(),
        api_key_helper=os.environ.get(_ENV_API_KEY_HELPER) or None,
    )


def create_app() -> FastAPI:
    """
    Build the claude-sdk harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness
        API subset wired up. The wrapped
        :class:`ClaudeSDKExecutor` is constructed lazily on the
        first turn.
    """
    adapter = ExecutorAdapter(executor_factory=_build_claude_sdk_executor)
    return adapter.build()
