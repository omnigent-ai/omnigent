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

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.workflow import _build_acp_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig, ProviderAuth

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
    provider: str | None = None,
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
        executor=ExecutorSpec(
            type="omnigent",
            config=config,
            model=model,
            auth=ProviderAuth(name=provider) if provider else None,
        ),
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


# ── Curated model list + env denylist forwarding ────────────────────────────


def _write_provider_config(tmp_path: Path, models: dict[str, str] | None = None) -> None:
    provider = {
        "kind": "gateway",
        "default": True,
        "anthropic": {
            "base_url": "https://gw.example.com/anthropic",
            "api_key": "sk-anthropic",
        },
        "openai": {
            "base_url": "https://gw.example.com/openai",
            "api_key": "sk-openai",
            "wire_api": "chat",
            "models": models or {},
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"acp": {"agents": _AGENTS}, "providers": {"bifrost": provider}})
    )


def test_curated_model_list_is_independent_of_selection(_isolate_config: Path) -> None:
    """Selecting a model never changes the provider's approved catalog."""
    _write_provider_config(
        _isolate_config,
        {"default": "gpt-5.4", "reasoner": "deepseek-v4-pro"},
    )
    env = _build_acp_spawn_env(
        _make_spec(harness="acp:goose", model="deepseek-v4-pro", provider="bifrost")
    )
    assert env["HARNESS_ACP_MODEL"] == "deepseek-v4-pro"
    assert env["HARNESS_ACP_MODEL_LIST"] == "gpt-5.4,deepseek-v4-pro"


def test_provider_default_model_pins_launch_model(_isolate_config: Path) -> None:
    """An explicitly bound ACP agent honors its provider's default."""
    _write_provider_config(
        _isolate_config,
        {"default": "deepseek-v4-pro", "flash": "deepseek-v4-flash"},
    )
    env = _build_acp_spawn_env(_make_spec(harness="acp:gemini-cli", provider="bifrost"))
    assert env["HARNESS_ACP_MODEL"] == "deepseek-v4-pro"
    assert env["HARNESS_ACP_MODEL_LIST"] == "deepseek-v4-pro,deepseek-v4-flash"


def test_unrelated_global_provider_leaves_acp_configuration_unchanged(
    _isolate_config: Path,
) -> None:
    _write_provider_config(_isolate_config, {"default": "model-a", "small": "model-b"})
    env = _build_acp_spawn_env(_make_spec(harness="acp:gemini-cli"))
    assert "HARNESS_ACP_MODEL" not in env
    assert "HARNESS_ACP_MODEL_LIST" not in env


def test_default_only_provider_does_not_restrict_agent_models(_isolate_config: Path) -> None:
    _write_provider_config(_isolate_config, {"default": "model-a", "alias": "model-a"})
    env = _build_acp_spawn_env(
        _make_spec(harness="acp:goose", model="model-b", provider="bifrost")
    )
    assert env["HARNESS_ACP_MODEL"] == "model-b"
    assert "HARNESS_ACP_MODEL_LIST" not in env


def test_curated_spawn_rejects_unlisted_model(_isolate_config: Path) -> None:
    _write_provider_config(_isolate_config, {"default": "model-a", "small": "model-b"})
    with pytest.raises(OmnigentError, match="configured model list"):
        _build_acp_spawn_env(_make_spec(harness="acp:goose", model="model-c", provider="bifrost"))


@pytest.mark.parametrize("embedded", [False, True])
def test_explicit_acp_databricks_model_is_preserved(
    _isolate_config: Path,
    embedded: bool,
) -> None:
    agent = {"name": "Helper", "command": "helper --acp", "model": "databricks-model-a"}
    _write_acp_config(_isolate_config, agents=[agent])
    spec = _make_spec(harness="acp:helper", acp_agent=agent if embedded else _MISSING)
    assert _build_acp_spawn_env(spec)["HARNESS_ACP_MODEL"] == "databricks-model-a"


def test_embedded_agent_without_model_uses_selected_provider_default(
    _isolate_config: Path,
) -> None:
    _write_provider_config(_isolate_config, {"default": "model-a", "small": "model-b"})
    env = _build_acp_spawn_env(
        _make_spec(
            harness="acp",
            provider="bifrost",
            acp_agent={"name": "Helper", "command": "helper --acp"},
        )
    )
    assert env["HARNESS_ACP_MODEL"] == "model-a"
    assert env["HARNESS_ACP_MODEL_LIST"] == "model-a,model-b"


