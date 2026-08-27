"""``harness: goose`` wrap.

The entry point :mod:`omnigent.runtime.harnesses._runner` invokes after the
plugin registry resolves ``"goose"`` to this module. Goose speaks ACP, so this
adds no protocol code: it builds Goose's launch config and injects
:data:`~omnigent.inner.goose.GOOSE_ACP_EXTENSION`, which together are the only
things that distinguish a Goose harness process from a generic ``acp`` one.

Unlike :mod:`omnigent.inner.devin.harness`, this does not delegate to
:func:`omnigent.inner.acp_harness.create_app`: that reads the agent's launch from
``HARNESS_ACP_*``, and Goose's four launch quirks (forced approval mode, its
``GOOSE_`` env family, and its two sandbox roots) have no spelling there. The env
contract below is therefore Goose's own and unchanged from before Goose moved onto
the generic executor.

Auth is Goose's own configuration (``goose configure`` → keyring /
``~/.config/goose/config.yaml``); Omnigent stores no Goose credential. A spec
``executor.model`` is forwarded as a ``GOOSE_MODEL`` override; the provider stays
whatever ``goose configure`` selected unless ``HARNESS_GOOSE_PROVIDER`` overrides
it.

Env vars read at startup:

- ``HARNESS_GOOSE_MODEL``: optional ``GOOSE_MODEL`` override. ``None`` uses
  Goose's configured default.
- ``HARNESS_GOOSE_PROVIDER``: optional ``GOOSE_PROVIDER`` override.
- ``HARNESS_GOOSE_CWD``: working directory for the goose subprocess. ``None``
  falls back to ``OMNIGENT_RUNNER_WORKSPACE`` then the inherited cwd.
- ``OMNIGENT_GOOSE_PATH``: absolute path to a ``goose`` CLI binary. ``None``
  searches ``PATH``. (Legacy ``HARNESS_GOOSE_PATH`` still honored, deprecated.)
- ``HARNESS_GOOSE_BUILTINS``: comma-separated Goose builtin extensions to load
  (``--with-builtin``). ``None`` defaults to ``developer`` (shell + editor).
- ``HARNESS_GOOSE_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.goose import build_goose_executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_GOOSE_MODEL"
_ENV_PROVIDER = "HARNESS_GOOSE_PROVIDER"
_ENV_CWD = "HARNESS_GOOSE_CWD"
_ENV_BUILTINS = "HARNESS_GOOSE_BUILTINS"
_ENV_OS_ENV = "HARNESS_GOOSE_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Decodes the JSON-encoded :data:`_ENV_OS_ENV` (serialized via
    :func:`dataclasses.asdict`); falls back to ``caller_process`` +
    ``sandbox=none`` when the var is missing or malformed.
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


def _build_executor() -> Executor:
    """Assemble Goose's executor from env-var config (lazily, on first turn)."""
    cwd = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None
    builtins_raw = os.environ.get(_ENV_BUILTINS, "").strip()
    builtins = (
        tuple(part.strip() for part in builtins_raw.split(",") if part.strip())
        if builtins_raw
        else None
    )
    return build_goose_executor(
        cwd=cwd,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL, "").strip() or None,
        provider=os.environ.get(_ENV_PROVIDER, "").strip() or None,
        goose_path=resolve_harness_path("goose"),
        builtins=builtins,
    )


def create_app() -> FastAPI:
    """Build the Goose harness's FastAPI app (required entry point).

    The executor is constructed lazily on the first turn, so an absent ``goose``
    CLI surfaces as a request-time error rather than an app-boot crash.

    :returns: The app the runner serves for a ``harness: goose`` session.
    """
    adapter = ExecutorAdapter(executor_factory=_build_executor, harness_label="Goose")
    return adapter.build()
