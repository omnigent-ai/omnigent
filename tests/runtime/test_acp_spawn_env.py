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
    {
        "name": "OpenClaw",
        "command": "openclaw acp --url https://gateway --token token",
        "omnigent_mcp": False,
    },
]
_MISSING = object()


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OMNIGENT_CONFIG_HOME at a temp dir so the real config can't leak in."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write_acp_config(tmp_path: Path, agents: list[dict] | None = None) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"acp": {"agents": agents if agents is not None else _AGENTS}})
    )


def _make_spec(
    *,
    harness: str,
    model: str | None = None,
    acp_agent: object = _MISSING,
    permission_mode: str | None = None,
) -> AgentSpec:
    config: dict[str, object] = {"harness": harness}
    if model is not None:
        config["model"] = model
    if acp_agent is not _MISSING:
        config["acp_agent"] = acp_agent
    if permission_mode is not None:
        config["permission_mode"] = permission_mode
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
    assert env["HARNESS_ACP_OMNIGENT_MCP"] == "1"
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


def test_omnigent_mcp_flag_forwarded(_isolate_config: Path) -> None:
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:openclaw"))
    assert env["HARNESS_ACP_COMMAND"] == "openclaw acp --url https://gateway --token token"
    assert env["HARNESS_ACP_OMNIGENT_MCP"] == "0"


def test_embedded_omnigent_mcp_flag_forwarded() -> None:
    env = _build_acp_spawn_env(
        _make_spec(
            harness="acp:openclaw",
            acp_agent={
                "name": "OpenClaw",
                "command": "openclaw acp",
                "omnigent_mcp": False,
            },
        )
    )
    assert env["HARNESS_ACP_OMNIGENT_MCP"] == "0"


@pytest.mark.parametrize(
    "acp_agent",
    [
        None,
        "not-a-mapping",
        {},
        {"name": "Helper"},
        {"name": "Helper", "command": " "},
        {"name": "Helper", "command": "helper", "omnigent_mcp": "false"},
    ],
)
def test_malformed_embedded_agent_fails_loudly(acp_agent: object) -> None:
    with pytest.raises(ValueError, match="executor acp_agent"):
        _build_acp_spawn_env(_make_spec(harness="acp:helper", acp_agent=acp_agent))


def test_env_passthrough_names_are_forwarded(_isolate_config: Path) -> None:
    """Declared names reach the wrap so the agent can authenticate."""
    _write_acp_config(
        _isolate_config,
        agents=[
            {
                "name": "Grok Build",
                "command": "grok agent stdio",
                "env_passthrough": ["XAI_API_KEY", "GROK_TOKEN"],
            }
        ],
    )
    env = _build_acp_spawn_env(_make_spec(harness="acp:grok-build"))
    assert env["HARNESS_ACP_ENV_PASSTHROUGH"] == "XAI_API_KEY,GROK_TOKEN"


def test_env_passthrough_absent_when_undeclared(_isolate_config: Path) -> None:
    """No declaration writes no var, so the spawn env stays deny-by-default."""
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose"))
    assert "HARNESS_ACP_ENV_PASSTHROUGH" not in env


def test_embedded_agent_forwards_env_passthrough(_isolate_config: Path) -> None:
    """A spec-embedded one-shot agent declares names the same way."""
    _write_acp_config(_isolate_config, agents=[])
    spec = _make_spec(
        harness="acp:embedded",
        acp_agent={
            "name": "Embedded",
            "command": "agent stdio",
            "env_passthrough": ["XAI_API_KEY"],
        },
    )
    env = _build_acp_spawn_env(spec)
    assert env["HARNESS_ACP_ENV_PASSTHROUGH"] == "XAI_API_KEY"


def test_embedded_agent_overrides_config_lookup(_isolate_config: Path) -> None:
    """An embedded agent is used even when the slug is configured.

    When a one-shot embedded agent is present, it takes precedence over a
    config-based lookup so the client-side resolution can work with remote
    servers: the local runner resolves acp:<slug> and embeds it, bypassing
    the need for the server to have that agent configured.
    """
    _write_acp_config(_isolate_config)  # write config with goose and others
    # Now pass an embedded agent with a different command
    env = _build_acp_spawn_env(
        _make_spec(
            harness="acp:goose",
            acp_agent={
                "name": "Different Goose",
                "command": "goose acp --custom-flag",
            },
        )
    )
    # The embedded command is used, not the one from config
    assert env["HARNESS_ACP_COMMAND"] == "goose acp --custom-flag"
    assert env["HARNESS_ACP_NAME"] == "Different Goose"


