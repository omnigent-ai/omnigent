"""
``harness: codex`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"codex"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.codex_executor.CodexExecutor`
configured from env vars the parent process sets before spawning.
Mirrors the claude-sdk wrap (``claude_sdk_harness.py``); see that
module's docstring for the v1 config-flow rationale (env vars vs
per-request).

Env vars read at startup:

- ``HARNESS_CODEX_MODEL``: model identifier, e.g.
  ``"databricks-gpt-5-4-mini"``. ``None`` falls back to Codex's
  own default.
- ``HARNESS_CODEX_GATEWAY``: ``"1"`` / ``"true"`` to route
  through a vendor-neutral gateway (base URL + bearer-token
  command + model). The Databricks AI gateway (Codex Responses
  API at ``/ai-gateway/codex/v1``) is one producer of this
  transport; generic ``key`` / ``gateway`` providers are another.
  Otherwise the executor uses Codex's built-in API path.
- ``HARNESS_CODEX_DATABRICKS_PROFILE``: Databricks-specific
  ``~/.databrickscfg`` profile name, used by the executor for
  Databricks credential resolution / token refresh when the
  gateway transport was fed from a Databricks profile, e.g.
  ``"<your-profile>"``.
- ``HARNESS_CODEX_MODEL_PROVIDER``: a codex ``model_provider``
  id to pin via a ``-c`` override, e.g. ``"openai"`` (force the
  built-in provider for a subscription) or ``"Databricks"`` (a
  custom ``[model_providers.X]`` table in the user's
  ``~/.codex/config.toml``, bridged into the per-session
  ``CODEX_HOME``). Mutually exclusive with
  ``HARNESS_CODEX_GATEWAY`` (the gateway path pins its own
  generated provider).
- ``HARNESS_CODEX_CWD``: working directory the executor launches
  the Codex CLI in. ``None`` falls back to the subprocess's
  inherited cwd.
- ``OMNIGENT_CODEX_PATH``: absolute path to a ``codex`` CLI binary.
  ``None`` searches ``PATH``. (Legacy ``HARNESS_CODEX_PATH`` still honored,
  deprecated.)
- ``HARNESS_CODEX_ENABLE_WEB_SEARCH``: ``"1"`` / ``"true"`` to
  leave Codex's built-in ``web_search`` tool enabled. ``"0"`` /
  ``"false"`` disables it (forces the model to use only
  AP-bridged tools). Default: ``True``.
- ``HARNESS_CODEX_DISABLE_NATIVE_TOOLS``: ``"1"`` / ``"true"``
  to disable Codex's native tools entirely for the turn.
  Default: ``False``.
- ``HARNESS_CODEX_OS_ENV``: JSON-encoded :class:`OSEnvSpec`
  (from :func:`dataclasses.asdict`). When unset, the wrap
  falls back to a default
  ``OSEnvSpec(type="caller_process", sandbox=type="none")`` so
  Omnigent mode parity with the legacy non-AP path holds for
  specs that don't declare an ``os_env:`` block.
- ``HARNESS_CODEX_RETRY_POLICY``: JSON-encoded
  :class:`RetryPolicy` (from :meth:`RetryPolicy.to_json`)
  carrying the spec's ``llm.retry`` budget. When set, the
  inner ``CodexExecutor`` constructs the policy and threads
  ``policy.codex_cli.env()`` (e.g. ``OPENAI_MAX_RETRIES``,
  ``OPENAI_TIMEOUT``) to the Codex CLI subprocess. When
  unset, the executor's default ``RetryPolicy()`` applies —
  matches AP's "omit on default" optimization in
  ``_serialize_retry_policy``. Phase 1f of
  ``designs/RETRY_ACROSS_HARNESSES.md``.
- ``HARNESS_CODEX_SKILLS_FILTER``: JSON-encoded
  ``str | list[str]`` carrying ``spec.skills_filter``. When
  unset, falls back to ``"all"``. Codex auto-discovers skills
  under ``$CODEX_HOME/skills/<name>/SKILL.md``; the executor
  populates that directory at startup with symlinks based on
  this filter (``"all"`` mirrors every host skill, ``"none"``
  leaves the directory empty so codex sees no skills, list
  exposes only the named ones).
- ``HARNESS_CODEX_BUNDLE_DIR``: Absolute path to the agent
  bundle's extracted root. When set, the executor also sources
  bundled skills from ``<bundle>/skills/<name>/SKILL.md``
  (in addition to ``~/.codex/skills/``). Bundle skills win on
  name conflicts. Unset for agents without a bundled-skill
  directory.
- ``HARNESS_CODEX_AGENT_NAME``: Agent display name. Reserved
  for future use (e.g. namespacing bundled skills).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.spec.types import RetryPolicy

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. See
# the module docstring for semantics. Centralizing as constants
# so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_CODEX_MODEL"
_ENV_GATEWAY = "HARNESS_CODEX_GATEWAY"
_ENV_DATABRICKS_PROFILE = "HARNESS_CODEX_DATABRICKS_PROFILE"
_ENV_MODEL_PROVIDER = "HARNESS_CODEX_MODEL_PROVIDER"
_ENV_GATEWAY_HOST = "HARNESS_CODEX_GATEWAY_HOST"
_ENV_CWD = "HARNESS_CODEX_CWD"
_ENV_CODEX_PATH = "OMNIGENT_CODEX_PATH"
# Deprecated alias — read via resolve_harness_path() which warns on use.
# Remove this constant and the HARNESS_CODEX_PATH read in v0.8.0.
_LEGACY_ENV_CODEX_PATH = "HARNESS_CODEX_PATH"
_ENV_ENABLE_WEB_SEARCH = "HARNESS_CODEX_ENABLE_WEB_SEARCH"
_ENV_DISABLE_NATIVE_TOOLS = "HARNESS_CODEX_DISABLE_NATIVE_TOOLS"
_ENV_OS_ENV = "HARNESS_CODEX_OS_ENV"
_ENV_RETRY_POLICY = "HARNESS_CODEX_RETRY_POLICY"
_ENV_SKILLS_FILTER = "HARNESS_CODEX_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_CODEX_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_CODEX_AGENT_NAME"
_ENV_GATEWAY_BASE_URL = "HARNESS_CODEX_GATEWAY_BASE_URL"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_CODEX_GATEWAY_AUTH_COMMAND"
_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS = "HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS"

# Truthy strings the wrap accepts for boolean env vars. Must
# match the claude-sdk wrap's parser for consistency — operators
# learn one set of conventions, not five.
_TRUTHY_STRINGS = ("1", "true", "yes")


def _parse_truthy(env_var: str, default: bool) -> bool:
    """
    Parse a boolean-style env var the same way the claude-sdk
    wrap does.

    :param env_var: The env-var name (e.g. ``HARNESS_CODEX_GATEWAY``).
    :param default: The fallback when the env var is unset or
        empty. Used so each caller can express "unset means
        True" or "unset means False" without duplicating
        empty-string handling.
    :returns: ``True`` if the value is in :data:`_TRUTHY_STRINGS`
        (case-insensitive); ``False`` for any other non-empty
        value; *default* when unset or empty.
    """
    raw = os.environ.get(env_var, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY_STRINGS


def _resolve_os_env() -> OSEnvSpec:
    """
    Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Reads :data:`_ENV_OS_ENV` and decodes the JSON-encoded dict
    Omnigent serialized via :func:`dataclasses.asdict` on its
    :class:`OSEnvSpec`. When the env var is missing or
    malformed, falls back to ``caller_process + sandbox=none``
    so Codex's natives stay enabled — matches the legacy
    non-AP path's default.

    :returns: An :class:`OSEnvSpec` to hand to
        :class:`CodexExecutor`.
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
    :meth:`RetryPolicy.from_json`. Falls back to
    ``RetryPolicy()`` (defaults) when missing — Omnigent omits the
    env var when the spec's ``llm.retry`` matches defaults.
    Validation/parse errors degrade to the default policy with
    a warning log rather than crash, matching the
    claude-sdk wrap's behavior. Phase 1f of
    ``designs/RETRY_ACROSS_HARNESSES.md``.

    :returns: A :class:`RetryPolicy` to hand to
        :class:`CodexExecutor`.
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
    names). When the env var is missing or malformed, falls back to
    ``"all"`` — the SDK's default behavior of loading every skill
    discovered under ``$CODEX_HOME/skills/``.

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


