"""OS environments for agent subprocesses: the ``OSEnvironment`` contract and its factory.

Exports resolve lazily so ``python -m omnigent.environments.os_env`` (the
in-sandbox helper) does not import its own module twice.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.core.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.environments.os_env import (
        CallerProcessOSEnvironment,
        OSEnvironment,
        create_os_environment,
        default_os_env_spec_for_type,
    )

_EXPORTS = {
    "OSEnvSandboxSpec": "omnigent.core.datamodel",
    "OSEnvSpec": "omnigent.core.datamodel",
    "CallerProcessOSEnvironment": "omnigent.environments.os_env",
    "OSEnvironment": "omnigent.environments.os_env",
    "create_os_environment": "omnigent.environments.os_env",
    "default_os_env_spec_for_type": "omnigent.environments.os_env",
}

__all__ = [
    "CallerProcessOSEnvironment",
    "OSEnvSandboxSpec",
    "OSEnvSpec",
    "OSEnvironment",
    "create_os_environment",
    "default_os_env_spec_for_type",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
