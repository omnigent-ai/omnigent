"""Live provider-mutation journey against guarded disposable hosts.

The browser drives the catalog-key form.  The remaining mutations use the same
host-tunnel HTTP surface so the focused journey can cover state that has no
dedicated form on a single agent card.  No vendor executable or endpoint is
contacted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.provider_setup_runtime import HOST_IDS, ProviderSetupRuntime


def _config_path(runtime: ProviderSetupRuntime) -> Path:
    return runtime.root / "host-a/config/config.yaml"


def _config(runtime: ProviderSetupRuntime) -> dict[str, Any]:
    value = yaml.safe_load(_config_path(runtime).read_text())
    assert isinstance(value, dict)
    return value


def _secrets(runtime: ProviderSetupRuntime) -> dict[str, str]:
    path = runtime.root / "host-a/config/secrets.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _action(
    runtime: ProviderSetupRuntime, payload: dict[str, Any], *, expected: int = 200
) -> httpx.Response:
    response = httpx.post(
        f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup/actions",
        json=payload,
        headers={"Origin": runtime.url},
        timeout=30,
    )
    assert response.status_code == expected, response.text
    return response


def _detect(
    runtime: ProviderSetupRuntime, payload: dict[str, Any], *, expected: int = 200
) -> dict[str, Any]:
    response = httpx.post(
        f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup/detect",
        json=payload,
        headers={"Origin": runtime.url},
        timeout=30,
    )
    assert response.status_code == expected, response.text
    return response.json()


def _select_host_and_open_codex(page: Page, runtime: ProviderSetupRuntime) -> None:
    page.goto(runtime.url + "/settings/providers")
    expect(page.get_by_role("heading", name="Providers", exact=True)).to_be_visible()
    page.get_by_test_id("settings-providers-host").click()
    page.get_by_role("option", name="Fixture computer A · online", exact=True).click()
    page.get_by_test_id("setup-agent-codex").click()
    expect(page.get_by_role("button", name="API key", exact=True)).to_be_visible()


def _add_catalog_key_in_browser(page: Page) -> None:
    """Save a named OpenAI key through the rendered Providers form."""
    page.get_by_text("Advanced provider tools", exact=True).click()
    page.get_by_text("Add a provider", exact=True).click()
    page.get_by_role("button", name="Add provider", exact=True).click()
    page.get_by_label("Vendor", exact=True).click()
    options = page.get_by_role("option").all_inner_texts()
    openai = next(option for option in options if option.lower().startswith("openai"))
    page.get_by_role("option", name=openai, exact=True).click()
    page.get_by_label("API key", exact=True).fill("fixture-catalog-original")
    page.get_by_role("button", name="More options", exact=True).click()
    page.get_by_label("Connection name", exact=True).fill("catalog-mutation")
    page.get_by_label("Default model", exact=True).fill("fixture-catalog-model")
    page.get_by_role("button", name="Save provider", exact=True).click()
    expect(page.get_by_text("Added catalog-mutation", exact=True)).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("fixture-catalog-original")


def _seed_advanced_fields(runtime: ProviderSetupRuntime) -> str:
    config = _config(runtime)
    entry = config["providers"]["catalog-mutation"]
    assert isinstance(entry, dict)
    entry["future_setting"] = {"preserve": True}
    entry["openai"]["context_window"] = 24680
    old_ref = entry["openai"]["api_key_ref"]
    _config_path(runtime).write_text(yaml.safe_dump(config, sort_keys=False))
    assert isinstance(old_ref, str)
    return old_ref


def _replace_named_key_and_check_cleanup(runtime: ProviderSetupRuntime) -> dict[str, Any]:
    old_ref = _seed_advanced_fields(runtime)
    result = _action(
        runtime,
        {
            "action": "add_key",
            "provider": "openai",
            "name": "catalog-mutation",
            "model": "fixture-replacement-model",
            "secret": "fixture-catalog-replacement",
        },
    ).json()
    config = _config(runtime)
    entry = config["providers"]["catalog-mutation"]
    assert entry["future_setting"] == {"preserve": True}
    assert entry["openai"]["context_window"] == 24680
    assert entry["openai"]["models"]["default"] == "fixture-replacement-model"
    new_ref = entry["openai"]["api_key_ref"]
    assert new_ref != old_ref
    assert old_ref.removeprefix("keychain:") not in _secrets(runtime)
    assert new_ref.removeprefix("keychain:") in _secrets(runtime)
    assert [row["name"] for row in result["inventory"]["providers"]].count("catalog-mutation") == 1
    return {"old_ref_cleaned": True, "advanced_fields_preserved": True}


def _check_catalog_endpoint_replacement(runtime: ProviderSetupRuntime) -> dict[str, Any]:
    for vendor in ("openrouter", "openai"):
        _action(
            runtime,
            {
                "action": "add_key",
                "provider": vendor,
                "name": "endpoint-replacement",
                "model": "fixture-endpoint-model",
                "secret": f"fixture-{vendor}-key",
            },
        )
        config = _config(runtime)
        entry = config["providers"]["endpoint-replacement"]
        if vendor == "openrouter":
            assert entry["openai"]["wire_api"] == "chat"
            entry["future_setting"] = {"keep": True}
            entry["openai"]["context_window"] = 200000
            _config_path(runtime).write_text(yaml.safe_dump(config, sort_keys=False))
    assert entry["openai"]["base_url"] == "https://api.openai.com/v1"
    assert "wire_api" not in entry["openai"]
    assert entry["future_setting"] == {"keep": True}
    assert entry["openai"]["context_window"] == 200000
    summary = {
        "base_url": entry["openai"]["base_url"],
        "wire_api": entry["openai"].get("wire_api"),
        "advanced_fields_preserved": True,
    }
    _action(runtime, {"action": "remove_provider", "name": "endpoint-replacement"})
    return summary


def _check_shared_secret_is_retained(runtime: ProviderSetupRuntime) -> bool:
    _action(
        runtime,
        {
            "action": "add_key",
            "provider": "openai",
            "name": "shared-mutation",
            "model": "fixture-shared-original",
            "secret": "fixture-shared-original",
        },
    )
    config = _config(runtime)
    shared = config["providers"]["shared-mutation"]
    old_ref = shared["openai"]["api_key_ref"]
    sibling = copy.deepcopy(shared)
    sibling.pop("default", None)
    config["providers"]["shared-sibling"] = sibling
    _config_path(runtime).write_text(yaml.safe_dump(config, sort_keys=False))
    _action(
        runtime,
        {
            "action": "add_key",
            "provider": "openai",
            "name": "shared-mutation",
            "model": "fixture-shared-replacement",
            "secret": "fixture-shared-replacement",
        },
    )
    updated = _config(runtime)
    assert updated["providers"]["shared-sibling"]["openai"]["api_key_ref"] == old_ref
    assert old_ref.removeprefix("keychain:") in _secrets(runtime)
    return True


def _check_provider_and_harness_controls(runtime: ProviderSetupRuntime) -> dict[str, Any]:
    _action(
        runtime,
        {
            "action": "add_bedrock",
            "name": "mutation-bedrock",
            "base_url": "https://bedrock.fixture.invalid/runtime",
            "model": "anthropic.fixture-v1",
            "secret": "fixture-bedrock-secret",
        },
    )
    for harness in ("cursor", "antigravity", "copilot"):
        _action(
            runtime,
            {"action": "set_harness_key", "harness": harness, "secret": f"fixture-{harness}"},
        )
    _action(runtime, {"action": "set_copilot_host", "host": "enterprise.fixture.invalid"})
    _action(runtime, {"action": "set_opencode_model", "model": "fixture/opencode-model"})
    config = _config(runtime)
    assert config["providers"]["mutation-bedrock"]["kind"] == "bedrock"
    assert config["cursor"]["api_key_ref"].startswith("keychain:cursor-")
    assert config["antigravity"]["api_key_ref"].startswith("keychain:antigravity-")
    assert config["copilot"]["github_token_ref"].startswith("keychain:copilot-")
    assert config["copilot"]["github_host"] == "enterprise.fixture.invalid"
    assert config["opencode_model"] == "fixture/opencode-model"
    inventory = httpx.get(f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup", timeout=30).json()
    settings = inventory["harness_settings"]
    assert settings == {
        "cursor_key_configured": True,
        "antigravity_key_configured": True,
        "copilot_key_configured": True,
        "copilot_host": "enterprise.fixture.invalid",
        "opencode_model": "fixture/opencode-model",
    }
    return {"bedrock": True, "harness_controls": True}


def _check_acp_add_remove_and_import_fingerprint(runtime: ProviderSetupRuntime) -> dict[str, Any]:
    marker = runtime.root / "acp-command-ran"
    command = f"sh -c 'touch {marker}'"
    _action(
        runtime,
        {
            "action": "add_acp",
            "name": "Mutation Agent",
            "command": command,
            "model": "fixture-acp",
        },
    )
    assert not marker.exists(), "Saving ACP command data must not execute it"
    agent = next(
        row for row in _config(runtime)["acp"]["agents"] if row["name"] == "Mutation Agent"
    )
    assert agent["command"] == command
    _action(runtime, {"action": "remove_acp", "slug": "mutation-agent"})
    assert not _config(runtime).get("acp", {}).get("agents", [])

    source = runtime.root / "mutation-acpx.json"
    source.write_text(
        json.dumps(
            {
                "agents": {
                    "Imported Mutation": {
                        "command": "fixture-import",
                        "args": ["--header", "Authorization: Bearer FIXTURE_IMPORT_SECRET"],
                    }
                }
            }
        )
    )
    preview = _detect(runtime, {"import_source": "acpx", "import_path": str(source)})
    rendered_preview = json.dumps(preview)
    assert "FIXTURE_IMPORT_SECRET" not in rendered_preview
    item = next(row for row in preview["imports"] if row["name"] == "Imported Mutation")
    assert item["command"] == "fixture-import (2 arguments hidden)"

    source.write_text(json.dumps({"agents": {"Imported Mutation": {"command": "changed-import"}}}))
    rejected = _action(
        runtime,
        {
            "action": "import_acp",
            "source": "acpx",
            "path": str(source),
            "names": ["Imported Mutation"],
            "fingerprints": {"Imported Mutation": item["fingerprint"]},
        },
        expected=400,
    )
    assert "FIXTURE_IMPORT_SECRET" not in rejected.text
    assert not _config(runtime).get("acp", {}).get("agents", [])

    current = _detect(runtime, {"import_source": "acpx", "import_path": str(source)})
    fresh = next(row for row in current["imports"] if row["name"] == "Imported Mutation")
    _action(
        runtime,
        {
            "action": "import_acp",
            "source": "acpx",
            "path": str(source),
            "names": ["Imported Mutation"],
            "fingerprints": {"Imported Mutation": fresh["fingerprint"]},
        },
    )
    imported = _config(runtime)["acp"]["agents"]
    assert imported == [{"name": "Imported Mutation", "command": "changed-import"}]
    return {"save_did_not_execute_acp": True, "stale_import_rejected": True}


def _check_scope_replacement_in_browser(
    page: Page, runtime: ProviderSetupRuntime, recordings: Path
) -> bool:
    _action(
        runtime,
        {
            "action": "add_gateway",
            "name": "scope-reuse",
            "base_url": f"http://127.0.0.1:{runtime.mock_ports[0]}/v1",
            "families": ["anthropic", "openai"],
            "wire_api": "chat",
            "models": {"anthropic": "fixture-claude", "openai": "fixture-openai"},
            "secret": "fixture-scope-secret",
        },
    )
    page.reload()
    page.get_by_text("Advanced provider tools", exact=True).click()
    page.get_by_text("Manage all connections", exact=True).click()
    row = page.get_by_test_id("provider-row-scope-reuse")
    scope = row.get_by_label("Default scope for scope-reuse")
    scope.click()
    page.get_by_role("option", name="OpenAI", exact=True).click()
    expect(scope).to_contain_text("OpenAI")

    _action(
        runtime,
        {
            "action": "add_gateway",
            "name": "scope-reuse",
            "base_url": f"http://127.0.0.1:{runtime.mock_ports[0]}/v1",
            "families": ["anthropic"],
            "models": {"anthropic": "fixture-claude-next"},
            "secret": "fixture-scope-next",
        },
    )
    # An unrelated UI save refreshes inventory without remounting this named row.
    page.get_by_test_id("provider-row-fixture-secondary").get_by_role(
        "button", name="Make default"
    ).click()
    expect(scope).to_contain_text("Anthropic")
    scope.click()
    expect(page.get_by_role("option", name="OpenAI", exact=True)).to_have_count(0)
    page.keyboard.press("Escape")
    with page.expect_request(f"**/v1/hosts/{HOST_IDS[0]}/setup/actions") as request:
        row.get_by_role("button", name="Make default").click()
    assert request.value.post_data_json == {
        "action": "set_default",
        "name": "scope-reuse",
        "surface": "anthropic",
    }
    expect(row).to_contain_text("Default for Anthropic")
    saved = _config(runtime)["providers"]["scope-reuse"]
    assert "openai" not in saved
    assert saved["default"] in (True, "anthropic", ["anthropic"])
    page.screenshot(path=str(recordings / "scope-replacement.png"), full_page=True)
    return True


def _check_import_preview_switch_in_browser(
    page: Page, runtime: ProviderSetupRuntime, recordings: Path
) -> bool:
    source_a = runtime.root / "import-preview-a.json"
    source_b = runtime.root / "import-preview-b.json"
    source_a.write_text(json.dumps({"agents": {"Preview Swap": {"command": "fixture-a"}}}))
    source_b.write_text(json.dumps({"agents": {"Preview Swap": {"command": "fixture-b"}}}))
    page.get_by_role("button", name="Back to tools", exact=True).click()
    page.get_by_text("Import ACP agents", exact=True).click()
    page.get_by_label("Import format", exact=True).click()
    page.get_by_role("option", name="acpx", exact=True).click()
    path = page.get_by_label("Configuration path", exact=True)
    button = page.get_by_role("button", name="Import selected acpx agents")

    path.fill(str(source_a))
    with page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/detect") as first:
        page.get_by_role("button", name="Preview import").click()
    fingerprint_a = first.value.json()["imports"][0]["fingerprint"]
    checkbox = page.get_by_role("checkbox")
    expect(checkbox).to_have_count(1)
    checkbox.check()
    expect(button).to_be_enabled()

    path.fill(str(source_b))
    with page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/detect") as second:
        page.get_by_role("button", name="Preview import").click()
    fingerprint_b = second.value.json()["imports"][0]["fingerprint"]
    assert fingerprint_b != fingerprint_a
    expect(checkbox).not_to_be_checked()
    expect(button).to_be_disabled()
    page.screenshot(path=str(recordings / "changed-preview-cleared.png"), full_page=True)
    checkbox.check()
    with (
        page.expect_request(f"**/v1/hosts/{HOST_IDS[0]}/setup/actions") as request,
        page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/actions") as response,
    ):
        button.click()
    assert response.value.status == 200
    assert request.value.post_data_json == {
        "action": "import_acp",
        "source": "acpx",
        "names": ["Preview Swap"],
        "path": str(source_b),
        "fingerprints": {"Preview Swap": fingerprint_b},
    }
    saved = _config(runtime)["acp"]["agents"]
    assert next(row for row in saved if row["name"] == "Preview Swap")["command"] == "fixture-b"
    return True


def _check_malformed_config_is_rejected_before_secret_write(runtime: ProviderSetupRuntime) -> bool:
    path = _config_path(runtime)
    path.write_text("[broken")
    before = _secrets(runtime)
    response = _action(
        runtime,
        {
            "action": "add_gateway",
            "name": "must-not-save",
            "base_url": "http://127.0.0.1:1/v1",
            "families": ["openai"],
            "wire_api": "responses",
            "models": {"openai": "fixture"},
            "secret": "fixture-must-not-store",
        },
        expected=400,
    )
    assert response.json()["detail"] == "invalid setup configuration"
    assert path.read_text() == "[broken"
    assert _secrets(runtime) == before
    return True


def _write_sanitized_state(runtime: ProviderSetupRuntime, recordings: Path) -> None:
    """Record persisted fixture state without serializing disposable secret values."""
    config = _config(runtime)
    providers = config["providers"]
    catalog = providers["catalog-mutation"]
    state = {
        "fixture_only": True,
        "catalog_mutation": {
            "kind": catalog["kind"],
            "model": catalog["openai"]["models"]["default"],
            "context_window": catalog["openai"]["context_window"],
            "future_setting": catalog["future_setting"],
        },
        "bedrock": {
            "kind": providers["mutation-bedrock"]["kind"],
            "model": providers["mutation-bedrock"]["anthropic"]["models"]["default"],
        },
        "harness_settings": httpx.get(
            f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup", timeout=30
        ).json()["harness_settings"],
        "imported_acp_agents": config["acp"]["agents"],
        "stored_secret_slot_count": len(_secrets(runtime)),
    }
    (recordings / "mutation-state.json").write_text(json.dumps(state, indent=2))


def exercise_provider_mutations(
    page: Page, runtime: ProviderSetupRuntime, recordings: Path
) -> None:
    """Exercise provider writes through real browser/server/host processes."""
    _select_host_and_open_codex(page, runtime)
    _add_catalog_key_in_browser(page)
    result = {
        "result": "passed",
        "fixture_only": True,
        "catalog_named_replacement": _replace_named_key_and_check_cleanup(runtime),
        "catalog_endpoint_replacement": _check_catalog_endpoint_replacement(runtime),
        "shared_owned_slot_retained": _check_shared_secret_is_retained(runtime),
        "provider_and_harness_controls": _check_provider_and_harness_controls(runtime),
        "acp_and_import": _check_acp_add_remove_and_import_fingerprint(runtime),
        "scope_replacement_browser": _check_scope_replacement_in_browser(
            page, runtime, recordings
        ),
        "import_preview_switch_browser": _check_import_preview_switch_in_browser(
            page, runtime, recordings
        ),
    }
    _write_sanitized_state(runtime, recordings)
    result["malformed_config_rejected_before_secret_write"] = (
        _check_malformed_config_is_rejected_before_secret_write(runtime)
    )
    (recordings / "mutation-proof.json").write_text(json.dumps(result, indent=2))