def test_curated_provider_without_default_has_stable_launch_model(_isolate_config: Path) -> None:
    _write_provider_config(_isolate_config, {"fast": "model-a", "large": "model-b"})
    env = _build_acp_spawn_env(_make_spec(harness="acp:gemini-cli", provider="bifrost"))
    assert env["HARNESS_ACP_MODEL"] == "model-a"
    assert env["HARNESS_ACP_MODEL_LIST"] == "model-a,model-b"


@pytest.mark.parametrize("default_source", ["spec", "configured-agent", "embedded-agent"])
@pytest.mark.parametrize("model_override", [None, "model-b"])
def test_runner_rejects_unlisted_default_even_with_valid_override(
    _isolate_config: Path,
    default_source: str,
    model_override: str | None,
) -> None:
    """A valid pick cannot hide an invalid model that clearing the override would restore."""
    from omnigent.runner.app import _build_spawn_env_from_spec

    _write_provider_config(_isolate_config, {"default": "model-a", "large": "model-b"})
    spec = _make_spec(
        harness="acp:goose",
        model="old-model" if default_source == "spec" else None,
        provider="bifrost",
        acp_agent=(
            {"name": "Helper", "command": "helper --acp", "model": "old-model"}
            if default_source == "embedded-agent"
            else _MISSING
        ),
    )
    with pytest.raises(OmnigentError, match="configured model list") as exc_info:
        _build_spawn_env_from_spec(spec, "acp", model_override=model_override)
    rejected_model = "gpt-5.3" if default_source == "configured-agent" else "old-model"
    assert rejected_model in str(exc_info.value)


def test_runner_valid_override_preserves_original_default(_isolate_config: Path) -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    _write_provider_config(_isolate_config, {"default": "model-a", "large": "model-b"})
    spec = _make_spec(harness="acp:goose", model="model-a", provider="bifrost")
    env = _build_spawn_env_from_spec(spec, "acp", model_override="model-b")
    assert env is not None
    assert env["HARNESS_ACP_MODEL"] == "model-b"
    assert env["HARNESS_ACP_DEFAULT_MODEL"] == "model-a"
    assert env["HARNESS_ACP_MODEL_LIST"] == "model-a,model-b"
    assert spec.executor.model == "model-a"


@pytest.mark.parametrize("failure", ["missing-provider", "resolution-error"])
@pytest.mark.parametrize("model_override", [None, "model-b"])
def test_runner_rejects_failed_explicit_provider_resolution(
    _isolate_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    model_override: str | None,
) -> None:
    """Neither a pinned model nor an override permits launch without resolving its policy."""
    from omnigent.runner.app import _build_spawn_env_from_spec

    _write_acp_config(_isolate_config)
    if failure == "resolution-error":

        def fail_resolution(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("provider configuration unavailable")

        monkeypatch.setattr(
            "omnigent.runtime.workflow._resolve_provider_for_build", fail_resolution
        )
    spec = _make_spec(harness="acp:goose", model="model-a", provider="bifrost")
    expected_code = (
        ErrorCode.INVALID_INPUT if failure == "missing-provider" else ErrorCode.INTERNAL_ERROR
    )
    with pytest.raises(OmnigentError) as error:
        _build_spawn_env_from_spec(spec, "acp", model_override=model_override)
    assert error.value.code == expected_code
    with pytest.raises(OmnigentError):
        _build_acp_spawn_env(spec)


def test_curated_model_list_absent_when_nothing_curated(_isolate_config: Path) -> None:
    """No models: map anywhere → no HARNESS_ACP_MODEL_LIST (uncurated op)."""
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose", model="gpt-5.3"))
    assert "HARNESS_ACP_MODEL_LIST" not in env


def test_env_unset_denylist_forwarded(
    monkeypatch: pytest.MonkeyPatch, _isolate_config: Path
) -> None:
    """OMNIGENT_ACP_ENV_UNSET rides the spawn env so the wrap can scrub the CLI env."""
    _write_acp_config(_isolate_config)
    monkeypatch.setenv("OMNIGENT_ACP_ENV_UNSET", "ANTHROPIC_AUTH_TOKEN,OPENAI_API_KEY")
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose"))
    assert env["HARNESS_ACP_ENV_UNSET"] == "ANTHROPIC_AUTH_TOKEN,OPENAI_API_KEY"


def test_env_unset_absent_by_default(_isolate_config: Path) -> None:
    """Unset operator denylist forwards nothing — no scrubbing (safe default)."""
    _write_acp_config(_isolate_config)
    env = _build_acp_spawn_env(_make_spec(harness="acp:goose"))
    assert "HARNESS_ACP_ENV_UNSET" not in env
