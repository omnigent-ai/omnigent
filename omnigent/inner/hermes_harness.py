"""
``harness: hermes`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"hermes"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.hermes_executor.HermesExecutor`
configured from env vars the parent process sets before spawning.

Mirrors the pi harness wrap (``pi_harness.py``); see that module's
docstring for the v1 config-flow rationale (env vars vs per-request).

Env vars read at startup:

- ``HARNESS_HERMES_MODEL``: model identifier, e.g.
  ``"deepseek/deepseek-chat"`` or ``"anthropic/claude-sonnet-4"``.
  ``None`` falls back to Hermes' own configured default.
- ``HARNESS_HERMES_CWD``: working directory the subprocess runs in.
  ``None`` falls back to ``os.getcwd()``.
- ``HARNESS_HERMES_PATH``: absolute path to the ``hermes`` CLI binary.
  ``None`` searches ``PATH``.
- ``HARNESS_HERMES_OS_ENV``: JSON-encoded :class:`OSEnvSpec`
  (from :func:`dataclasses.asdict`). When unset, the wrap
  falls back to a default
  ``OSEnvSpec(type="caller_process", sandbox=type="none")`` so
  Omnigent mode parity with the legacy non-AP path holds for
  specs that don't declare an ``os_env:`` block.
- ``HARNESS_HERMES_SKILLS_FILTER``: JSON-encoded
  ``str | list[str]`` carrying ``spec.skills_filter``. When
  unset, falls back to ``"all"``.
- ``HARNESS_HERMES_BUNDLE_DIR``: Absolute path to the agent
  bundle's extracted root. When set, the executor sources
  bundled skills from ``<bundle>/skills/<name>/``. Unset for
  agents without a bundled-skill directory.
- ``HARNESS_HERMES_AGENT_NAME``: Agent display name. Reserved for
  future use; currently unused by Hermes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.hermes_executor import HermesExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. See
# the module docstring for semantics. Centralizing as constants
# so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_HERMES_MODEL"
_ENV_CWD = "HARNESS_HERMES_CWD"
_ENV_HERMES_PATH = "HARNESS_HERMES_PATH"
_ENV_OS_ENV = "HARNESS_HERMES_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_HERMES_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_HERMES_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_HERMES_AGENT_NAME"


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
        :class:`HermesExecutor`.
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


def _build_hermes_executor() -> Executor:
    """
    Construct a :class:`HermesExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first
    turn. Heavyweight init (CLI discovery) happens at this point
    — operators see the failure surface as a startup error on the
    first request, not at FastAPI app boot.

    :returns: A configured :class:`HermesExecutor` instance.
    :raises FileNotFoundError: If ``hermes`` is not on PATH and
        ``HARNESS_HERMES_PATH`` isn't set.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = str(Path(bundle_dir_raw)) if bundle_dir_raw else None
    agent_name_raw = os.environ.get(_ENV_AGENT_NAME, "").strip()
    agent_name = agent_name_raw or None
    return HermesExecutor(
        hermes_path=os.environ.get(_ENV_HERMES_PATH),
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE"),
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL),
        skills_filter=_resolve_skills_filter(),
        bundle_dir=bundle_dir,
        agent_name=agent_name,
    )


def create_app() -> FastAPI:
    """
    Build the hermes harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness
        API subset wired up. The wrapped :class:`HermesExecutor`
        is constructed lazily on the first turn (so an absent
        ``hermes`` CLI surfaces as a request-time error, not a
        FastAPI app-boot crash).
    """
    adapter = ExecutorAdapter(executor_factory=_build_hermes_executor)
    return adapter.build()
