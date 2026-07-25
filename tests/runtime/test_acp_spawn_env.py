"""Tests for ``_build_acp_spawn_env`` in ``omnigent/runtime/workflow.py``.

The builder resolves the picked ``acp:<slug>`` (carried in
``spec.executor.config["harness"]``) to a user-configured agent in the ``acp:``
config block and maps it to the ``HARNESS_ACP_*`` env vars the generic ACP
harness wrap reads. Like Goose, the agent owns its own auth, so no credential is
wired; a ``databricks-*`` model is dropped in favour of the agent's own model.

Unit test — no subprocess spawn. End-to-end verification of the wrap → executor
path lives in ``tests/inner/test_acp_executor.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.runtime.workflow import _build_acp_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig

_AGENTS = [
    {"name": "Gemini CLI", "command": "gemini --experimental-acp"},
    {"name": "Goose", "command": "goose acp", "model": "gpt-5.3", "session_id_mode": "client"},
]


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OMNIGENT_CONFIG_HOME at a temp dir so the real config can't leak in."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write_acp_config(tmp_path: Path, agents: list[dict] | None = None) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"acp": {"agents": agents if agents is not None else _AGENTS}})
    )


def _make_spec(*, harness: str, model: str | None = None) -> AgentSpec:
    config: dict[str, object] = {"harness": harness}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="test-acp",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_slug_resolves_to_command(_isolate_config: Path) -> None:
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose"))
    assert env["HARNESS_ACP_COMMAND"] == "goose acp"
    assert env["HARNESS_ACP_NAME"] == "Goose"
    assert env["HARNESS_ACP_SESSION_ID_MODE"] == "client"
    # Per-agent model applies when the spec pins none.
    assert env["HARNESS_ACP_MODEL"] == "gpt-5.3"


def test_other_slug_resolves_independently(_isolate_config: Path) -> None:
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:gemini-cli"))
    assert env["HARNESS_ACP_COMMAND"] == "gemini --experimental-acp"
    assert env["HARNESS_ACP_NAME"] == "Gemini CLI"


def test_bare_acp_falls_back_to_first_agent(_isolate_config: Path) -> None:
    """A bare ``acp`` id (slug lost) still launches the first configured agent."""
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp"))
    assert env["HARNESS_ACP_COMMAND"] == "gemini --experimental-acp"


def test_unknown_slug_falls_back_to_first_agent(_isolate_config: Path) -> None:
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:nonexistent"))
    assert env["HARNESS_ACP_COMMAND"] == "gemini --experimental-acp"


def test_no_agents_omits_command(_isolate_config: Path) -> None:
    """With nothing configured, no command is written — the wrap errors at request time."""
    _write_acp_config(_isolate_config, agents=[])
    env = _build_acp_spawn_env(_make_spec(harness="acp"))
    assert "HARNESS_ACP_COMMAND" not in env


def test_spec_model_overrides_agent_model(_isolate_config: Path) -> None:
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose", model="claude-sonnet-4-6"))
    assert env["HARNESS_ACP_MODEL"] == "claude-sonnet-4-6"


def test_databricks_model_dropped_agent_model_used(_isolate_config: Path) -> None:
    """A ``databricks-*`` gateway id isn't a valid third-party model — drop it,
    fall back to the agent's own configured model."""
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose", model="databricks-claude-sonnet-4"))
    assert env["HARNESS_ACP_MODEL"] == "gpt-5.3"


def test_send_model_flag_forwarded(_isolate_config: Path) -> None:
    _write_acp_config(
        _isolate_config,
        agents=[{"name": "Qwen", "command": "qwen --acp", "send_model": True}],
    )
    env = _build_acp_spawn_env(_make_spec(harness="acp:qwen"))
    assert env["HARNESS_ACP_SEND_MODEL"] == "1"
