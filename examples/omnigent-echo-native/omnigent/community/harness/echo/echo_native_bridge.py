"""Spawn-env builder hook for the echo-native example harness.

``build_echo_native_spawn_env`` is the ``spawn_env_builder`` hook: the runner
calls it to derive per-spawn environment variables from the agent spec before
launching the harness subprocess. Unlike the launch adapter this is a pure,
side-effect-free mapping, so the example implements it for real.
"""

from __future__ import annotations

from typing import Any


def build_echo_native_spawn_env(agent_spec: Any) -> dict[str, str]:
    """Return env vars to inject into the echo-native spawn (``spawn_env_builder``).

    A real builder reads vendor config off ``agent_spec`` (model override, auth
    passthrough, sandbox hints). The example emits a single marker var so the
    hook is observable end to end without needing a live vendor.

    :param agent_spec: The session's resolved agent spec (may be ``None``).
    :returns: Environment variables for the harness subprocess.
    """
    env = {"OMNIGENT_ECHO_NATIVE_EXAMPLE": "1"}
    model = getattr(agent_spec, "model", None)
    if isinstance(model, str) and model:
        env["OMNIGENT_ECHO_MODEL"] = model
    return env
