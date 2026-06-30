"""
``harness: pi`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"pi"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.pi_executor.PiExecutor`
configured from env vars the parent process sets before spawning.
Mirrors the claude-sdk wrap (``claude_sdk_harness.py``) and codex
wrap (``codex_harness.py``); see the claude-sdk module's docstring
for the v1 config-flow rationale (env vars vs per-request).

Env vars read at startup:

- ``HARNESS_PI_MODEL``: model identifier, e.g.
  ``"databricks-claude-sonnet-4-6"``. ``None`` falls back to the
  Databricks default model on the profile-derived gateway path,
  else to Pi's own default.
- ``HARNESS_PI_GATEWAY``: ``"1"`` / ``"true"`` to write a
  ``models.json`` pointing Pi at a vendor-neutral gateway (base
  URLs + bearer-token command + model). The Databricks AI gateway
  is one producer of this transport; generic ``key`` / ``gateway``
  providers are another. Otherwise the executor uses Pi's built-in
  API path.
- ``HARNESS_PI_DATABRICKS_PROFILE``: Databricks-specific
  ``~/.databrickscfg`` profile name, used by the executor for
  Databricks credential resolution / token refresh when the
  gateway transport was fed from a Databricks profile, e.g.
  ``"<your-profile>"``.
- ``HARNESS_PI_GATEWAY_BASE_URLS``: JSON object of gateway base
  URLs keyed by model family, e.g.
  ``{"claude": "https://example.databricks.com/ai-gateway/anthropic"}``.
- ``HARNESS_PI_CWD``: working directory the executor launches
  the Pi CLI in. ``None`` falls back to ``OMNIGENT_RUNNER_WORKSPACE`` if set,
  then to the subprocess's inherited cwd.
- ``HARNESS_PI_PATH``: absolute path to a ``pi`` CLI binary.
  ``None`` searches ``PATH``.
- ``HARNESS_PI_OS_ENV``: JSON-encoded :class:`OSEnvSpec`
  (from :func:`dataclasses.asdict`). When unset, the wrap
  falls back to a default
  ``OSEnvSpec(type="caller_process", sandbox=type="none")`` so
  Omnigent mode parity with the legacy non-AP path holds for
  specs that don't declare an ``os_env:`` block.
- ``HARNESS_PI_SKILLS_FILTER``: JSON-encoded
  ``str | list[str]`` carrying ``spec.skills_filter``. When
  unset, falls back to ``"all"``. The Pi executor translates
  the filter into Pi CLI args at construction time:
  ``"all"`` adds ``--skill <path>`` for every bundled skill
  while leaving auto-discovery on, ``"none"`` adds
  ``--no-skills`` to suppress everything, and a list adds
  ``--no-skills`` plus ``--skill <path>`` for each named
  bundle skill.
- ``HARNESS_PI_BUNDLE_DIR``: Absolute path to the agent
  bundle's extracted root. When set, the executor sources
  bundled skills from ``<bundle>/skills/<name>/`` for the
  ``"all"`` and named-list cases. Unset for agents without a
  bundled-skill directory.
- ``HARNESS_PI_AGENT_NAME``: Agent display name. Reserved for
  future use; currently unused by Pi.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.pi_executor import PiExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. See
# the module docstring for semantics. Centralizing as constants
# so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_PI_MODEL"
_ENV_GATEWAY = "HARNESS_PI_GATEWAY"
_ENV_DATABRICKS_PROFILE = "HARNESS_PI_DATABRICKS_PROFILE"
_ENV_GATEWAY_HOST = "HARNESS_PI_GATEWAY_HOST"
_ENV_CWD = "HARNESS_PI_CWD"
_ENV_PI_PATH = "HARNESS_PI_PATH"
_ENV_OS_ENV = "HARNESS_PI_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_PI_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_PI_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_PI_AGENT_NAME"
_ENV_GATEWAY_BASE_URL = "HARNESS_PI_GATEWAY_BASE_URL"
_ENV_GATEWAY_BASE_URLS = "HARNESS_PI_GATEWAY_BASE_URLS"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_PI_GATEWAY_AUTH_COMMAND"
_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS = "HARNESS_PI_GATEWAY_AUTH_REFRESH_INTERVAL_MS"

# Truthy strings the wrap accepts for boolean env vars. Must
# match the claude-sdk and codex wraps' parsers for consistency
# — operators learn one set of conventions, not five.
_TRUTHY_STRINGS = ("1", "true", "yes")


def _parse_truthy(env_var: str, default: bool) -> bool:
    """
    Parse a boolean-style env var the same way the claude-sdk
    and codex wraps do.

    :param env_var: The env-var name (e.g. ``HARNESS_PI_GATEWAY``).
    :param default: The fallback when the env var is unset or
        empty.
    :returns: ``True`` if the value is in :data:`_TRUTHY_STRINGS`
        (case-insensitive); ``False`` for any other non-empty
        value; *default* when unset or empty.
    """
    raw = os.environ.get(env_var, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY_STRINGS


def _resolve_gateway_base_urls() -> dict[str, str] | None:
    """
    Decode Pi gateway base URLs from the gateway transport env var.

    :returns: Mapping of Pi provider family to base URL, or ``None`` when
        unset or invalid.
    """
    raw = os.environ.get(_ENV_GATEWAY_BASE_URLS, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("Ignoring invalid %s JSON", _ENV_GATEWAY_BASE_URLS)
        return None
    if not isinstance(value, dict):
        _logger.warning("Ignoring non-object %s value", _ENV_GATEWAY_BASE_URLS)
        return None
    return {str(key): str(item) for key, item in value.items() if isinstance(item, str)}


def _resolve_os_env() -> OSEnvSpec:
    """
    Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Reads :data:`_ENV_OS_ENV` and decodes the JSON-encoded dict
    Omnigent serialized via :func:`dataclasses.asdict` on its
    :class:`OSEnvSpec`. When the env var is missing or
    malformed, falls back to ``caller_process + sandbox=none``
    so AP-bridged tools stay enabled — matches the legacy
    non-AP path's default for specs without an
    ``os_env:`` block.

    :returns: An :class:`OSEnvSpec` to hand to
        :class:`PiExecutor`.
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


