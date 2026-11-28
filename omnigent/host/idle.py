"""Host quiescence contract for managed runtimes that suspend idle compute."""

from __future__ import annotations

import hashlib
import os

HOST_IDLE_SUSPEND_ENV = "OMNIGENT_HOST_IDLE_SUSPEND"
RUNNER_WORK_IDLE_ENV = "OMNIGENT_RUNNER_WORK_IDLE"
HOST_IDLE_EXIT_CODE = 75
CAP_IDLE_QUIESCE = "idle_quiesce_v1"


def idle_suspend_enabled() -> bool:
    return os.environ.get(HOST_IDLE_SUSPEND_ENV) == "1"


def activation_generation() -> str | None:
    from omnigent.host.identity_env import HOST_TOKEN_ENV_VAR

    token = os.environ.get(HOST_TOKEN_ENV_VAR)
    return hashlib.sha256(token.encode()).hexdigest() if token else None
