"""Host setup operates on isolated config and never contacts a paid provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.config import load_global_config, save_global_config
from omnigent.onboarding import ambient, secrets
from omnigent.onboarding import detected as _detected  # noqa: F401
from omnigent.onboarding import setup_service as service
from omnigent.onboarding.setup_schema import SETUP_ACTION_ADAPTER, SetupDetectRequest


@pytest.fixture(autouse=True)
def isolated_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setattr(ambient, "detect_providers", lambda **_: [])
    from omnigent.onboarding import openclaw_config, providers

    monkeypatch.setattr(
        openclaw_config, "discover_openclaw_agents", lambda: openclaw_config.OpenClawDiscovery(())
    )
    monkeypatch.setattr(providers, "get_chat_models", lambda _: [])
    monkeypatch.setattr(providers, "default_chat_model", lambda _: None)


def apply(**values: object):
    return service.apply_setup_action(SETUP_ACTION_ADAPTER.validate_python(values))


def gateway(name: str = "gateway", **overrides: object):
    return apply(
        **{
            "action": "add_gateway",
            "name": name,
            "base_url": "https://gateway.example/v1",
            "secret": "TEST-SECRET",
            "families": ["openai"],
            "models": {"openai": "test-model"},
            "wire_api": "chat",
            **overrides,
        }
    )


def test_named_keys_keep_distinct_sources_and_vendor_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FIRST_FIXTURE_KEY", "first")
    monkeypatch.setenv("SECOND_FIXTURE_KEY", "second")
    apply(
        action="add_key", provider="openrouter", env_var="FIRST_FIXTURE_KEY", model="vendor/model"
    )
    apply(
        action="add_key",
        provider="openrouter",
        env_var="SECOND_FIXTURE_KEY",
        model="vendor/model2",
    )
    apply(action="add_key", provider="openrouter", env_var="FIRST_FIXTURE_KEY", model="updated")
    entries = load_global_config()["providers"]
    assert set(entries) == {"openrouter", "openrouter-2"}
    assert entries["openrouter"]["openai"]["base_url"] == "https://openrouter.ai/api/v1"
    assert entries["openrouter"]["openai"]["wire_api"] == "chat"
    assert entries["openrouter"]["openai"]["models"]["default"] == "updated"


@pytest.mark.parametrize("model_input", [{}, {"model": None}])
def test_key_without_model_uses_cli_catalog_default(
    monkeypatch: pytest.MonkeyPatch, model_input: dict[str, object]
):
    from omnigent.onboarding import providers

    requested = []

    def default(provider: str) -> str:
        requested.append(provider)
        return "fixture-catalog-model"

    monkeypatch.setattr(providers, "default_chat_model", default)
    apply(action="add_key", provider="openai", secret="fixture-key", **model_input)
    assert requested == ["openai"]
    assert load_global_config()["providers"]["openai"]["openai"]["models"]["default"] == (
        "fixture-catalog-model"
    )


def test_explicit_key_model_does_not_load_catalog(monkeypatch: pytest.MonkeyPatch):
    from omnigent.onboarding import providers

    monkeypatch.setattr(
        providers, "default_chat_model", lambda _: pytest.fail("explicit model fetched catalog")
    )
    apply(action="add_key", provider="openai", secret="fixture-key", model="my-fixture-model")
    assert load_global_config()["providers"]["openai"]["openai"]["models"]["default"] == (
        "my-fixture-model"
    )


def test_missing_catalog_default_leaves_existing_config_and_secret_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    apply(action="add_key", provider="openai", secret="fixture-old", model="fixture-old-model")
    before = (tmp_path / "config.yaml").read_bytes()
    monkeypatch.setattr(
        secrets, "store_secret", lambda *_: pytest.fail("secret written without a model")
    )
    with pytest.raises(ValueError, match="No default model is available"):
        apply(action="add_key", provider="openai", secret="fixture-new")
    assert (tmp_path / "config.yaml").read_bytes() == before


def test_gateway_defaults_and_reconfiguration_preserve_advanced(tmp_path: Path):
    gateway()
    cfg = load_global_config()
    cfg["providers"]["gateway"]["openai"]["context_window"] = 200000
    cfg["providers"]["gateway"]["openai"]["models"]["fast"] = "fast-model"
    cfg["providers"]["gateway"]["future_setting"] = {"keep": True}
    save_global_config(cfg)
    gateway("second")
    apply(action="set_default", name="second", surface="openai")
    gateway(secret="NEW-SECRET")
    cfg = load_global_config()
    assert cfg["providers"]["gateway"]["future_setting"] == {"keep": True}
    assert cfg["providers"]["gateway"]["openai"]["context_window"] == 200000
    assert cfg["providers"]["gateway"]["openai"]["models"]["fast"] == "fast-model"
    inventory = service.get_setup_inventory()
    assert inventory.effective_defaults["openai"] == "second"
    assert "TEST-SECRET" not in inventory.model_dump_json()
    assert "NEW-SECRET" not in (tmp_path / "config.yaml").read_text()


@pytest.mark.parametrize(
    "raw", ["[broken", "- sequence", "providers: invalid", "providers:\n  bad: 4"]
)
def test_malformed_config_rejected_before_secret_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
):
    path = tmp_path / "config.yaml"
    path.write_text(raw)
    monkeypatch.setattr(
        secrets, "store_secret", lambda *_: pytest.fail("secret write before validation")
    )
    with pytest.raises(ValueError, match="existing configuration"):
        gateway()
    assert path.read_text() == raw


@pytest.mark.parametrize(
    "overrides",
    [
        {"models": {}},
        {"base_url": "https://user:password@gateway.example"},
        {"base_url": "https://gateway.example?token=private"},
        {"secret": " "},
    ],
)
def test_invalid_gateway_never_writes(
    overrides: dict[str, object], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        secrets, "store_secret", lambda *_: pytest.fail("secret write before validation")
    )
    with pytest.raises(ValueError):
        gateway(**overrides)
    assert not load_global_config()


def test_passive_inventory_never_detects_resolves_or_fetches(monkeypatch: pytest.MonkeyPatch):
    gateway()
    monkeypatch.setattr(ambient, "detect_providers", lambda: pytest.fail("passive detection"))
    monkeypatch.setattr(secrets, "load_secret", lambda *_: pytest.fail("secret resolved"))
    assert service.get_setup_inventory().providers[0].name == "gateway"


def test_explicit_harness_status_checks_only_requested_harness(
    monkeypatch: pytest.MonkeyPatch,
):
    from omnigent.onboarding import harness_readiness, providers

    checked: list[str] = []
    monkeypatch.setattr(
        harness_readiness,
        "_harness_availability",
        lambda harness: checked.append(harness) or "needs-auth",
    )
    monkeypatch.setattr(
        harness_readiness,
        "configured_harness_map",
        lambda: pytest.fail("full readiness map was probed"),
    )
    monkeypatch.setattr(ambient, "detect_providers", lambda **_: pytest.fail("providers detected"))
    monkeypatch.setattr(service, "_discover_imports", lambda *_: pytest.fail("imports discovered"))
    monkeypatch.setattr(
        providers, "get_chat_models", lambda *_: pytest.fail("model catalog loaded")
    )

    result = service.detect_setup_connections(
        SetupDetectRequest(harness="antigravity-native", import_source="acpx")
    )

    assert checked == ["antigravity-native"]
    assert result.harness_status is not None
    assert result.harness_status.harness == "antigravity-native"
    assert result.harness_status.availability == "needs-auth"
    assert result.providers == []
    assert result.imports == []
    assert result.models == {}


def test_explicit_harness_status_failure_has_no_success_verdict(
    monkeypatch: pytest.MonkeyPatch,
):
    from omnigent.onboarding import harness_readiness

    def failed_check(_harness: str) -> None:
        raise RuntimeError("fixture private error")

    monkeypatch.setattr(harness_readiness, "_harness_availability", failed_check)
    result = service.detect_setup_connections(SetupDetectRequest(harness="opencode"))

    assert result.harness_status is None
    assert result.warnings == [
        "The requested harness status could not be checked on this computer"
    ]
    assert "fixture private error" not in result.model_dump_json()


def test_explicit_harness_status_rejects_unknown_harness():
    with pytest.raises(ValueError):
        SetupDetectRequest.model_validate({"harness": "arbitrary-command"})


def test_builtin_acp_instructions_follow_cli_catalog_and_custom_shadowing():
    from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES

    inventory = service.get_setup_inventory()
    assert {row.id for row in inventory.builtin_acp} == set(ACP_CLI_HARNESSES)
    for row in inventory.builtin_acp:
        spec = ACP_CLI_HARNESSES[row.id].install
        assert row.label == spec.display
        assert row.install_command == spec.install_hint
        assert row.auth_instructions == spec.auth_hint
    assert not load_global_config()

    save_global_config({"acp": {"agents": [{"name": "Devin", "command": "fixture-devin --acp"}]}})
    inventory = service.get_setup_inventory()
    assert "devin" not in {row.id for row in inventory.builtin_acp}
    assert inventory.acp_agents[0].slug == "devin"
    assert load_global_config()["acp"]["agents"][0]["command"] == "fixture-devin --acp"


def test_harness_keys_preserve_advanced_host_and_removal():
    save_global_config({"copilot": {"github_host": "enterprise.example", "future": 4}})
    apply(action="set_harness_key", harness="copilot", secret="fixture-token")
    apply(action="set_copilot_host", host="new.example")
    apply(action="remove_harness_key", harness="copilot")
    assert load_global_config()["copilot"] == {"github_host": "new.example", "future": 4}


def test_gateway_reconfiguration_drops_deselected_families():
    gateway(families=["anthropic", "openai"], models={"anthropic": "fixture", "openai": "fixture"})
    gateway(families=["openai"])
    raw = load_global_config()["providers"]["gateway"]
    assert "anthropic" not in raw
    inventory = service.get_setup_inventory()
    assert set(inventory.providers[0].families) == {"openai", "pi"}
    assert inventory.effective_defaults.get("anthropic") is None


def test_standalone_credential_rotation_is_not_a_setup_action():
    with pytest.raises(ValueError):
        apply(
            action="update_provider_credential",
            name="gateway",
            families=["openai"],
            secret="fixture",
        )


def test_acp_edits_preserve_unknown_existing_fields():
    existing = {
        "name": "Old",
        "command": "old --acp",
        "future": 3,
        "env_passthrough": ["FIXTURE_TOKEN"],
        "session_id_mode": "client",
        "send_model": True,
        "omnigent_mcp": False,
        "inject_system_prompt": False,
    }
    save_global_config({"acp": {"future": True, "agents": [existing]}})
    apply(
        action="add_acp",
        name="New",
        command="new --acp",
        model="fixture-model",
    )
    assert load_global_config()["acp"]["agents"] == [
        existing,
        {"name": "New", "command": "new --acp", "model": "fixture-model"},
    ]
    apply(action="remove_acp", slug="new")
    assert load_global_config()["acp"] == {
        "future": True,
        "agents": [existing],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("env_passthrough", ["FIXTURE_TOKEN"]),
        ("session_id_mode", "client"),
        ("send_model", True),
        ("omnigent_mcp", False),
        ("inject_system_prompt", False),
    ],
)
def test_acp_creation_rejects_nonstandard_options(field: str, value: object):
    with pytest.raises(ValueError):
        apply(action="add_acp", name="New", command="new --acp", **{field: value})
    assert "acp" not in load_global_config()


def test_detect_adopt_remove_dismiss_and_re_adopt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setattr(
        ambient,
        "detect_providers",
        lambda **_: [ambient.DetectedProvider("openai", "key", "openai", "$OPENAI_API_KEY")],
    )
    assert service.detect_setup_connections().providers[0].name == "openai"
    apply(action="adopt_detected", name="openai")
    apply(action="remove_provider", name="openai")
    assert service.get_setup_inventory().dismissed_detections == ["openai"]
    apply(action="adopt_detected", name="openai")
    assert service.get_setup_inventory().dismissed_detections == []


def test_pi_subscription_and_bedrock_compatibility():
    apply(action="subscription", cli="pi")
    apply(action="add_bedrock", model="bedrock-model", secret="bedrock-secret")
    cfg = service.get_setup_inventory()
    assert cfg.effective_defaults["pi"] == "pi-subscription"
    assert cfg.effective_defaults["anthropic"] == "bedrock"
    assert "pi" not in next(p for p in cfg.providers if p.name == "bedrock").default_scopes


def test_vendor_subscription_requires_successful_login():
    with pytest.raises(ValueError, match="vendor login"):
        apply(action="subscription", cli="codex")
    assert load_global_config() == {}


def test_opencode_model_clear():
    apply(action="set_opencode_model", model="provider/model")
    assert service.get_setup_inventory().harness_settings.opencode_model == "provider/model"
    apply(action="set_opencode_model", model=None)
    assert "opencode_model" not in load_global_config()


def test_failed_reconfiguration_keeps_previous_secret_and_reference(
    monkeypatch: pytest.MonkeyPatch,
):
    gateway()
    before = load_global_config()
    old_ref = before["providers"]["gateway"]["openai"]["api_key_ref"]
    old_secret = secrets.load_secret(old_ref.removeprefix("keychain:"))
    monkeypatch.setattr(
        service.operations,
        "save_setup_settings",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("fixture failure")),
    )
    with pytest.raises(OSError, match="fixture failure"):
        gateway(secret="replacement")
    assert load_global_config() == before
    assert secrets.load_secret(old_ref.removeprefix("keychain:")) == old_secret


def test_reconfiguration_does_not_change_shared_sibling_credential():
    gateway()
    config = load_global_config()
    from copy import deepcopy

    sibling = deepcopy(config["providers"]["gateway"])
    sibling.pop("default", None)
    config["providers"]["sibling"] = sibling
    save_global_config(config)
    old_ref = sibling["openai"]["api_key_ref"]
    gateway(secret="replacement")
    assert load_global_config()["providers"]["sibling"]["openai"]["api_key_ref"] == old_ref
    assert secrets.load_secret(old_ref.removeprefix("keychain:")) == "TEST-SECRET"


def test_remove_acp_skips_malformed_existing_entries():
    malformed = {"name": 3, "command": "invalid-agent"}
    save_global_config(
        {"acp": {"agents": [malformed, {"name": "Keep", "command": "valid-agent"}]}}
    )
    apply(action="remove_acp", slug="keep")
    assert load_global_config()["acp"]["agents"] == [malformed]


def test_import_previews_hide_arguments_and_reject_changed_commands(tmp_path: Path):
    import json

    source = tmp_path / "acpx.json"
    source.write_text(
        json.dumps(
            {
                "agents": {
                    "Fixture": {
                        "command": "fixture",
                        "args": ["--header", "Authorization: Bearer SECRET"],
                    }
                }
            }
        )
    )
    preview = service.detect_setup_connections(
        service.SetupDetectRequest(import_path=str(source), import_source="acpx")
    )
    assert "SECRET" not in preview.model_dump_json()
    assert preview.imports[0].command == "fixture (2 arguments hidden)"
    fingerprints = {p.name: p.fingerprint for p in preview.imports}
    source.write_text(json.dumps({"agents": {"Fixture": {"command": "changed"}}}))
    with pytest.raises(ValueError, match="changed"):
        apply(
            action="import_acp",
            source="acpx",
            path=str(source),
            names=["Fixture"],
            fingerprints=fingerprints,
        )
    current = service.detect_setup_connections(
        service.SetupDetectRequest(import_path=str(source), import_source="acpx")
    )
    apply(
        action="import_acp",
        source="acpx",
        path=str(source),
        names=["Fixture"],
        fingerprints={p.name: p.fingerprint for p in current.imports},
    )
    assert load_global_config()["acp"]["agents"][0]["command"] == "changed"


def test_passive_explicit_pi_cli_config_does_not_probe(monkeypatch: pytest.MonkeyPatch):
    from omnigent.onboarding import provider_config

    save_global_config(
        {
            "providers": {
                "custom": {
                    "kind": "cli-config",
                    "cli": "codex",
                    "model_provider": "Fixture",
                    "default": "pi",
                }
            }
        }
    )
    monkeypatch.setattr(
        provider_config, "_cli_config_serves_pi", lambda *_: pytest.fail("vendor probe")
    )
    inventory = service.get_setup_inventory()
    assert inventory.effective_defaults["pi"] == "custom"
    assert not inventory.pi_default_requires_detection


@pytest.mark.parametrize("can_serve_pi", [False, True])
def test_implicit_pi_cli_config_requires_explicit_resolution(
    monkeypatch: pytest.MonkeyPatch, can_serve_pi: bool
):
    from omnigent.onboarding import provider_config, providers

    save_global_config(
        {
            "providers": {
                "codex-config": {
                    "kind": "cli-config",
                    "cli": "codex",
                    "model_provider": "Fixture",
                    "default": "openai",
                }
            }
        }
    )
    checks: list[str] = []

    def capable(entry):
        checks.append(entry.name)
        return can_serve_pi

    monkeypatch.setattr(provider_config, "_cli_config_serves_pi", capable)
    inventory = service.get_setup_inventory()
    assert inventory.effective_defaults["pi"] is None
    assert inventory.pi_default_requires_detection
    assert checks == []
    monkeypatch.setattr(ambient, "detect_providers", lambda **_: pytest.fail("vendor detected"))
    monkeypatch.setattr(service, "_discover_imports", lambda *_: pytest.fail("imports read"))
    monkeypatch.setattr(providers, "get_chat_models", lambda *_: pytest.fail("catalog fetched"))
    result = service.detect_setup_connections(SetupDetectRequest(pi_default=True))
    assert checks == ["codex-config"]
    assert result.pi_default_checked
    assert result.pi_default_provider == ("codex-config" if can_serve_pi else None)
    assert result.providers == []
    assert result.imports == []
    assert result.models == {}


def test_pi_resolution_failure_is_sanitized_and_unchecked(monkeypatch: pytest.MonkeyPatch):
    from omnigent.onboarding import provider_config

    def fail(_config, _harness):
        raise RuntimeError("fixture-sensitive-value")

    monkeypatch.setattr(provider_config, "default_provider_for_harness", fail)
    result = service.detect_setup_connections(SetupDetectRequest(pi_default=True))
    assert result.pi_default_provider is None
    assert not result.pi_default_checked
    assert result.warnings == ["The Pi default could not be checked on this computer"]
    assert "fixture-sensitive-value" not in result.model_dump_json()


def test_invalid_acp_config_rejected_before_credential_write(monkeypatch: pytest.MonkeyPatch):
    save_global_config(
        {"acp": {"agents": [{"name": "Fixture", "command": "fixture", "omnigent_mcp": "invalid"}]}}
    )
    monkeypatch.setattr(
        secrets, "store_secret", lambda *_: pytest.fail("secret write before parse")
    )
    with pytest.raises(ValueError, match="existing configuration"):
        gateway()


def test_browser_detection_never_uses_claude_keychain_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ambient, "_claude_login_detected", lambda: pytest.fail("Keychain probe"))
    monkeypatch.setattr(
        ambient, "_claude_credentials_path", lambda: tmp_path / "missing-claude.json"
    )
    monkeypatch.setattr(ambient, "_codex_auth_path", lambda: tmp_path / "missing-codex.json")
    monkeypatch.setattr(ambient, "claude_managed_gateway", lambda: (None, False))
    monkeypatch.setattr(ambient, "codex_config_detection", lambda: None)
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: False)
    ambient._detect_providers_now(allow_keychain=False)


@pytest.mark.parametrize(
    "old_ref, expected",
    [("keychain:openai-other", "openai-2"), ("keychain:openai-" + "a" * 32, "openai")],
)
def test_readding_named_key_recognizes_only_own_rotated_slots(old_ref: str, expected: str):
    save_global_config(
        {
            "providers": {
                "openai": {
                    "kind": "key",
                    "openai": {
                        "base_url": "https://api.openai.com/v1",
                        "api_key_ref": old_ref,
                        "models": {"default": "original"},
                    },
                }
            }
        }
    )
    result = apply(
        action="add_key", provider="openai", secret="fixture-replacement", model="updated"
    )
    assert result.message == f"Added {expected}"
    assert load_global_config()["providers"][expected]["openai"]["models"]["default"] == "updated"


def test_inline_harness_credentials_are_listed_without_resolving():
    save_global_config(
        {
            "cursor": {"api_key": "INLINE-SECRET"},
            "antigravity": {"api_key": "$FIXTURE_GEMINI"},
            "copilot": {"github_token": "INLINE-TOKEN"},
        }
    )
    inventory = service.get_setup_inventory()
    assert inventory.harness_settings.cursor_key_configured
    assert inventory.harness_settings.antigravity_key_configured
    assert inventory.harness_settings.copilot_key_configured
    assert "INLINE-SECRET" not in inventory.model_dump_json()
    assert "INLINE-TOKEN" not in inventory.model_dump_json()


def test_existing_unicode_provider_identifier_can_be_managed():
    name = "研究/代理:work"
    save_global_config(
        {
            "providers": {
                name: {
                    "kind": "key",
                    "openai": {
                        "base_url": "https://api.openai.com/v1",
                        "api_key_ref": "env:FIXTURE_UNUSED",
                    },
                }
            }
        }
    )
    apply(action="set_default", name=name, surface="openai")
    assert service.get_setup_inventory().effective_defaults["openai"] == name
    apply(action="remove_provider", name=name)
    assert service.get_setup_inventory().providers == []


def test_readoption_keeps_configured_entry_authoritative(monkeypatch: pytest.MonkeyPatch):
    apply(action="add_key", provider="openai", secret="fixture-key", model="custom-model")
    before = load_global_config()
    before["providers"]["openai"]["openai"]["context_window"] = 200000
    before["providers"]["openai"]["future_setting"] = {"keep": True}
    save_global_config(before)
    monkeypatch.setenv("OPENAI_API_KEY", "different-fixture-key")
    monkeypatch.setattr(
        ambient,
        "detect_providers",
        lambda **_: [ambient.DetectedProvider("openai", "key", "openai", "$OPENAI_API_KEY")],
    )
    apply(action="adopt_detected", name="openai")
    assert load_global_config()["providers"] == before["providers"]


def test_adoption_deduplicates_subscription_identity(monkeypatch: pytest.MonkeyPatch):
    save_global_config(
        {"providers": {"my-codex": {"kind": "subscription", "cli": "codex", "default": "openai"}}}
    )
    before = load_global_config()["providers"]
    monkeypatch.setattr(
        ambient,
        "detect_providers",
        lambda **_: [ambient.DetectedProvider("codex", "subscription", "openai", "fixture")],
    )
    apply(action="adopt_detected", name="codex")
    assert load_global_config()["providers"] == before


def test_cli_replaces_ui_staged_key_in_place(monkeypatch: pytest.MonkeyPatch):
    from omnigent import cli_config
    from omnigent.onboarding import interactive
    from omnigent.onboarding.provider_config import get_default_provider

    apply(action="add_key", provider="openai", secret="ui-fixture", model="ui-model")
    monkeypatch.setattr(interactive, "select", lambda *_a, **_kw: 0)
    answers = iter(["cli-fixture", "cli-model"])
    monkeypatch.setattr(interactive, "prompt_text", lambda *_a, **_kw: next(answers))
    cli_config._configure_harness_add("openai")
    config = load_global_config()
    assert set(config["providers"]) == {"openai"}
    entry = config["providers"]["openai"]
    assert entry["openai"]["api_key_ref"] == "keychain:openai"
    assert entry["openai"]["models"]["default"] == "cli-model"
    assert secrets.load_secret("openai") == "cli-fixture"
    assert get_default_provider(config, "openai").name == "openai"


@pytest.mark.parametrize("harness", [None, "cursor", "antigravity", "copilot"])
def test_failed_save_cleans_fresh_secret(harness: str | None, monkeypatch: pytest.MonkeyPatch):
    def save(secret: str):
        if harness:
            return apply(action="set_harness_key", harness=harness, secret=secret)
        return gateway(secret=secret)

    save("original-fixture")
    before = load_global_config()
    original = secrets._read_secrets_file()
    monkeypatch.setattr(
        service.operations,
        "save_setup_settings",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("fixture persistence failure")),
    )
    with pytest.raises(OSError, match="fixture persistence failure"):
        save("replacement-fixture")
    assert load_global_config() == before
    assert secrets._read_secrets_file() == original


@pytest.mark.parametrize("harness", [None, "cursor"])
def test_failed_save_and_cleanup_reports_sanitized_persistence_error(
    harness: str | None, monkeypatch: pytest.MonkeyPatch
):
    before = load_global_config()

    def fail_save(*_args, **_kwargs):
        raise OSError("fixture-private-save-secret")

    def fail_cleanup(*_args):
        raise OSError("fixture-private-cleanup-secret")

    monkeypatch.setattr(service.operations, "save_setup_settings", fail_save)
    monkeypatch.setattr(service.operations, "cleanup_unreferenced_secret", fail_cleanup)
    with pytest.raises(service.SetupPersistenceError) as failure:
        if harness:
            apply(action="set_harness_key", harness=harness, secret="fixture-secret")
        else:
            gateway(secret="fixture-secret")
    assert str(failure.value) == "Setup was not saved; stored secret cleanup did not complete"
    assert "fixture-private" not in str(failure.value)
    assert "fixture-secret" not in str(failure.value)
    assert load_global_config() == before


@pytest.mark.parametrize("harness", [None, "cursor", "antigravity", "copilot"])
def test_replacement_cleans_owned_superseded_secret(harness: str | None):
    def save(secret: str):
        if harness:
            return apply(action="set_harness_key", harness=harness, secret=secret)
        return gateway(secret=secret)

    save("original-fixture")
    old_slots = set(secrets._read_secrets_file())
    save("replacement-fixture")
    remaining = secrets._read_secrets_file()
    assert not old_slots.intersection(remaining)
    assert list(remaining.values()) == ["replacement-fixture"]


@pytest.mark.parametrize("shared", [False, True])
def test_harness_replacement_preserves_foreign_or_shared_secret(shared: bool):
    ref = "keychain:cursor" if shared else "keychain:unrelated-slot"
    secrets.store_secret(ref.removeprefix("keychain:"), "original-fixture")
    config: dict[str, object] = {"cursor": {"api_key_ref": ref}}
    if shared:
        config["unrelated"] = {"nested": [ref]}
    save_global_config(config)
    apply(action="set_harness_key", harness="cursor", secret="replacement-fixture")
    assert secrets.load_secret(ref.removeprefix("keychain:")) == "original-fixture"


def test_cleanup_failure_reports_successful_save(monkeypatch: pytest.MonkeyPatch):
    gateway()
    monkeypatch.setattr(
        secrets,
        "delete_secret",
        lambda *_: (_ for _ in ()).throw(OSError("fixture cleanup failure")),
    )
    result = gateway(secret="replacement-fixture")
    assert "cleanup did not complete" in result.message
    ref = load_global_config()["providers"]["gateway"]["openai"]["api_key_ref"]
    assert secrets.load_secret(ref.removeprefix("keychain:")) == "replacement-fixture"