def _build_pi_executor() -> Executor:
    """
    Construct a :class:`PiExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first
    turn. Heavyweight init (CLI discovery, eager Databricks
    credential resolution) happens at this point — operators
    see the failure surface as a startup error on the first
    request, not at FastAPI app boot.

    :returns: A configured :class:`PiExecutor` instance.
    :raises ImportError: If the ``pi`` CLI isn't on PATH and
        ``HARNESS_PI_PATH`` isn't set — the inner executor's
        constructor surfaces this as a clear ImportError.
    :raises OSError: If ``HARNESS_PI_GATEWAY`` is set but
        credentials are missing — the inner executor's
        constructor fails loud.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    agent_name_raw = os.environ.get(_ENV_AGENT_NAME, "").strip()
    agent_name = agent_name_raw or None
    return PiExecutor(
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE"),
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL),
        pi_path=os.environ.get(_ENV_PI_PATH),
        gateway=_parse_truthy(_ENV_GATEWAY, default=False),
        databricks_profile=os.environ.get(_ENV_DATABRICKS_PROFILE),
        gateway_host=os.environ.get(_ENV_GATEWAY_HOST) or None,
        base_url_override=os.environ.get(_ENV_GATEWAY_BASE_URL) or None,
        base_urls_override=_resolve_gateway_base_urls(),
        gateway_auth_command=os.environ.get(_ENV_GATEWAY_AUTH_COMMAND) or None,
        bundle_dir=bundle_dir,
        agent_name=agent_name,
        skills_filter=_resolve_skills_filter(),
    )


def _resolve_skills_filter() -> str | list[str]:
    """
    Resolve the inner-executor ``skills_filter`` from env config.

    Reads :data:`_ENV_SKILLS_FILTER` and decodes the JSON-encoded
    ``str | list[str]`` (``"all"``, ``"none"``, or a list of skill
    names). Falls back to ``"all"`` on missing or malformed input
    — matches the SDK default behavior.

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


def create_app() -> FastAPI:
    """
    Build the pi harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness
        API subset wired up. The wrapped :class:`PiExecutor`
        is constructed lazily on the first turn (so an absent
        ``pi`` CLI surfaces as a request-time error, not a
        FastAPI app-boot crash).
    """
    adapter = ExecutorAdapter(executor_factory=_build_pi_executor)
    return adapter.build()