def _build_codex_executor() -> Executor:
    """
    Construct a :class:`CodexExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first
    turn. Heavyweight init (CLI discovery, eager Databricks
    credential resolution) happens at this point — operators
    see the failure surface as a startup error on the first
    request, not at FastAPI app boot.

    :returns: A configured :class:`CodexExecutor` instance.
    :raises ImportError: If the ``codex`` CLI isn't on PATH and
        ``OMNIGENT_CODEX_PATH`` (legacy ``HARNESS_CODEX_PATH``) isn't set — the inner executor's
        constructor surfaces this as a clear ImportError.
    :raises OSError: If ``HARNESS_CODEX_GATEWAY`` is set but
        credentials are missing — the inner executor's
        constructor fails loud.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    agent_name_raw = os.environ.get(_ENV_AGENT_NAME, "").strip()
    agent_name = agent_name_raw or None
    return CodexExecutor(
        cwd=os.environ.get(_ENV_CWD),
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL),
        codex_path=resolve_harness_path("codex"),
        gateway=_parse_truthy(_ENV_GATEWAY, default=False),
        databricks_profile=os.environ.get(_ENV_DATABRICKS_PROFILE),
        model_provider_override=os.environ.get(_ENV_MODEL_PROVIDER) or None,
        gateway_host=os.environ.get(_ENV_GATEWAY_HOST) or None,
        # Default ``True`` mirrors the inner CodexExecutor's
        # constructor default, which mirrors Codex's own
        # default. An operator who set the env var to ``"0"``
        # wants the search disabled.
        enable_web_search=_parse_truthy(_ENV_ENABLE_WEB_SEARCH, default=True),
        # Default ``False`` mirrors the inner executor's default
        # (native tools enabled).
        disable_native_tools=_parse_truthy(_ENV_DISABLE_NATIVE_TOOLS, default=False),
        base_url_override=os.environ.get(_ENV_GATEWAY_BASE_URL) or None,
        gateway_auth_command=os.environ.get(_ENV_GATEWAY_AUTH_COMMAND) or None,
        gateway_auth_refresh_interval_ms=os.environ.get(_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS)
        or None,
        retry_policy=_resolve_retry_policy(),
        bundle_dir=bundle_dir,
        agent_name=agent_name,
        skills_filter=_resolve_skills_filter(),
    )


def create_app() -> FastAPI:
    """
    Build the codex harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness
        API subset wired up. The wrapped
        :class:`CodexExecutor` is constructed lazily on the
        first turn.
    """
    adapter = ExecutorAdapter(executor_factory=_build_codex_executor)
    return adapter.build()
