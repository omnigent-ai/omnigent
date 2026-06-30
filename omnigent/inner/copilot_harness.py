"""
``harness: copilot`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"copilot"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.copilot_executor.CopilotExecutor`, which drives
a persistent GitHub Copilot SDK (``github-copilot-sdk``) session. Mirrors the
cursor / antigravity wraps' env-var config flow.

Like cursor and antigravity, copilot has NO gateway / Databricks-profile env
vars: the Copilot SDK talks only to GitHub's Copilot backend (authenticated by a
GitHub token) and has no custom API base-URL override, so there is nothing for
the workflow layer to route through the Databricks AI gateway.

Env vars read at startup:

- ``HARNESS_COPILOT_MODEL``: Copilot model id, e.g. ``"claude-haiku-4.5"`` or
  ``"gpt-5-mini"``. ``None`` lets Copilot auto-select. A ``databricks-*`` id
  (from a spec authored for another harness) is dropped by the executor.
- ``HARNESS_COPILOT_CWD``: working directory the session operates in.
  ``None`` falls back to ``os_env.cwd`` then the process cwd.
- ``HARNESS_COPILOT_GITHUB_TOKEN``: GitHub token carrying Copilot access, used
  as the SDK ``github_token``. ``None`` falls back to an inherited
  ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``.
- ``HARNESS_COPILOT_OS_ENV``: JSON-encoded :class:`OSEnvSpec` (its ``cwd`` is
  used when ``HARNESS_COPILOT_CWD`` is unset). Defaults to
  ``caller_process + sandbox=none``.
- ``HARNESS_COPILOT_SKILLS_FILTER``: JSON ``str | list[str]`` (parity;
  copilot has no skill mechanism wired here). Defaults to ``"all"``.
- ``HARNESS_COPILOT_BUNDLE_DIR`` / ``HARNESS_COPILOT_AGENT_NAME``:
  reserved for future use.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.copilot_executor import CopilotExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_COPILOT_MODEL"
_ENV_CWD = "HARNESS_COPILOT_CWD"
_ENV_GITHUB_TOKEN = "HARNESS_COPILOT_GITHUB_TOKEN"
_ENV_OS_ENV = "HARNESS_COPILOT_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_COPILOT_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_COPILOT_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_COPILOT_AGENT_NAME"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Decodes the JSON-encoded dict Omnigent serialized via
    :func:`dataclasses.asdict`. When the env var is missing or malformed, falls
    back to ``caller_process + sandbox=none`` — matches the cursor/codex/pi
    wraps' default for specs without an ``os_env:`` block.
    """
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env", _ENV_OS_ENV, exc
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
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _resolve_skills_filter() -> str | list[str]:
    """Resolve ``skills_filter`` from :data:`_ENV_SKILLS_FILTER` (defaults ``"all"``)."""
    raw = os.environ.get(_ENV_SKILLS_FILTER, "").strip()
    if not raw:
        return "all"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "%s is not valid JSON (%s); falling back to 'all'", _ENV_SKILLS_FILTER, exc
        )
        return "all"
    if isinstance(decoded, str) and decoded in ("all", "none"):
        return decoded
    if isinstance(decoded, list) and all(isinstance(s, str) for s in decoded):
        return decoded
    _logger.warning(
        "%s decoded to unsupported shape %r; falling back to 'all'", _ENV_SKILLS_FILTER, decoded
    )
    return "all"


def _build_copilot_executor() -> Executor:
    """Construct a :class:`CopilotExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so a
    missing ``github-copilot-sdk`` install surfaces as a request-time error
    rather than an app-boot crash.

    :raises ImportError: If the ``github-copilot-sdk`` package isn't installed.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    return CopilotExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        github_token=os.environ.get(_ENV_GITHUB_TOKEN) or None,
        bundle_dir=bundle_dir,
        agent_name=os.environ.get(_ENV_AGENT_NAME, "").strip() or None,
        skills_filter=_resolve_skills_filter(),
    )


def create_app() -> FastAPI:
    """Build the copilot harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_copilot_executor)
    return adapter.build()
