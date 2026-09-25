"""Per-harness inference bindings survive snapshots and do not activate other gateways."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import tomllib
import yaml

from omnigent.errors import OmnigentError
from omnigent.harnesses.claude_native.main import resolve_native_claude_config
from omnigent.harnesses.codex_native.app_server import resolve_native_codex_launch
from omnigent.harnesses.opencode_native.provider import resolve_bound_opencode_gateway
from omnigent.harnesses.pi_native.credentials import (
    _live_family_model_entries,
    resolve_pi_native_provider,
)
from omnigent.host.connect import _build_runner_env, _write_runner_inference_config
from omnigent.inference_config import inference_config_scope, validate_inference_credentials
from omnigent.models.model_catalog import (
    _acp_launch_model,
    acp_curated_models,
    list_models_for_worker,
    validate_acp_model,
)
from omnigent.runtime.workflow import (
    _build_acp_cli_spawn_env,
    _build_claude_sdk_spawn_env,
    _build_codex_spawn_env,
    _resolve_provider_for_build,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_HARNESS_TMP_PARENT", str(tmp_path / "harness"))
    monkeypatch.delenv("OMNIGENT_INFERENCE_CONFIG", raising=False)
    monkeypatch.setenv("BIFROST_TEST_KEY", "test-bifrost-token")
    monkeypatch.setenv("UNITY_TEST_KEY", "test-unity-token")


def _profile() -> dict[str, object]:
    providers = {}
    for name in ("bifrost", "unity"):
        providers[name] = {
            "kind": "gateway",
            "anthropic": {
                "base_url": f"https://{name}.example/anthropic",
                "api_key_ref": f"env:{name.upper()}_TEST_KEY",
                "models": {"default": "model-a"},
            },
            "openai": {
                "base_url": f"https://{name}.example/v1",
                "api_key_ref": f"env:{name.upper()}_TEST_KEY",
                "models": {"default": "model-a"},
            },
        }
    harnesses = {
        harness: {
            "provider": "unity" if harness == "claude-native" else "bifrost",
            "default_model": "model-a",
            "model_allowlist": ["model-a", "databricks-literal/model-b"],
        }
        for harness in (
            "claude-native",
            "claude-sdk",
            "codex-native",
            "codex",
            "pi-native",
            "opencode-native",
            "jcode",
            "acp:custom",
        )
    }
    return {"providers": providers, "inference": {"harnesses": harnesses}}


def _spec(harness: str, model: str | None = None, provider: str | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="inference-test",
        instructions="Test routing.",
        executor=ExecutorSpec(
            type="omnigent",
            model=model,
            config={"harness": harness},
            auth=ProviderAuth(name=provider) if provider else None,
        ),
    )


def test_native_and_sdk_claude_keep_distinct_bindings() -> None:
    with inference_config_scope(_profile()):
        native = resolve_native_claude_config(spec=_spec("claude-native"))
        sdk = _build_claude_sdk_spawn_env(_spec("claude-sdk"))
    assert native is not None
    assert native.env["ANTHROPIC_BASE_URL"] == "https://unity.example/anthropic"
    assert native.routable_models == ("model-a", "databricks-literal/model-b")
    assert sdk["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://bifrost.example/anthropic"


def test_pi_legacy_config_error_keeps_native_login_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config._load_config", Mock(side_effect=OSError("unreadable"))
    )
    assert resolve_pi_native_provider() is None


def test_pi_invalid_saved_profile_does_not_fall_back_to_native_login(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(tmp_path / "missing-profile.json"))
    with pytest.raises(OmnigentError, match="saved inference configuration"):
        resolve_pi_native_provider()


@pytest.mark.parametrize("harness", ["claude-sdk", "codex", "pi", "qwen", "openai-agents"])
def test_unbound_host_keeps_its_explicit_provider_and_model(harness, tmp_path) -> None:
    from omnigent.runner.app import _HARNESS_MODEL_ENV_KEY, _build_spawn_env_from_spec

    config = _profile()
    config.pop("inference")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    spec = _spec(harness, "gpt-legacy", provider="bifrost")
    provider = _resolve_provider_for_build(spec, harness_type=harness)
    assert provider is not None and provider.name == "bifrost"
    env = _build_spawn_env_from_spec(spec, harness)
    assert env[_HARNESS_MODEL_ENV_KEY[harness]] == "gpt-legacy"
    assert "HARNESS_PI_PRESERVE_MODEL_IDS" not in env


@pytest.mark.parametrize("harness", ["codex", "claude-sdk", "pi", "qwen", "openai-agents"])
def test_bound_sdk_selected_model_precedes_an_obsolete_spec_pin(harness: str) -> None:
    from omnigent.runner.app import _HARNESS_MODEL_ENV_KEY, _build_spawn_env_from_spec

    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            harness: {
                "provider": "bifrost",
                "default_model": "model-a",
                "model_allowlist": ["model-a"],
            }
        }
    }
    with inference_config_scope(profile):
        env = _build_spawn_env_from_spec(
            _spec(harness, "obsolete-spec-model"), harness, model_override="model-a"
        )
    assert env is not None
    assert env[_HARNESS_MODEL_ENV_KEY[harness]] == "model-a"


def test_sdk_harness_override_resolves_its_own_provider_binding() -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    with inference_config_scope(_profile()):
        env = _build_spawn_env_from_spec(
            _spec("claude-native", "model-a"), "codex", model_override="model-a"
        )
    assert env is not None
    assert env["HARNESS_CODEX_GATEWAY_BASE_URL"] == "https://bifrost.example/v1"


def test_bound_claude_sdk_preserves_a_literal_anthropic_prefix() -> None:
    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "claude-sdk": {
                "provider": "bifrost",
                "default_model": "anthropic/private-model[large]",
                "model_allowlist": ["anthropic/private-model[large]"],
            }
        }
    }
    with inference_config_scope(profile):
        env = _build_claude_sdk_spawn_env(_spec("claude-sdk"))
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "anthropic/private-model[large]"


@pytest.mark.asyncio
async def test_bound_pi_sdk_preserves_literal_suffix_through_executor_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.inner.pi_harness import _build_pi_executor
    from omnigent.runtime.workflow import _build_pi_spawn_env

    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "pi": {
                "provider": "bifrost",
                "default_model": "private/model[large]",
                "model_allowlist": ["private/model[large]"],
            }
        }
    }
    with inference_config_scope(profile):
        env = _build_pi_spawn_env(_spec("pi"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("omnigent.inner.pi_harness.resolve_harness_path", lambda _: "/fake/pi")
    monkeypatch.setattr("omnigent.inner.pi_executor._fetch_shell_command_token", lambda _: "token")
    executor = _build_pi_executor()
    assert await executor._resolve_model(None) == "private/model[large]"
    assert env["HARNESS_PI_GATEWAY_OPENAI_WIRE_API"] == "responses"


@pytest.mark.asyncio
async def test_bound_claude_launch_never_falls_back_to_cli_login() -> None:
    from omnigent.runner.native.orchestration import _auto_create_claude_terminal

    resources = Mock(terminal_registry=None)
    resources.launch_required_terminal = AsyncMock()
    resolver = AsyncMock(side_effect=ValueError("credential unavailable"))
    async with httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"labels": {}})),
    ) as client:
        with inference_config_scope(_profile()), pytest.raises(ValueError, match="credential"):
            await _auto_create_claude_terminal(
                "bound-claude-failure",
                resources,
                lambda _session, _event: None,
                server_client=client,
                resolve_launch_config=resolver,
            )
    resources.launch_required_terminal.assert_not_called()


def test_codex_binding_routes_without_spec_auth_or_family_default() -> None:
    with inference_config_scope(_profile()):
        native = resolve_native_codex_launch(model=None, spec=_spec("codex-native"))
        sdk = _build_codex_spawn_env(_spec("codex", "databricks-literal/model-b"))
    assert native.model == "model-a"
    assert "https://bifrost.example/v1" in " ".join(native.config_overrides)
    assert native.profile is None
    assert sdk["HARNESS_CODEX_MODEL"] == "databricks-literal/model-b"
    assert sdk["HARNESS_CODEX_GATEWAY_BASE_URL"] == "https://bifrost.example/v1"


def test_explicit_conflicting_provider_is_rejected() -> None:
    with inference_config_scope(_profile()), pytest.raises(OmnigentError, match="conflicts"):
        _resolve_provider_for_build(
            _spec("claude-native", provider="bifrost"), harness_type="claude-sdk"
        )


def test_bound_codex_does_not_fallback_when_credential_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BIFROST_TEST_KEY")
    monkeypatch.delenv("OMNIGENT_BIFROST_TEST_KEY", raising=False)
    with inference_config_scope(_profile()), pytest.raises(ValueError, match="cannot route Codex"):
        resolve_native_codex_launch(model=None)


def test_pi_single_model_policy_preserves_literal_ids_and_stays_curated() -> None:
    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "pi-native": {
                "provider": "bifrost",
                "default_model": "databricks-literal/model-b",
                "model_allowlist": ["databricks-literal/model-b"],
            }
        }
    }
    with inference_config_scope(profile):
        provider = resolve_pi_native_provider()
        assert provider is not None
        assert provider.model == "databricks-literal/model-b"
        assert provider.curated_models
        assert [row["id"] for row in _live_family_model_entries(provider, transport=None)] == [
            "databricks-literal/model-b"
        ]
        with pytest.raises(OmnigentError, match="configured model list"):
            resolve_pi_native_provider(model="unlisted")


def test_acp_slug_binding_preserves_one_model_restriction() -> None:
    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "acp:custom": {
                "provider": "bifrost",
                "default_model": "databricks-literal/model-b",
                "model_allowlist": ["databricks-literal/model-b"],
            }
        }
    }
    spec = _spec("acp:custom")
    with inference_config_scope(profile):
        assert _acp_launch_model(spec) == "databricks-literal/model-b"
        assert acp_curated_models(spec) == ("databricks-literal/model-b",)
        validate_acp_model(spec, None)
        with pytest.raises(OmnigentError, match="configured model list"):
            validate_acp_model(spec, "model-a")
        assert acp_curated_models(_spec("acp:other")) == ()


def test_opencode_uses_bound_gateway_and_exact_model() -> None:
    with inference_config_scope(_profile()):
        gateway = resolve_bound_opencode_gateway(model="databricks-literal/model-b")
    assert gateway is not None
    assert gateway.base_url == "https://bifrost.example/v1"
    assert gateway.model_id == "databricks-literal/model-b"
    assert gateway.model_ids == ("model-a", "databricks-literal/model-b")


def test_bound_qwen_rejects_a_responses_only_endpoint() -> None:
    from omnigent.runtime.workflow import _build_qwen_spawn_env

    profile = {
        "providers": {
            "gateway": {
                "kind": "gateway",
                "openai": {
                    "base_url": "https://gateway.example/v1",
                    "api_key_ref": "env:BIFROST_TEST_KEY",
                    "wire_api": "responses",
                    "models": {"default": "model-a"},
                },
            }
        },
        "inference": {"harnesses": {"qwen": {"provider": "gateway"}}},
    }
    with inference_config_scope(profile), pytest.raises(ValueError, match="chat wire API"):
        _build_qwen_spawn_env(_spec("qwen"))


def test_jcode_binding_avoids_connect_and_isolates_session_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = Mock(side_effect=AssertionError("Bifrost must not activate the Databricks broker"))
    monkeypatch.setattr("omnigent.host.jcode_databricks.connect_jcode_gateway_env", broker)
    with inference_config_scope(_profile()):
        first = _build_acp_cli_spawn_env(_spec("jcode"), harness="jcode", session_id="first")
        second = _build_acp_cli_spawn_env(
            _spec("jcode", "databricks-literal/model-b"), harness="jcode", session_id="second"
        )
    assert first["JCODE_HOME"] != second["JCODE_HOME"]
    config = tomllib.loads((Path(first["JCODE_HOME"]) / "config.toml").read_text())
    assert config["providers"]["omnigent"]["base_url"] == "https://bifrost.example/v1"
    assert config["provider"]["default_model"] == "model-a"
    assert second["HARNESS_ACP_MODEL"] == "databricks-literal/model-b"
    assert "test-bifrost-token" not in (Path(first["JCODE_HOME"]) / "config.toml").read_text()
    broker.assert_not_called()


def test_saved_runner_config_keeps_old_route_after_host_profile_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _profile()
    saved = _write_runner_inference_config("old-session", original)
    edited = copy.deepcopy(original)
    edited["inference"] = {
        "harnesses": {
            "claude-native": {
                "provider": "bifrost",
                "default_model": "model-a",
                "model_allowlist": ["model-a"],
            }
        }
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(edited))
    current = resolve_native_claude_config(spec=_spec("claude-native"))
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(saved))
    resumed = resolve_native_claude_config(spec=_spec("claude-native"))
    assert current is not None and resumed is not None
    assert current.env["ANTHROPIC_BASE_URL"] == "https://bifrost.example/anthropic"
    assert resumed.env["ANTHROPIC_BASE_URL"] == "https://unity.example/anthropic"
    assert json.loads(saved.read_text()) == original
    assert saved.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "reference", ["env:CUSTOM_GATEWAY_KEY", "$CUSTOM_GATEWAY_KEY", "${CUSTOM_GATEWAY_KEY}"]
)
@pytest.mark.parametrize("host_config_unreadable", [False, True])
def test_saved_credential_references_survive_runner_launch(
    reference, host_config_unreadable, tmp_path, monkeypatch
) -> None:
    profile = _profile()
    for provider in profile["providers"].values():
        for family in ("anthropic", "openai"):
            provider[family]["api_key_ref"] = reference
    validate_inference_credentials(profile)
    with monkeypatch.context() as host:
        host.setattr(
            "omnigent.onboarding.provider_config.load_config",
            Mock(side_effect=OSError("unreadable"))
            if host_config_unreadable
            else Mock(return_value={}),
        )
        env = _build_runner_env(
            {
                "CUSTOM_GATEWAY_KEY": "saved-test-token",
                "UNRELATED_SECRET": "private",
                "HOME": str(tmp_path),
                "OMNIGENT_CONFIG_HOME": str(tmp_path),
            },
            server_url="https://omnigent.example",
            runner_id="runner",
            binding_token="binding",
            workspace="/tmp",
            parent_pid=1,
            inference_config=profile,
        )
    assert env["CUSTOM_GATEWAY_KEY"] == "saved-test-token"
    assert "UNRELATED_SECRET" not in env
    with patch.dict(os.environ, env, clear=True), inference_config_scope(profile):
        claude = resolve_native_claude_config(spec=_spec("claude-native"))
        codex = resolve_native_codex_launch(model=None, spec=_spec("codex-native"))
        opencode = resolve_bound_opencode_gateway(model=None)
    assert claude is not None and "saved-test-token" in claude.api_key_helper
    assert "saved-test-token" in " ".join(codex.config_overrides)
    assert opencode is not None and opencode.api_key == "saved-test-token"


def test_ordinary_host_keeps_legacy_credential_forwarding(monkeypatch) -> None:
    config = _profile()
    config.pop("inference")
    config["providers"]["bifrost"]["openai"]["api_key_ref"] = "$CUSTOM_GATEWAY_KEY"
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    env = _build_runner_env(
        {"CUSTOM_GATEWAY_KEY": "private", "UNITY_TEST_KEY": "legacy-token"},
        server_url="https://omnigent.example",
        runner_id="runner",
        binding_token="binding",
        workspace="/tmp",
        parent_pid=1,
    )
    assert "CUSTOM_GATEWAY_KEY" not in env
    assert env["UNITY_TEST_KEY"] == "legacy-token"


def test_host_ucode_configures_only_connected_harness_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.host.connect import _generate_ucode_configs

    profile = _profile()
    providers = profile["providers"]
    assert isinstance(providers, dict)
    providers["unity"] = {"kind": "databricks", "connection": "databricks"}
    inference = profile["inference"]
    assert isinstance(inference, dict)
    harnesses = inference["harnesses"]
    assert isinstance(harnesses, dict)
    harnesses["pi"] = {"provider": "bifrost"}
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._read_databrickscfg_host",
        lambda profile: "https://unity.example",
    )
    monkeypatch.setattr(
        "omnigent.host.databricks_credential.broker_token_command", lambda host: "broker-token"
    )
    configure = Mock()
    monkeypatch.setattr("omnigent.onboarding.ucode_setup.configure_ucode_for_sandbox", configure)
    with inference_config_scope(profile):
        _generate_ucode_configs()
    assert configure.call_args.kwargs["agents"] == ("claude",)

    harnesses["claude-native"] = {"provider": "bifrost"}
    configure.reset_mock()
    with inference_config_scope(profile):
        _generate_ucode_configs()
    configure.assert_not_called()


def test_worker_catalog_intersects_live_inventory_without_rejecting_private_aliases() -> None:
    from omnigent.models import model_catalog

    model_catalog._listing_cache.clear()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"id": "unlisted"},
                    {"id": "databricks-literal/model-b"},
                    {"id": "model-a"},
                ]
            },
        )
    )
    with inference_config_scope(_profile()):
        listing = list_models_for_worker(_spec("claude-sdk"), "claude-sdk", transport=transport)
    assert listing.verified
    assert [row.id for row in listing.models] == ["model-a", "databricks-literal/model-b"]


def test_child_dispatch_keeps_bound_provider_model_ids_literal() -> None:
    from omnigent.runner.tool_dispatch import _dispatch_model_mismatch, _normalize_subagent_model

    with inference_config_scope(_profile()):
        assert _dispatch_model_mismatch("codex-native", "model-a") is None
        assert _dispatch_model_mismatch("codex-native", "unlisted") is not None
        assert (
            _normalize_subagent_model(
                "databricks-literal/model-b",
                sub_agent_name="child",
                agent_spec=None,
                harness="codex-native",
            )
            == "databricks-literal/model-b"
        )


def test_pi_mixed_shortlist_registers_each_models_wire() -> None:
    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "pi-native": {
                "provider": "bifrost",
                "default_model": "claude-primary",
                "model_allowlist": ["claude-primary", "gpt-primary"],
            }
        }
    }
    with inference_config_scope(profile):
        provider = resolve_pi_native_provider()
    assert provider is not None
    rendered = provider.to_models_config()["providers"]
    assert rendered["omnigent"]["api"] == "anthropic-messages"
    assert [row["id"] for row in rendered["omnigent"]["models"]] == ["claude-primary"]
    assert rendered["omnigent-openai"]["api"] == "openai-responses"
    assert [row["id"] for row in rendered["omnigent-openai"]["models"]] == ["gpt-primary"]


def test_acp_override_uses_selected_agents_inference_binding(tmp_path: Path) -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "acp": {
                    "agents": [
                        {"name": "Other", "command": "other acp"},
                        {"name": "Custom", "command": "custom acp"},
                    ]
                }
            }
        )
    )
    spec = _spec("acp:other")
    with inference_config_scope(_profile()):
        env = _build_spawn_env_from_spec(spec, "acp:custom")
        assert env is not None
        assert env["HARNESS_ACP_COMMAND"] == "custom acp"
        assert env["HARNESS_ACP_MODEL"] == "model-a"
        assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [
            "model-a",
            "databricks-literal/model-b",
        ]
        with pytest.raises(OmnigentError, match="configured model list"):
            _build_spawn_env_from_spec(spec, "acp:custom", model_override="unlisted")
    assert spec.executor.config["harness"] == "acp:other"
