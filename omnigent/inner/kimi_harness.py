"""``harness: kimi`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent
process resolves ``"kimi"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.kimi_executor.KimiExecutor` that drives
the upstream Moonshot AI ``kimi`` CLI
(https://github.com/MoonshotAI/Kimi-Code) headlessly via
``kimi -p <prompt> --output-format stream-json`` per turn.

Env vars read at startup (full contract in
``omnigent.inner.kimi_executor``):

- ``HARNESS_KIMI_MODEL`` — model id (e.g. ``kimi-k2-turbo``); ``None``
  lets kimi's ``default_model`` from ``~/.kimi/config.toml`` win.
- ``HARNESS_KIMI_CWD`` — working directory the kimi subprocess runs in
  (upstream has no ``--work-dir`` flag, so this is threaded as
  subprocess ``cwd=``).
- ``HARNESS_KIMI_PATH`` — path to the ``kimi`` binary. Default
  ``"kimi"``.
- ``HARNESS_KIMI_PLAN`` — truthy → ``--plan`` (read-only plan mode).
- ``HARNESS_KIMI_CONTINUE_LAST`` — truthy → ``-C`` (continue the
  previous session for the working directory). Mutually exclusive with
  an active resume id; the explicit id wins.
- ``HARNESS_KIMI_SKILLS_DIRS`` — JSON array of paths, each forwarded
  as ``--skills-dir <path>``.
- ``HARNESS_KIMI_OS_ENV`` — JSON-encoded :class:`OSEnvSpec`. ``None``
  falls back to ``caller_process + sandbox=none`` (kimi handles its
  own sandbox + approval flow internally).

Provider routing for kimi happens via ``kimi provider add`` / its
``~/.kimi/config.toml`` (out-of-band from Omnigent) — upstream kimi
has no per-spawn ``--config-file`` or env-var provider override.
Omnigent-side provider injection remains a deferred follow-up.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.kimi_executor import KimiExecutor, _resolve_skills_dirs
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_KIMI_MODEL"
_ENV_CWD = "HARNESS_KIMI_CWD"
_ENV_BIN = "HARNESS_KIMI_PATH"
_ENV_PLAN = "HARNESS_KIMI_PLAN"
_ENV_CONTINUE_LAST = "HARNESS_KIMI_CONTINUE_LAST"
_ENV_SKILLS_DIRS = "HARNESS_KIMI_SKILLS_DIRS"
_ENV_OS_ENV = "HARNESS_KIMI_OS_ENV"


def _parse_truthy_with_default(value: str | None, *, default: bool) -> bool:
    """Same as ``_parse_truthy`` but with an explicit default for unset/empty."""
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Mirrors the cursor / antigravity wraps' default: when no spec was
    serialised, fall back to ``caller_process + sandbox=none``. Kimi
    has its own internal sandbox / approval flow, so Omnigent does not
    wrap the subprocess in bwrap / seatbelt by default — the user can
    still set a sandbox via the spec's ``os_env`` block.
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
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _build_kimi_executor() -> Executor:
    """Construct a :class:`KimiExecutor` from env-var config.

    Called lazily by :class:`ExecutorAdapter` on the first turn, so a
    missing ``kimi`` binary surfaces as a request-time error (not an
    app-boot crash) — matching how the cursor / antigravity wraps
    defer their SDK / binary lookup.
    """
    return KimiExecutor(
        # Run kimi in the session workspace: an explicit HARNESS_KIMI_CWD wins,
        # else the runner's OMNIGENT_RUNNER_WORKSPACE (the cwd the user launched
        # in), else the process cwd. Without the workspace fallback kimi ran out
        # of the runner's cwd (a /tmp launcher dir), so its tools reported the
        # wrong directory. Mirrors goose / pi / qwen / hermes harness cwd resolution.
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        binary_path=os.environ.get(_ENV_BIN) or None,
        plan=_parse_truthy_with_default(os.environ.get(_ENV_PLAN), default=False),
        continue_last_session=_parse_truthy_with_default(
            os.environ.get(_ENV_CONTINUE_LAST), default=False
        ),
        skills_dirs=_resolve_skills_dirs(os.environ.get(_ENV_SKILLS_DIRS)),
    )


def create_app() -> FastAPI:
    """Build the kimi harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_kimi_executor)
    return adapter.build()
