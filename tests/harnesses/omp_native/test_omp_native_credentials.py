"""Canonical OMP provider configuration and native own-login model discovery."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnigent.harnesses.omp_native import credentials as creds
from omnigent.harnesses.pi_native import credentials as pi_creds


def _provider(**kwargs: object) -> creds.PiProviderConfig:
    return replace(
        creds.PiProviderConfig(
            provider_id="omnigent",
            base_url="https://gateway.example/v1",
            api="openai-completions",
            model="custom-model",
            api_key="test-key",
            auth_header=True,
        ),
        **kwargs,
    )


def test_managed_launch_writes_canonical_yaml_and_preserves_omp_resources(tmp_path: Path) -> None:
    root, managed = tmp_path / "own-omp", tmp_path / "session-omp"
    root.mkdir()
    settings = {
        "defaultThinkingLevel": "low",
        "enabledModels": ["custom/*"],
        "extensions": ["./extensions/plugin.mjs"],
        "extensionHandlers": {"sessionStartTimeoutMs": 1234},
        "theme": "dark",
    }
    (root / "config.yml").write_text(yaml.safe_dump(settings))
    (root / "extensions").mkdir()
    (root / "extensions" / "plugin.mjs").write_text("export default () => {}")
    (root / "mcp.json").write_text('{"mcpServers": {}}')
    (root / ".mcp.json").write_text('{"mcpServers": {"fallback": {"command": "server"}}}')
    (root / "managed-skills").mkdir()
    (root / "managed-skills" / "SKILL.md").write_text("Managed OMP skill")
    (root / "agent.db").write_bytes(b"own SQLite login must remain private")

    launch = creds.omp_native_provider_launch(managed, _provider(), "high", global_agent_dir=root)

    assert launch.args == [
        "--provider",
        "omnigent",
        "--model",
        "custom-model",
        "--thinking",
        "high",
    ]
    assert launch.env == {
        "PI_CODING_AGENT_DIR": str(managed),
        "OMP_PROFILE": "",
        "PI_PROFILE": "",
    }
    configured = yaml.safe_load((managed / "models.yml").read_text())["providers"]["omnigent"]
    assert configured["baseUrl"] == "https://gateway.example/v1"
    assert configured["apiKey"] == "test-key"
    assert configured["authHeader"] is True
    assert configured["models"] == [{"id": "custom-model"}]
    copied = yaml.safe_load((managed / "config.yml").read_text())
    assert "defaultThinkingLevel" not in copied
    assert copied["enabledModels"] == ["custom/*"]
    assert copied["extensions"] == [str(root / "extensions/plugin.mjs")]
    assert copied["extensionHandlers"] == {
        "sessionStartTimeoutMs": 1234,
        "toolCallTimeoutMs": 86_410_000,
    }
    assert (managed / "extensions").samefile(root / "extensions")
    assert (managed / "mcp.json").samefile(root / "mcp.json")
    assert (managed / ".mcp.json").samefile(root / ".mcp.json")
    assert (managed / "managed-skills").samefile(root / "managed-skills")
    assert not (managed / "agent.db").exists()
    assert yaml.safe_load((root / "config.yml").read_text()) == settings
    assert not (managed / "models.json").exists()
    assert not (managed / "settings.json").exists()
    assert stat.S_IMODE(managed.stat().st_mode) == 0o700
    assert stat.S_IMODE((managed / "models.yml").stat().st_mode) == 0o600


def test_relaunch_refreshes_live_yaml_not_legacy_json(tmp_path: Path) -> None:
    (tmp_path / "models.json").write_text('{"providers": {"retired": {}}}')
    path = creds.write_omp_models_config(tmp_path, _provider(api_key="first"))
    assert path.name == "models.yml"
    creds.write_omp_models_config(tmp_path, _provider(api_key="refreshed", model="new-model"))
    payload = yaml.safe_load(path.read_text())["providers"]
    assert list(payload) == ["omnigent"]
    assert payload["omnigent"]["apiKey"] == "refreshed"
    assert payload["omnigent"]["models"] == [{"id": "new-model"}]
    assert not (tmp_path / "models.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="bounded command wrapper is POSIX-specific")
def test_yaml_command_auth_uses_supported_shared_refresh_transport(tmp_path: Path) -> None:
    provider = _provider(
        api_key="!printf renewed",
        auth_refresh_interval_ms=120_000,
        auth_timeout_ms=23_000,
    )
    launch = creds.omp_native_provider_launch(
        tmp_path / "managed", provider, global_agent_dir=tmp_path / "own"
    )
    managed = tmp_path / "managed"
    config = yaml.safe_load((managed / "models.yml").read_text())
    command = config["providers"]["omnigent"]["apiKey"]
    argv = shlex.split(command[1:])
    assert command.startswith("!")
    assert argv[-2] == str(managed / "omnigent-auth" / "command.cjs")
    refresh = json.loads(Path(argv[-1]).read_text())
    assert refresh["command"] == "printf renewed"
    assert refresh["refreshIntervalMs"] == 120_000
    assert refresh["refreshTimeoutMs"] == 23_000
    assert refresh["timeoutMs"] == 8_000
    assert launch.env[creds.AUTH_DIR_ENV] == str(managed / "omnigent-auth")
    assert "OMNIGENT_PI_NATIVE_AUTH_DIR" not in launch.env
    assert not (managed / "models.json").exists()
    retired = managed / "omnigent-auth" / "auth-retired.json"
    retired.write_text('{"command":"must not run"}')
    creds.write_omp_models_config(managed, _provider())
    assert not retired.exists()
    assert (
        yaml.safe_load((managed / "models.yml").read_text())["providers"]["omnigent"]["apiKey"]
        == "test-key"
    )


def test_canonical_yaml_omits_pi_only_thinking_fields_without_mutation(tmp_path: Path) -> None:
    provider = _provider(
        api="anthropic-messages",
        model="claude-opus-4-6",
        extra_models=[
            {
                "id": "claude-opus-4-6",
                "reasoning": True,
                "input": ["text", "image"],
                "thinkingLevelMap": {"off": None},
                "compat": {"forceAdaptiveThinking": True, "supportsStrictMode": False},
                "contextWindow": 200_000,
            }
        ],
    )
    rendered = provider.to_models_config()
    before = json.dumps(rendered, sort_keys=True)
    path = creds.write_omp_models_config(tmp_path, provider, rendered)
    configured = yaml.safe_load(path.read_text())["providers"]["omnigent"]
    assert "compat" not in configured
    model = configured["models"][0]
    assert model["compat"] == {"supportsStrictMode": False}
    assert "thinkingLevelMap" not in model
    assert model["reasoning"] is True
    assert model["input"] == ["text", "image"]
    assert model["contextWindow"] == 200_000
    assert json.dumps(rendered, sort_keys=True) == before


def test_selection_routes_slash_model_to_actual_secondary_provider(tmp_path: Path) -> None:
    provider = _provider(
        curated_models=True,
        additional_providers={
            "omnigent-openai": {
                "baseUrl": "https://gateway.example/responses",
                "api": "openai-responses",
                "apiKey": "secondary-key",
                "models": [{"id": "vendor/gpt-model"}],
            }
        },
    )
    launch = creds.omp_native_provider_launch(
        tmp_path / "managed",
        provider,
        selection="omnigent/vendor/gpt-model",
        global_agent_dir=tmp_path / "own",
    )
    assert launch.args == [
        "--provider",
        "omnigent-openai",
        "--model",
        "omnigent-openai/vendor/gpt-model",
    ]
    settings = yaml.safe_load((tmp_path / "managed" / "config.yml").read_text())
    assert settings["enabledModels"] == [
        "omnigent/custom-model",
        "omnigent-openai/vendor/gpt-model",
    ]


def test_inference_binding_and_effort_notice_use_omp_identity(tmp_path: Path) -> None:
    launch = creds.omp_native_provider_launch(
        tmp_path / "managed",
        _provider(inference_bound=True),
        "impossible-effort",
        selection="omnigent/stale-pick",
        global_agent_dir=tmp_path / "own",
    )
    assert launch.env["OMNIGENT_OMP_INFERENCE_BOUND"] == "1"
    assert "OMNIGENT_PI_INFERENCE_BOUND" not in launch.env
    assert launch.args == ["--provider", "omnigent", "--model", "custom-model"]
    assert launch.effort_warning is not None
    assert "not supported by omp" in launch.effort_warning


def test_rejects_own_root_before_overwriting_any_credential(tmp_path: Path) -> None:
    (tmp_path / "models.yml").write_text("providers: {own: {apiKey: preserve}}")
    with pytest.raises(ValueError, match="own login directory"):
        creds.omp_native_provider_launch(tmp_path, _provider(), global_agent_dir=tmp_path)
    assert (tmp_path / "models.yml").read_text() == "providers: {own: {apiKey: preserve}}"


@pytest.mark.parametrize(
    ("env", "suffix"),
    [
        ({}, ".omp/agent"),
        ({"PI_CONFIG_DIR": ".custom-omp"}, ".custom-omp/agent"),
        ({"OMP_PROFILE": "work", "PI_CODING_AGENT_DIR": "/ignored"}, ".omp/profiles/work/agent"),
        ({"PI_PROFILE": "legacy"}, ".omp/profiles/legacy/agent"),
        ({"OMP_PROFILE": "", "PI_PROFILE": "legacy"}, ".omp/agent"),
    ],
)
def test_own_root_follows_omp_profiles_not_pi_default(env: dict[str, str], suffix: str) -> None:
    assert creds.global_omp_agent_dir(env=env) == Path.home() / suffix
    assert creds.global_omp_agent_dir(
        env={"PI_CODING_AGENT_DIR": "relative"}, workspace=Path("/work")
    ) == Path("/work/relative")


def _catalog_command(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
    *,
    scoped_indices: list[int] | None = None,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    executable = "/configured/omp"
    monkeypatch.setenv("OMNIGENT_OMP_PATH", executable)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append({"argv": argv, **kwargs})
        if "--extension" in argv:
            stdout = (
                f"OMNIGENT_OMP_NATIVE_RESOLVER:{json.dumps(scoped_indices)}\n"
                '{"models":[]}\n'
            )
        else:
            stdout = json.dumps({"models": rows})
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(creds.subprocess, "run", run)
    monkeypatch.setattr(
        pi_creds, "_pi_own_login_models", lambda *_: pytest.fail("OMP read Pi inventory")
    )
    return calls


def test_own_catalog_uses_supported_omp_command_and_sqlite_owned_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _catalog_command(
        monkeypatch,
        [
            {"provider": "openai-codex", "id": "gpt-5.6-sol", "name": "GPT Sol"},
            {"provider": "custom", "id": "vendor/model", "name": "Custom"},
            {"provider": "omnigent", "id": "retired-managed-model"},
            {"provider": "bad", "id": None},
        ],
        scoped_indices=[1],
    )
    (tmp_path / "auth.json").write_text("Pi legacy auth must not be read")
    (tmp_path / "config.yml").write_text("enabledModels: [custom/**]\n")
    options = creds.omp_own_login_model_options(tmp_path)
    assert options == [
        {"id": "custom/vendor/model", "model": "custom/vendor/model", "displayName": "Custom"}
    ]
    assert calls[0]["argv"] == ["/configured/omp", "models", "--json"]
    assert calls[1]["argv"][:5] == [
        "/configured/omp", "models", "--json", "--no-extensions", "--extension"
    ]
    scope_input = json.loads(calls[1]["input"])
    assert scope_input["patterns"] == ["custom/**"]
    assert scope_input["models"] == [
        {"provider": "openai-codex", "id": "gpt-5.6-sol", "name": "GPT Sol"},
        {"provider": "custom", "id": "vendor/model", "name": "Custom"},
    ]
    assert calls[0]["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path)
    assert calls[0]["env"]["OMP_PROFILE"] == ""
    assert creds.omp_own_login_serves_reference("openai-codex/gpt-5.6-sol", tmp_path)


def test_own_catalog_keeps_native_profile_without_forced_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _catalog_command(monkeypatch, [])
    monkeypatch.setenv("OMP_PROFILE", "work")
    creds._omp_own_login_models()
    assert calls[0]["env"]["OMP_PROFILE"] == "work"


def test_own_catalog_command_failure_never_falls_back_to_pi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog_command(monkeypatch, [])
    monkeypatch.setattr(
        creds.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=1, stdout="")
    )
    assert creds.omp_own_login_model_options() == []


def test_omp_own_subscription_preserves_login_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _catalog_command(monkeypatch, [])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text("theme: own-theme\n")
    (tmp_path / "agent.db").write_bytes(b"own login")
    config = {"providers": {"omp-login": {"kind": "subscription", "cli": "omp", "default": "omp"}}}
    assert creds.resolve_omp_native_provider(config_loader=lambda: config) is None
    assert (tmp_path / "config.yml").read_text() == "theme: own-theme\n"
    assert (tmp_path / "agent.db").read_bytes() == b"own login"
    assert not (tmp_path / "models.yml").exists()


def test_omp_reference_routing_never_reads_pi_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _catalog_command(monkeypatch, [{"provider": "openai-codex", "id": "gpt-5.6-sol"}])
    monkeypatch.setattr(
        pi_creds, "pi_own_login_serves_reference", lambda *_: pytest.fail("OMP consulted Pi login")
    )
    config = {
        "providers": {
            "gateway": {
                "kind": "gateway",
                "default": "omp",
                "openai": {
                    "base_url": "https://gateway.example/v1",
                    "api_key": "test-key",
                    "wire_api": "chat",
                },
            }
        }
    }
    assert (
        creds.resolve_omp_native_provider(
            model="openai-codex/gpt-5.6-sol", config_loader=lambda: config
        )
        is None
    )


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-sol"),
        ("omnigent/gpt-model", "gpt-model"),
        ("omnigent/vendor/model", None),
    ],
)
def test_own_model_arg_uses_native_selector_grammar(selection: str, expected: str | None) -> None:
    assert creds.omp_own_login_model_arg(selection) == expected


def test_managed_omp_provider_resolves_independently_of_pi_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pi_creds,
        "pi_own_login_serves_reference",
        lambda *_: pytest.fail("OMP consulted Pi login"),
    )
    config = {
        "providers": {
            "gateway": {
                "kind": "gateway",
                "default": "omp",
                "openai": {
                    "base_url": "https://gateway.example/v1",
                    "api_key": "test-key",
                    "wire_api": "chat",
                },
            }
        }
    }
    provider = creds.resolve_omp_native_provider(
        model="custom-model", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api_key == "test-key"
    assert provider.model == "custom-model"
    assert provider.api == "openai-completions"


def test_mlflow_provider_loads_omp_gateway_adapter(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    launch = creds.omp_native_provider_launch(
        managed,
        _provider(base_url="https://workspace.example/ai-gateway/mlflow/v1"),
        global_agent_dir=tmp_path / "own",
    )
    assert launch.args[-2:] == [
        "--extension",
        str(managed / "omnigent_omp_gateway.mjs"),
    ]
    adapter = (managed / "omnigent_omp_gateway.mjs").read_text()
    assert "@oh-my-pi/pi-ai/providers/openai-completions" in adapter
    assert (managed / "omnigent_gateway.js").exists()
    assert json.loads((managed / "omnigent_gateway_providers.json").read_text()) == ["omnigent"]


def test_databricks_provider_uses_omp_gateway_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent import databricks_ai_gateway

    monkeypatch.setattr(databricks_ai_gateway, "is_ug_route", lambda _: False)
    path = creds.write_omp_models_config(
        tmp_path,
        _provider(
            base_url="https://workspace.cloud.databricks.com/ai-gateway/anthropic",
            api="anthropic-messages",
            model="claude-sonnet-4-6",
        ),
    )
    headers = yaml.safe_load(path.read_text())["providers"]["omnigent"]["headers"]
    assert headers["Databricks-Custom-Coding-Agent"] == "omp"


@pytest.mark.parametrize("indices", [[99], [-1], [True]])
def test_omp_curation_rejects_invalid_native_results_without_unscoped_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, indices: list[int]
) -> None:
    _catalog_command(
        monkeypatch,
        [{"provider": "custom", "id": "model"}],
        scoped_indices=indices,
    )
    (tmp_path / "config.yml").write_text("enabledModels: [custom/*]\n")
    assert creds.omp_own_login_model_options(tmp_path) == []


@pytest.mark.parametrize(
    ("patterns", "expected"),
    [
        (["custom/*"], []),
        (["custom/**"], ["custom/vendor/model"]),
        (["gpt-*"], ["openai/gpt-4o", "openai/gpt-4.1"]),
        (["openai/gpt-4.1:high"], ["openai/gpt-4.1"]),
        (["vendor"], ["custom/vendor/model"]),
        (["@smol"], ["openai/gpt-4o"]),
        (["absent/*"], []),
        (["custom/*", "vendor"], ["custom/vendor/model"]),
    ],
)
def test_real_omp_scope_uses_native_semantics_without_pi_or_node(
    monkeypatch: pytest.MonkeyPatch, patterns: list[str], expected: list[str]
) -> None:
    executable = os.environ.get("OMNIGENT_OMP_PATH") or shutil.which("omp")
    if not executable:
        pytest.skip("OMP is required")
    monkeypatch.setenv("OMNIGENT_OMP_PATH", executable)
    # The host locates its runtime and modules; Python never asks for Pi, Node or Bun.
    monkeypatch.setattr(creds.shutil, "which", lambda *_, **__: None)
    models = [
        creds.ScopableModel("custom", "vendor/model", "Custom"),
        creds.ScopableModel("openai", "gpt-4o", "GPT 4o"),
        creds.ScopableModel("openai", "gpt-4.1", "GPT 4.1"),
    ]
    scoped = creds._scope_omp_models(patterns, models, {"modelRoles": {"smol": "openai/gpt-4o"}})
    assert [model.reference for model in scoped] == expected


@pytest.mark.skipif(os.name != "posix", reason="Exercises a POSIX launcher wrapper")
def test_omp_wrapper_resolves_host_package_and_curates_without_binary_layout_assumptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = os.environ.get("OMNIGENT_OMP_PATH") or shutil.which("omp")
    if not executable:
        pytest.skip("OMP is required")
    wrapper = tmp_path / "unrelated-bin" / "omp-wrapper"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(executable)} "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv("OMNIGENT_OMP_PATH", str(wrapper))
    monkeypatch.setattr(creds.shutil, "which", lambda *_, **__: None)

    root = creds.resolve_omp_package_root()

    assert root is not None
    assert json.loads((root / "package.json").read_text())["name"] == (
        "@oh-my-pi/pi-coding-agent"
    )
    assert not wrapper.is_relative_to(root)
    models = [
        creds.ScopableModel("custom", "vendor/model", "Custom"),
        creds.ScopableModel("openai", "gpt-4o", "GPT 4o"),
    ]
    assert creds._scope_omp_models(["custom/**"], models, {}) == [models[0]]
