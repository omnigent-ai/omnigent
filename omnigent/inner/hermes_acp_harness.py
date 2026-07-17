"""``harness: hermes-acp`` wrap.

Thin module exposing :func:`create_app` -- the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"hermes-acp"`` via the harness registry. Mirrors
:mod:`omnigent.inner.hermes_harness`, but wraps the streaming
:class:`omnigent.inner.hermes_acp_executor.HermesAcpExecutor` instead of the
batch executor. Reads the same ``HARNESS_HERMES_*`` env vars so an agent can
switch ``harness: hermes`` to ``harness: hermes-acp`` with no other change.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.hermes_acp_executor import HermesAcpExecutor

# Reuse the batch wrap's env resolvers so the two paths stay in lockstep.
from omnigent.inner.hermes_harness import (
    _ENV_AGENT_NAME,
    _ENV_BUNDLE_DIR,
    _ENV_CWD,
    _ENV_HERMES_PATH,
    _ENV_MODEL,
    _resolve_os_env,
    _resolve_skills_filter,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)


def _build_hermes_acp_executor() -> Executor:
    """Construct a :class:`HermesAcpExecutor` from the shared ``HARNESS_HERMES_*`` env config."""
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    agent_name_raw = os.environ.get(_ENV_AGENT_NAME, "").strip()
    return HermesAcpExecutor(
        hermes_path=os.environ.get(_ENV_HERMES_PATH),
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE"),
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL),
        skills_filter=_resolve_skills_filter(),
        bundle_dir=str(Path(bundle_dir_raw)) if bundle_dir_raw else None,
        agent_name=agent_name_raw or None,
    )


def create_app() -> FastAPI:
    """Build the hermes-acp harness FastAPI app (executor constructed lazily on first turn)."""
    adapter = ExecutorAdapter(executor_factory=_build_hermes_acp_executor)
    return adapter.build()
