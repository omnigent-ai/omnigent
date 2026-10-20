"""Routing SDK harness requests to a Unity Gateway model provider service."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from omnigent.databricks_ai_gateway import (
    MODEL_PROVIDER_SERVICE_ENV_VAR,
    MODEL_PROVIDER_SERVICE_HEADER,
    model_provider_service_headers,
    validate_model_provider_service,
)
from omnigent.inference_config import inference_config_scope
from omnigent.spec.types import AgentSpec, ApiKeyAuth, DatabricksAuth, ExecutorSpec

_BEDROCK = "main.ai.bedrock_prod"


@pytest.mark.parametrize(
    "name",
    ["a.b", "a.b.c.d", "", "a.b.c\nX-Injected: 1", "a.b.c d", "a..c"],
)
def test_invalid_model_provider_service_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="three-part Unity Catalog name"):
        validate_model_provider_service(name)


def test_headers_follow_the_spawn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODEL_PROVIDER_SERVICE_ENV_VAR, raising=False)
    assert model_provider_service_headers() == {}

    monkeypatch.setenv(MODEL_PROVIDER_SERVICE_ENV_VAR, f" {_BEDROCK} ")
    assert model_provider_service_headers() == {MODEL_PROVIDER_SERVICE_HEADER: _BEDROCK}


def test_claude_gateway_env_adds_the_provider_header(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner.claude_sdk_executor import _resolve_gateway_env

    monkeypatch.setenv(MODEL_PROVIDER_SERVICE_ENV_VAR, _BEDROCK)
    env = _resolve_gateway_env(
        host_override="https://example.databricks.com/",
        base_url_override="https://example.databricks.com/ai-gateway/anthropic",
        auth_command_override="printf token",
    )

    assert env["ANTHROPIC_CUSTOM_HEADERS"].splitlines() == [
        "x-databricks-use-coding-agent-mode: true",
        f"{MODEL_PROVIDER_SERVICE_HEADER}: {_BEDROCK}",
    ]


def test_claude_provider_service_skips_gateway_alias_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's served ids are unknown to the provider, so aliases stay unpinned."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    monkeypatch.setenv(MODEL_PROVIDER_SERVICE_ENV_VAR, _BEDROCK)
    executor = ClaudeSDKExecutor(
        gateway=True,
        gateway_host="https://example.databricks.com",
        base_url_override="https://example.databricks.com/ai-gateway/anthropic",
        gateway_auth_command="printf token",
        model="us.anthropic.claude-sonnet-5",
    )
    env = {"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"}

    with patch("omnigent.models.model_catalog.listing_for_provider") as listing:
        overrides = asyncio.run(executor._apply_gateway_model_vocabulary(env, "printf token"))

    listing.assert_not_called()
    assert overrides == {}
    assert not any(key.startswith("ANTHROPIC_DEFAULT_") for key in env)


@pytest.mark.parametrize("service", [None, "main.ai.azure-openai-prod"])
def test_openai_agents_databricks_client_sends_the_provider_header(
    monkeypatch: pytest.MonkeyPatch, service: str | None
) -> None:
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda profile=None, **_kw: (
            httpx.BasicAuth("token", ""),
            "https://example.databricks.com",
        ),
    )
    if service is None:
        monkeypatch.delenv(MODEL_PROVIDER_SERVICE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MODEL_PROVIDER_SERVICE_ENV_VAR, service)

    client = _get_openai_async_client(profile="DEV", model="gpt-6-astra")

    assert client.default_headers.get(MODEL_PROVIDER_SERVICE_HEADER) == service


def _spec(config: dict[str, str]) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="provider-routed",
        executor=ExecutorSpec(
            type="omnigent",
            config=config,
            model="us.anthropic.claude-sonnet-5",
            auth=ApiKeyAuth(api_key="synthetic-test-key", base_url="http://127.0.0.1:1"),
        ),
    )


def test_runner_spawn_env_carries_the_spec_provider_service() -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    with inference_config_scope({}):
        routed = _build_spawn_env_from_spec(
            _spec({"harness": "claude-sdk", "model_provider_service": _BEDROCK}), "claude-sdk"
        )
        plain = _build_spawn_env_from_spec(_spec({"harness": "claude-sdk"}), "claude-sdk")

    assert routed is not None and plain is not None
    assert routed[MODEL_PROVIDER_SERVICE_ENV_VAR] == _BEDROCK
    assert MODEL_PROVIDER_SERVICE_ENV_VAR not in plain


def test_runner_spawn_env_carries_provider_service_for_antigravity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Databricks-routed antigravity agent sends the provider header too."""
    from omnigent.runner.app import _build_spawn_env_from_spec
    from omnigent.runtime import workflow

    monkeypatch.setattr(
        workflow,
        "get_workspace_url_for_profile",
        lambda profile: "https://example.cloud.databricks.com",
    )
    spec = AgentSpec(
        spec_version=1,
        name="gemini-routed",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "antigravity", "model_provider_service": "main.ai.gemini_prod"},
            model="gemini-3.8-flash",
            auth=DatabricksAuth(profile="dev"),
        ),
    )
    with inference_config_scope({}):
        env = _build_spawn_env_from_spec(spec, "antigravity")

    assert env is not None
    assert env[MODEL_PROVIDER_SERVICE_ENV_VAR] == "main.ai.gemini_prod"
    assert env["HARNESS_ANTIGRAVITY_GATEWAY_BASE_URL"] == (
        "https://example.cloud.databricks.com/ai-gateway/gemini"
    )


