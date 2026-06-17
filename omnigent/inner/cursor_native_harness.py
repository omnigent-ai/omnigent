"""``harness: cursor-native`` wrap (Cursor CLI via the ACP server).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"cursor-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.cursor_native_executor.CursorNativeExecutor`,
which drives the official Cursor CLI's ACP server (``cursor-agent acp``) over
stdio. Distinct from the SDK ``cursor`` harness: auth is the ambient
``cursor-agent login`` (no ``CURSOR_API_KEY``), and there is no gateway /
Databricks-profile routing (cursor talks only to Cursor's own backend).

Env vars read at startup:

- ``HARNESS_CURSOR_NATIVE_MODEL``: reserved model pin (logged, not yet applied —
  cursor-agent uses its configured default in this build).
- ``HARNESS_CURSOR_NATIVE_CWD``: working directory the session operates in;
  ``None`` falls back to the process cwd.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from omnigent.inner.cursor_native_executor import CursorNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_ENV_MODEL = "HARNESS_CURSOR_NATIVE_MODEL"
_ENV_CWD = "HARNESS_CURSOR_NATIVE_CWD"


def _build_cursor_native_executor() -> Executor:
    """Construct a :class:`CursorNativeExecutor` from env-var config."""
    return CursorNativeExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        model=os.environ.get(_ENV_MODEL) or None,
    )


def create_app() -> FastAPI:
    """Build the cursor-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_cursor_native_executor)
    return adapter.build()
