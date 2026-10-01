"""
Environments primitive — wraps the existing inner implementation.

This module is the canonical OS-environment surface for new code
(per ``designs/SERVER_HARNESS_CONTRACT.md`` §Environments — unified
subprocess spawning). It re-exports the ``OSEnvironment`` ABC, the
in-process ``CallerProcessOSEnvironment`` impl, the
``create_os_environment`` factory, and the ``OSEnvSpec`` /
``OSEnvSandboxSpec`` dataclasses from ``omnigent.inner`` without
copying or re-implementing any logic.

New consumers (the ``sys_os_*`` builtins, the ``sys_terminal_*``
facade once it lands, the eventual unified ``run_subprocess``
primitive) target this module instead of reaching into
``inner.os_env`` / ``inner.datamodel`` directly. Legacy consumers
inside ``inner/`` keep importing from those modules unchanged
during the transition; both import locations stay valid until the
inner sunset gate is met (see §Deletions).

The unified subprocess primitive (``run_subprocess(env, cmd,
...)``) the contract describes is intentionally NOT defined yet:
no consumer in this PR needs it, and adding it speculatively would
violate "bare minimum implementation". It will land alongside the
first consumer that actually needs subprocess argv shaping driven
by an ``OSEnvSpec`` (Phase 2 step 9 / 12 / 11 candidates).
"""

from __future__ import annotations

from omnigent.core.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.environments.os_env import (
    CallerProcessOSEnvironment,
    OSEnvironment,
    create_os_environment,
    default_os_env_spec_for_type,
)

__all__ = [
    "CallerProcessOSEnvironment",
    "OSEnvSandboxSpec",
    "OSEnvSpec",
    "OSEnvironment",
    "create_os_environment",
    "default_os_env_spec_for_type",
]
