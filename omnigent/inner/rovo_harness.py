"""
``harness: rovo`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"rovo"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally instantiates
:class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter` around a
:class:`omnigent.inner.rovo_executor.RovoExecutor` configured from env vars the
parent process sets before spawning. Mirrors the codex wrap
(``codex_harness.py``); see that module for the v1 env-var config-flow rationale.

Env vars read at startup:

- ``HARNESS_ROVO_MODEL``: Rovo Dev model display name, e.g.
  ``"Claude Sonnet 4.6"``. ``None`` lets Rovo pick its own default.
- ``HARNESS_ROVO_CWD``: working directory the executor launches Rovo Dev in.
  ``None`` falls back to the current working directory at turn time.
- ``HARNESS_ROVO_ACLI_PATH``: absolute path to the ``acli`` binary. ``None``
  searches ``PATH``.
- ``HARNESS_ROVO_CONFIG_FILE``: Rovo Dev ``--config-file`` (defaults to
  ``~/.rovodev/config.yml`` inside the CLI when omitted).
- ``HARNESS_ROVO_SITE_URL``: Rovo Dev ``--site-url``.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.rovo_executor import RovoExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_ROVO_MODEL"
_ENV_CWD = "HARNESS_ROVO_CWD"
_ENV_ACLI_PATH = "HARNESS_ROVO_ACLI_PATH"
_ENV_CONFIG_FILE = "HARNESS_ROVO_CONFIG_FILE"
_ENV_SITE_URL = "HARNESS_ROVO_SITE_URL"


def _build_rovo_executor() -> Executor:
    """
    Construct a :class:`RovoExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so the
    Rovo Dev ACP subprocess is only spawned when a real conversation starts.

    :returns: A configured :class:`RovoExecutor` instance.
    """
    return RovoExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        model=os.environ.get(_ENV_MODEL) or None,
        acli_path=os.environ.get(_ENV_ACLI_PATH) or None,
        config_file=os.environ.get(_ENV_CONFIG_FILE) or None,
        site_url=os.environ.get(_ENV_SITE_URL) or None,
    )


def create_app() -> FastAPI:
    """
    Build the rovo harness's FastAPI app.

    Required entry point per the harness contract — the runner imports this
    module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and invokes
    ``create_app()`` to get the app it serves. The wrapped
    :class:`RovoExecutor` is constructed lazily on the first turn.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s :meth:`build`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_rovo_executor)
    return adapter.build()
