"""Desktop keyring grants through runner dispatch and the vendor CLI boundary."""

from __future__ import annotations

import os

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.goose_executor import GooseExecutor
from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.runtime.harnesses.process_manager import _build_harness_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec


@pytest.mark.parametrize("sandbox_type", [None, "none", "auto", "linux_bwrap", "darwin_seatbelt"])
@pytest.mark.parametrize("explicit", [False, True])
def test_goose_desktop_grant_requires_unsandboxed_spec(
    monkeypatch: pytest.MonkeyPatch, sandbox_type: str | None, explicit: bool
) -> None:
    session_env = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    monkeypatch.setattr(os, "environ", {**session_env, "PATH": "/usr/bin:/bin"})
    os_env = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type=sandbox_type, env_passthrough=list(session_env) if explicit else None
        )
        if sandbox_type is not None
        else None,
    )
    spec = AgentSpec(
        spec_version=1,
        name="desktop-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "goose"}),
        os_env=os_env,
    )

    overrides = _build_spawn_env_from_spec(spec, "goose")
    harness_env = _build_harness_spawn_env(overrides)
    monkeypatch.setattr(os, "environ", harness_env)
    executor = GooseExecutor.__new__(GooseExecutor)
    executor._os_env = os_env
    executor._provider = None
    executor._model = None
    cli_env = executor._build_spawn_env()

    for env in (harness_env, cli_env):
        if explicit and sandbox_type == "none":
            assert {name: env[name] for name in session_env} == session_env
        else:
            assert session_env.keys().isdisjoint(env)
