"""Hold a runner's turns until its worktree's project setup has finished.

The host passes the path of the worktree's setup marker when setup is still
due; the marker is written when setup ends, whatever its outcome, so the agent
never runs against a half-installed worktree. Projects that opt into
``parallel_setup`` get no marker and are never held.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from omnigent.project_setup import PROJECT_DEFERRED_ENV_ENV_VAR, SETUP_BUDGET_S

_logger = logging.getLogger(__name__)

PROJECT_SETUP_MARKER_ENV_VAR = "OMNIGENT_PROJECT_SETUP_MARKER"
#: The host's setup budget plus slack for it to write the marker.
WAIT_TIMEOUT_S = SETUP_BUDGET_S + 60.0
_POLL_INTERVAL_S = 0.5

_released = False


def apply_deferred_project_env() -> None:
    """
    Apply project env changes the host kept off the runner's own startup.

    Called once the interpreter is up, so ``PYTHONPATH`` and friends reach the
    agent's tool processes without changing how the runner itself started.
    A malformed payload is logged and ignored.
    """
    raw = os.environ.pop(PROJECT_DEFERRED_ENV_ENV_VAR, None)
    if not raw:
        return
    try:
        payload = json.loads(raw)
        set_vars = {str(k): str(v) for k, v in payload.get("set", {}).items()}
        unset_vars = [str(k) for k in payload.get("unset", [])]
    except (ValueError, AttributeError, TypeError):
        _logger.warning("Ignoring a malformed deferred project env")
        return
    for key in unset_vars:
        os.environ.pop(key, None)
    os.environ.update(set_vars)


async def wait_for_project_setup(
    *, timeout_s: float = WAIT_TIMEOUT_S, poll_interval_s: float = _POLL_INTERVAL_S
) -> None:
    """
    Return once the worktree's project setup has finished, or immediately if none is due.

    After the first return every later turn proceeds at once. A timeout lets
    the turn run anyway rather than wedging the session.

    :param timeout_s: Longest wait.
    :param poll_interval_s: Seconds between marker checks.
    """
    global _released
    if _released:
        return
    marker = os.environ.get(PROJECT_SETUP_MARKER_ENV_VAR)
    if marker and not os.path.exists(marker):
        _logger.info("Waiting for project setup to finish before the first turn")
        deadline = time.monotonic() + timeout_s
        while not os.path.exists(marker):
            if time.monotonic() >= deadline:
                _logger.warning("Project setup did not finish within %.0fs; continuing", timeout_s)
                break
            await asyncio.sleep(poll_interval_s)
    _released = True
