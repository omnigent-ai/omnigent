"""
Tests that every spawn-env builder threads the session workspace ``cwd`` into
its ``HARNESS_<H>_CWD`` env var.

Regression guard for the bug where a session's selected working folder was
honored by the Files panel / primary OS environment but NOT by the spawned
harness subprocess (codex, claude-sdk, cursor, qwen, goose, copilot, acp):
the builders accepted ``workdir`` (the agent bundle) but never the runtime
``cwd``, so the subprocess inherited the runner's launch directory instead
of the session workspace. Mirrors ``test_pi_spawn_env.py`` — pi/kimi already
threaded ``cwd``; this locks the rest.

Unit test — no subprocess spawn, no real CLIs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import (
    _build_acp_spawn_env,
    _build_claude_sdk_spawn_env,
    _build_codex_spawn_env,
    _build_copilot_spawn_env,
    _build_cursor_spawn_env,
    _build_goose_spawn_env,
    _build_qwen_spawn_env,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec

# (harness name, builder callable, HARNESS_<H>_CWD env var)
_BUILDERS = [
    ("claude-sdk", _build_claude_sdk_spawn_env, "HARNESS_CLAUDE_SDK_CWD"),
    ("codex", _build_codex_spawn_env, "HARNESS_CODEX_CWD"),
    ("cursor", _build_cursor_spawn_env, "HARNESS_CURSOR_CWD"),
    ("qwen", _build_qwen_spawn_env, "HARNESS_QWEN_CWD"),
    ("goose", _build_goose_spawn_env, "HARNESS_GOOSE_CWD"),
    ("copilot", _build_copilot_spawn_env, "HARNESS_COPILOT_CWD"),
    ("acp", _build_acp_spawn_env, "HARNESS_ACP_CWD"),
]


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OMNIGENT_CONFIG_HOME at an empty temp dir so the developer's real
    ``~/.omnigent/config.yaml`` cannot influence provider/model resolution.

    Also stub ``detect_providers`` so ambient CLI config files (e.g.
    ``~/.codex/config.toml``) on a developer's machine cannot leak into the
    provider resolution path and cause spurious failures (matches the
    isolation pattern in ``test_runner_dispatch.py`` / ``test_cli.py``).
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)


def _make_spec(harness: str) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name=f"test-{harness}",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


@pytest.mark.parametrize("harness,builder,cwd_var", _BUILDERS)
def test_builder_threads_session_cwd_distinct_from_bundle(
    tmp_path: Path, harness: str, builder, cwd_var: str
) -> None:
    """The session workspace lands in ``HARNESS_<H>_CWD``, separate from the
    bundle ``workdir``. Conflating them launches the harness in the wrong repo."""
    workspace = tmp_path / "selected-workspace"
    workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / f"ag_{harness}"
    bundle_dir.mkdir(parents=True)

    env = builder(_make_spec(harness), cwd=workspace, workdir=bundle_dir)

    assert env[cwd_var] == str(workspace)


@pytest.mark.parametrize("harness,builder,cwd_var", _BUILDERS)
def test_builder_omits_cwd_when_none(harness: str, builder, cwd_var: str) -> None:
    """When no session workspace is provided the CWD var is absent, so the
    harness applies its own OMNIGENT_RUNNER_WORKSPACE / inherited-cwd fallback."""
    env = builder(_make_spec(harness), cwd=None, workdir=None)

    assert cwd_var not in env