@pytest.mark.parametrize("harness", ["codex", "pi"])
def test_runner_rejects_a_provider_service_on_unsupported_harnesses(harness: str) -> None:
    """Harnesses that cannot send the header must not silently drop the setting."""
    from omnigent.runner.app import _build_spawn_env_from_spec

    spec = _spec({"harness": harness, "model_provider_service": _BEDROCK})
    with inference_config_scope({}), pytest.raises(ValueError, match="not supported by the"):
        _build_spawn_env_from_spec(spec, harness)


def test_runner_rejects_a_malformed_provider_service() -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    with inference_config_scope({}), pytest.raises(ValueError, match="three-part"):
        _build_spawn_env_from_spec(
            _spec({"harness": "claude-sdk", "model_provider_service": "a.b\nX: y"}), "claude-sdk"
        )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("databricks-claude-sonnet-5", True),
        ("system.ai.gpt-6-luna", True),
        ("databricks/databricks-claude-opus-5-5", True),
        ("us.anthropic.claude-sonnet-5", False),
        ("gpt-6-astra", False),
    ],
)
def test_databricks_catalog_model_detection(model: str, expected: bool) -> None:
    from omnigent.databricks_ai_gateway import is_databricks_catalog_model

    assert is_databricks_catalog_model(model) is expected


def _env_with_override(config: dict[str, str], model_override: str) -> dict[str, str]:
    from omnigent.runner.app import _build_spawn_env_from_spec

    with inference_config_scope({}):
        env = _build_spawn_env_from_spec(
            _spec(config), "claude-sdk", model_override=model_override
        )
    assert env is not None
    return env


@pytest.mark.parametrize("override", ["databricks-claude-sonnet-5", "system.ai.claude-sonnet-5"])
def test_provider_pinned_agent_ignores_a_databricks_hosted_override(override: str) -> None:
    """The provider cannot serve a Databricks-hosted id; the agent keeps its provider model."""
    env = _env_with_override(
        {"harness": "claude-sdk", "model_provider_service": _BEDROCK}, override
    )

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "us.anthropic.claude-sonnet-5"
    assert env[MODEL_PROVIDER_SERVICE_ENV_VAR] == _BEDROCK


def test_provider_pinned_agent_accepts_a_provider_model_override() -> None:
    env = _env_with_override(
        {"harness": "claude-sdk", "model_provider_service": _BEDROCK},
        "us.anthropic.claude-opus-5-5",
    )

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "us.anthropic.claude-opus-5-5"
    assert env[MODEL_PROVIDER_SERVICE_ENV_VAR] == _BEDROCK


def test_unpinned_agent_still_takes_a_databricks_hosted_override() -> None:
    env = _env_with_override({"harness": "claude-sdk"}, "databricks-claude-opus-5-5")

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-opus-5-5"
    assert MODEL_PROVIDER_SERVICE_ENV_VAR not in env
