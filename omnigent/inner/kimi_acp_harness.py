"""
``harness: kimi-acp`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"kimi-acp"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.kimi_acp_executor.KimiAcpExecutor`, which drives
Kimi Code's ACP mode (``kimi acp``, JSON-RPC over stdio) — the long-lived,
protocol-driven counterpart to the per-turn ``kimi`` (``kimi -p``) harness and
the terminal-first ``kimi-native`` TUI harness. Mirrors the qwen ACP wrap
(``qwen_harness.py``).

Env vars read at startup:

- ``HARNESS_KIMI_ACP_MODEL``: model identifier passed in ``session/new``.
  ``None`` falls back to Kimi's configured default.
- ``HARNESS_KIMI_ACP_CWD``: working directory the executor launches the ``kimi``
  CLI in. ``None`` falls back to ``OMNIGENT_RUNNER_WORKSPACE`` if set, then to
  the subprocess's inherited cwd.
- ``HARNESS_KIMI_ACP_PATH``: absolute path to a ``kimi`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_KIMI_ACP_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset,
  falls back to ``OSEnvSpec(type="caller_process", sandbox=type="none")``.

Kimi Code authenticates through its own ``kimi login`` flow (OAuth / Moonshot
API key), not an Omnigent gateway — so this wrap declares no gateway routing.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.kimi_acp_executor import KimiAcpExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_KIMI_ACP_MODEL"
_ENV_CWD = "HARNESS_KIMI_ACP_CWD"
_ENV_KIMI_PATH = "HARNESS_KIMI_ACP_PATH"
_ENV_OS_ENV = "HARNESS_KIMI_ACP_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Reads the JSON-encoded dict Omnigent serialized via ``dataclasses.asdict``.
    When the env var is missing or malformed, falls back to
    ``caller_process + sandbox=none`` — matching the qwen / cursor wraps'
    default so AP-bridged tools stay enabled for specs without an ``os_env:``
    block.

    :returns: An :class:`OSEnvSpec` to hand to :class:`KimiAcpExecutor`.
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


def _build_kimi_acp_executor() -> Executor:
    """Construct a :class:`KimiAcpExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so a
    missing ``kimi`` binary surfaces as a request-time error (not an app-boot
    crash) — matching the qwen wrap.

    :returns: A configured :class:`KimiAcpExecutor` instance.
    """
    cwd = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    kimi_path = os.environ.get(_ENV_KIMI_PATH, "").strip() or None

    return KimiAcpExecutor(
        cwd=cwd,
        os_env=_resolve_os_env(),
        model=model,
        kimi_path=kimi_path,
    )


def create_app() -> FastAPI:
    """Build the kimi-acp harness's FastAPI app (required entry point).

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s ``build``. The
        wrapped :class:`KimiAcpExecutor` is constructed lazily on the first turn
        (so an absent ``kimi`` CLI surfaces as a request-time error).
    """
    adapter = ExecutorAdapter(
        executor_factory=_build_kimi_acp_executor, harness_label="Kimi Code (ACP)"
    )
    return adapter.build()
