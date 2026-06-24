"""
``harness: cursor`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"cursor"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.cursor_executor.CursorExecutor`, which
drives a persistent Cursor SDK (``cursor-sdk``) agent over a local bridge.
Mirrors the codex / pi wraps' env-var config flow.

Unlike the gateway-backed harnesses, cursor has NO gateway /
Databricks-profile env vars: the SDK talks only to Cursor's own backend and has
no custom API base-URL override, so there is nothing for the workflow layer to
route through the Databricks AI gateway.

Env vars read at startup:

- ``HARNESS_CURSOR_MODEL``: Cursor model id, e.g. ``"gpt-5"`` or ``"auto"``.
  ``None`` resolves to cursor's ``auto`` select. A ``databricks-*`` id (from a
  spec authored for another harness) is dropped by the executor.
- ``HARNESS_CURSOR_CWD``: working directory the session operates in.
  ``None`` falls back to ``os_env.cwd`` then the process cwd.
- ``HARNESS_CURSOR_API_KEY``: Cursor API key, used as the SDK ``api_key``.
  ``None`` falls back to an inherited ``CURSOR_API_KEY``. The SDK requires an
  API key (unlike a ``cursor-agent login``).
- ``HARNESS_CURSOR_OS_ENV``: JSON-encoded :class:`OSEnvSpec` (its ``cwd`` is
  used when ``HARNESS_CURSOR_CWD`` is unset). Defaults to
  ``caller_process + sandbox=none``.
- ``HARNESS_CURSOR_SKILLS_FILTER``: JSON ``str | list[str]`` (parity;
  cursor has no skill mechanism here). Defaults to ``"all"``.
- ``HARNESS_CURSOR_BUNDLE_DIR`` / ``HARNESS_CURSOR_AGENT_NAME``:
  reserved for future use.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.cursor_executor import CursorExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_CURSOR_MODEL"
_ENV_CWD = "HARNESS_CURSOR_CWD"
_ENV_API_KEY = "HARNESS_CURSOR_API_KEY"
_ENV_OS_ENV = "HARNESS_CURSOR_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_CURSOR_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_CURSOR_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_CURSOR_AGENT_NAME"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Decodes the JSON-encoded dict Omnigent serialized via
    :func:`dataclasses.asdict`. When the env var is missing or malformed, falls
    back to ``caller_process + sandbox=none`` — matches the codex/pi wraps'
    default for specs without an ``os_env:`` block.
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


def _build_cursor_executor() -> Executor:
    """Construct a :class:`CursorExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so a
    missing ``cursor-sdk`` install surfaces as a request-time error rather than
    an app-boot crash.

    :raises ImportError: If the ``cursor-sdk`` package isn't installed.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    return CursorExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        api_key=os.environ.get(_ENV_API_KEY) or None,
        bundle_dir=bundle_dir,
        agent_name=os.environ.get(_ENV_AGENT_NAME, "").strip() or None,
        skills_filter=_resolve_skills_filter(),
    )


def create_app() -> FastAPI:
    """Build the cursor harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_cursor_executor, harness_label="Cursor")
    return adapter.build()