@pytest.mark.parametrize(
    "agent_fields",
    [
        # Qwen-shaped agent: client-side session id + send model
        {
            "name": "Qwen",
            "command": "qwen --acp",
            "session_id_mode": "client",
            "send_model": True,
            "model": "qwen-vl-max",
            "expected_session_id_mode": "client",
            "expected_send_model": True,
            "expected_model": "qwen-vl-max",
        },
        # Agent with MCP disabled
        {
            "name": "Custom",
            "command": "custom acp",
            "omnigent_mcp": False,
            "expected_omnigent_mcp": False,
        },
        # Agent with environment passthrough
        {
            "name": "GrokAgent",
            "command": "grok agent stdio",
            "env_passthrough": ["XAI_API_KEY"],
            "expected_env_passthrough": "XAI_API_KEY",
        },
        # Agent with model only
        {
            "name": "TestAgent",
            "command": "test --acp",
            "model": "test-model-v1",
            "expected_model": "test-model-v1",
        },
    ],
)
def test_embedded_agent_round_trip_preserves_all_fields(
    agent_fields: dict,
) -> None:
    """Embedded agents preserve all fields through the round-trip.

    What breaks if this fails: an agent configured with non-default values
    (e.g. Qwen with `session_id_mode: client`, OpenClaw with
    `omnigent_mcp: false`) silently loses these settings when launched via
    `--harness acp:<slug>`, causing incorrect spawn behavior at runtime.
    Example: Qwen expects the runner to generate the session id but switches
    to server mode, fails to send the model, and breaks Qwen's response
    generation.
    """
    agent_dict = {
        "name": agent_fields["name"],
        "command": agent_fields["command"],
    }
    # Add optional fields that were declared
    for field in ("model", "session_id_mode", "send_model", "omnigent_mcp", "env_passthrough"):
        if field in agent_fields:
            agent_dict[field] = agent_fields[field]

    env = _build_acp_spawn_env(_make_spec(harness="acp:test", acp_agent=agent_dict))

    # Core fields always present
    assert env["HARNESS_ACP_COMMAND"] == agent_fields["command"]
    assert env["HARNESS_ACP_NAME"] == agent_fields["name"]

    # session_id_mode preservation
    if "expected_session_id_mode" in agent_fields:
        assert env["HARNESS_ACP_SESSION_ID_MODE"] == agent_fields["expected_session_id_mode"], (
            f"session_id_mode not preserved: got {env.get('HARNESS_ACP_SESSION_ID_MODE')}"
        )

    # send_model preservation
    if "expected_send_model" in agent_fields:
        if agent_fields["expected_send_model"]:
            assert env["HARNESS_ACP_SEND_MODEL"] == "1"
        else:
            assert "HARNESS_ACP_SEND_MODEL" not in env

    # omnigent_mcp preservation
    if "expected_omnigent_mcp" in agent_fields:
        expected_mcp = "1" if agent_fields["expected_omnigent_mcp"] else "0"
        assert env["HARNESS_ACP_OMNIGENT_MCP"] == expected_mcp

    # model preservation
    if "expected_model" in agent_fields:
        assert env["HARNESS_ACP_MODEL"] == agent_fields["expected_model"]

    # env_passthrough preservation
    if "expected_env_passthrough" in agent_fields:
        assert env["HARNESS_ACP_ENV_PASSTHROUGH"] == agent_fields["expected_env_passthrough"]


def test_permission_mode_threads_into_env_var(_isolate_config: Path) -> None:
    """``executor.config.permission_mode`` sets ``HARNESS_ACP_PERMISSION_MODE``.

    Closes spec -> spawn env; the wrap's half is covered in
    ``tests/inner/test_acp_executor.py``. Without this the option is inert:
    the child never learns the user opted out of approval cards.
    """
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(
        _make_spec(harness="acp:goose", permission_mode="bypassPermissions")
    )
    assert env["HARNESS_ACP_PERMISSION_MODE"] == "bypassPermissions"


def test_no_permission_mode_omits_env_var(_isolate_config: Path) -> None:
    """Absent ``permission_mode`` leaves the harness wrap on its ``auto`` default."""
    _write_acp_config(_isolate_config)
    assert "HARNESS_ACP_PERMISSION_MODE" not in _build_acp_spawn_env(
        _make_spec(harness="acp:goose")
    )
