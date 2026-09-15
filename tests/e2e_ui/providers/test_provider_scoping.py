"""Browser regression for agent-scoped provider settings on a disposable host."""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.provider_setup_runtime import HOST_IDS, ProviderSetupRuntime

CLAUDE_NOTICE = (
    "Claude logins stored only in the OS Keychain are checked through guided sign-in, "
    "not detection."
)


@pytest.fixture
def provider_runtime(tmp_path: Path, built_spa: None) -> Iterator[ProviderSetupRuntime]:
    runtime = ProviderSetupRuntime(
        tmp_path / "provider-scoping", Path(__file__).resolve().parents[3]
    )
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()


def _config(runtime: ProviderSetupRuntime) -> dict:
    return yaml.safe_load((runtime.root / "host-a/config/config.yaml").read_text())


def _seed_cli_subscriptions(runtime: ProviderSetupRuntime) -> None:
    """Represent existing CLI logins in fixture config without running vendor CLIs."""
    path = runtime.root / "host-a/config/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["providers"]["fixture-primary"].pop("default")
    config["providers"]["claude-subscription"] = {
        "kind": "subscription",
        "cli": "claude",
        "default": "anthropic",
    }
    config["providers"]["codex-subscription"] = {
        "kind": "subscription",
        "cli": "codex",
        "default": "openai",
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    response = httpx.get(f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup", timeout=10)
    response.raise_for_status()
    inventory = response.json()
    assert inventory["effective_defaults"]["anthropic"] == "claude-subscription"
    assert inventory["effective_defaults"]["openai"] == "codex-subscription"


def _check_implicit_pi_cli_default(
    page: Page, runtime: ProviderSetupRuntime, recordings: Path
) -> None:
    """Resolve Pi through the guarded host's disposable Codex config."""
    path = runtime.root / "host-a/config/config.yaml"
    baseline = path.read_text()
    config = yaml.safe_load(baseline)
    config["providers"]["fixture-primary"].pop("default")
    config["providers"]["codex-config"] = {
        "kind": "cli-config",
        "cli": "codex",
        "model_provider": "FixtureGateway",
        "default": "openai",
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    inventory_response = httpx.get(f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup", timeout=10)
    inventory_response.raise_for_status()
    inventory = inventory_response.json()
    assert inventory["effective_defaults"]["pi"] is None
    assert inventory["pi_default_requires_detection"] is True

    page.reload()
    _agent(page, "pi")
    button = page.get_by_role("button", name="Check Pi default", exact=True)
    expect(button).to_be_visible()
    expect(
        page.get_by_text("Check local CLI configuration to identify Pi’s default.")
    ).to_be_visible()
    expect(page.get_by_test_id("agent-provider-row-codex-config")).not_to_contain_text(
        "Used for new sessions"
    )
    page.screenshot(path=str(recordings / "pi-default-unresolved.png"), full_page=True)

    # No Codex table exists yet, so the real resolver must reject this fallback.
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/v1/hosts/{HOST_IDS[0]}/setup/detect")
            and response.request.method == "POST"
        )
    ) as detection:
        button.click()
    real_response = detection.value
    assert real_response.request.post_data_json == {"pi_default": True}
    assert real_response.status == 200
    real_result = real_response.json()
    assert real_result["pi_default_checked"] is True
    assert real_result["pi_default_provider"] is None
    assert real_result["warnings"] == []
    expect(page.get_by_text("No compatible default found.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Check again", exact=True)).to_be_visible()
    assert path.read_text() == yaml.safe_dump(config, sort_keys=False)

    codex_config = runtime.root / "host-a/config/codex/config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text(
        "[model_providers.FixtureGateway]\n"
        'name = "Fixture gateway"\n'
        'base_url = "https://fixture.ai-gateway.cloud.databricks.com/codex/v1"\n'
        "[model_providers.FixtureGateway.auth]\n"
        'command = "fixture-token-command"\n'
    )
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/v1/hosts/{HOST_IDS[0]}/setup/detect")
            and response.request.method == "POST"
        )
    ) as checked:
        page.get_by_role("button", name="Check again", exact=True).click()
    checked_response = checked.value
    assert checked_response.request.post_data_json == {"pi_default": True}
    assert checked_response.status == 200
    assert checked_response.json()["pi_default_checked"] is True
    assert checked_response.json()["pi_default_provider"] == "codex-config"
    expect(page.get_by_text("Default: codex-config", exact=True)).to_be_visible()
    expect(page.get_by_test_id("agent-provider-row-codex-config")).to_contain_text(
        "Used for new sessions"
    )
    page.screenshot(path=str(recordings / "pi-default-resolved.png"), full_page=True)
    assert path.read_text() == yaml.safe_dump(config, sort_keys=False)

    path.write_text(baseline)
    page.reload()
    _agent(page, "pi")
    expect(page.get_by_role("button", name="Check Pi default", exact=True)).to_have_count(0)


def _agent(page: Page, agent_id: str) -> None:
    page.get_by_test_id(f"setup-agent-{agent_id}").click()


def _back(page: Page) -> None:
    page.get_by_role("button", name="Back to agents", exact=True).click()


def _wait_for_host_status(runtime: ProviderSetupRuntime, host_id: str, status: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = httpx.get(f"{runtime.url}/v1/hosts", timeout=3)
        response.raise_for_status()
        rows = response.json()
        hosts = rows if isinstance(rows, list) else rows.get("hosts", [])
        if any(row.get("host_id") == host_id and row.get("status") == status for row in hosts):
            return
        time.sleep(0.2)
    raise AssertionError(f"Fixture host {host_id} did not become {status}")


def test_agent_scopes_defaults_detection_and_gateway_validation(
    page: Page, provider_runtime: ProviderSetupRuntime, recordings: Path | None = None
) -> None:
    runtime = provider_runtime
    recordings = recordings or runtime.root.with_name(runtime.root.name.removesuffix("-state"))
    recordings.mkdir(parents=True, exist_ok=True)
    config_before = (runtime.root / "host-a/config/config.yaml").read_text()
    writes: list[str] = []
    page.on(
        "request",
        lambda request: (
            writes.append(f"{request.method} {request.url}")
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}
            else None
        ),
    )
    page.goto(runtime.url + "/settings/providers")
    page.get_by_test_id("settings-providers-host").click()
    page.get_by_role("option", name="Fixture computer A · online", exact=True).click()
    expect(page.get_by_test_id("setup-agent-pi")).to_be_visible()
    expect(page.get_by_role("heading", name="Agents", exact=True)).to_be_visible()
    assert writes == []
    assert (runtime.root / "host-a/config/config.yaml").read_text() == config_before
    page.screenshot(path=str(recordings / "overview-light.png"), full_page=True)

    _check_implicit_pi_cli_default(page, runtime, recordings)

    page.goto(runtime.url + "/settings/appearance")
    page.get_by_test_id("theme-dark").click()
    expect(page.locator("html")).to_have_class("dark")
    page.goto(runtime.url + "/settings/providers")
    expect(page.get_by_test_id("settings-providers-host")).to_contain_text("Fixture computer A")
    expect(page.get_by_test_id("setup-agent-pi")).to_be_visible()
    page.screenshot(path=str(recordings / "overview-dark.png"), full_page=True)

    _seed_cli_subscriptions(runtime)
    page.reload()
    expect(page.get_by_test_id("setup-agent-pi")).to_be_visible()

    _agent(page, "pi")
    expect(page.get_by_test_id("agent-provider-row-fixture-primary")).to_be_visible()
    expect(page.get_by_test_id("agent-provider-row-fixture-secondary")).to_be_visible()
    expect(page.get_by_test_id("agent-provider-row-claude-subscription")).to_have_count(0)
    expect(page.get_by_test_id("agent-provider-row-codex-subscription")).to_have_count(0)
    expect(
        page.get_by_text("Saves local routing to Pi; it does not check your Pi sign-in.")
    ).to_be_visible()
    page.screenshot(path=str(recordings / "pi-local-configuration.png"), full_page=True)
    page.get_by_role("button", name="Use Pi’s local configuration", exact=True).click()
    pi_row = page.get_by_test_id("agent-provider-row-pi-subscription")
    expect(pi_row.get_by_text("Used for new sessions", exact=True)).to_be_visible()
    assert _config(runtime)["providers"]["pi-subscription"]["default"] == "pi"
    page.reload()
    _agent(page, "pi")
    expect(pi_row.get_by_text("Used for new sessions", exact=True)).to_be_visible()
    page.screenshot(path=str(recordings / "pi-local-reloaded.png"), full_page=True)

    with page.expect_request(f"**/v1/hosts/{HOST_IDS[0]}/setup/detect") as status_request:
        page.get_by_role("button", name="Check status", exact=True).click()
    assert status_request.value.post_data_json == {"harness": "pi-native"}
    expect(page.get_by_text("Ready according to setup", exact=True)).to_be_visible()
    page.route(
        f"**/v1/hosts/{HOST_IDS[0]}/setup/detect",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"Fixture status check unavailable"}',
        ),
        times=1,
    )
    page.get_by_role("button", name="Check status", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Fixture status check unavailable")
    expect(page.get_by_text("Ready according to setup", exact=True)).to_have_count(0)

    page.get_by_role("button", name="Find credentials on this computer").click()
    results = page.get_by_test_id("provider-detection-results")
    expect(results).to_be_visible()
    expect(results).not_to_contain_text(CLAUDE_NOTICE)

    _back(page)
    _agent(page, "codex")
    codex_row = page.get_by_test_id("agent-provider-row-codex-subscription")
    expect(codex_row.get_by_text("Used for new sessions", exact=True)).to_be_visible()
    expect(page.get_by_test_id("agent-provider-row-claude-subscription")).to_have_count(0)
    expect(page.get_by_test_id("agent-provider-row-pi-subscription")).to_have_count(0)
    expect(page.get_by_test_id("provider-detection-results")).not_to_contain_text(CLAUDE_NOTICE)

    _back(page)
    _agent(page, "claude")
    claude_row = page.get_by_test_id("agent-provider-row-claude-subscription")
    expect(claude_row.get_by_text("Used for new sessions", exact=True)).to_be_visible()
    expect(page.get_by_test_id("agent-provider-row-codex-subscription")).to_have_count(0)
    expect(page.get_by_test_id("agent-provider-row-pi-subscription")).to_have_count(0)
    expect(page.get_by_test_id("provider-detection-results")).to_contain_text(CLAUDE_NOTICE)

    _back(page)
    _agent(page, "opencode")
    expect(page.get_by_test_id("provider-detection-results")).to_have_count(0)
    expect(page.get_by_role("button", name="Find credentials on this computer")).to_have_count(0)
    expect(page.locator("body")).not_to_contain_text(CLAUDE_NOTICE)

    _back(page)
    _agent(page, "pi")
    page.get_by_role("button", name="Compatible gateway", exact=True).click()
    save = page.get_by_role("button", name="Save gateway", exact=True)
    expect(save).to_be_disabled()
    page.get_by_label("Gateway name", exact=True).fill("pi-fixture-gateway")
    page.get_by_label("Base URL", exact=True).fill("javascript:invalid")
    page.get_by_label("OpenAI model", exact=True).fill("fixture-model")
    page.get_by_label("API key or token", exact=True).fill("fixture-only-secret")
    expect(save).to_be_enabled()
    save.click()
    expect(page.get_by_role("alert")).to_contain_text("invalid setup configuration")
    assert "pi-fixture-gateway" not in _config(runtime)["providers"]

    page.get_by_label("Base URL", exact=True).fill(f"http://127.0.0.1:{runtime.mock_ports[1]}/v1")
    page.route(
        f"**/v1/hosts/{HOST_IDS[0]}/setup/actions",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"Fixture save temporarily unavailable"}',
        ),
        times=1,
    )
    save.click()
    expect(page.get_by_role("alert")).to_contain_text("Fixture save temporarily unavailable")
    expect(page.get_by_test_id("settings-providers-host")).to_contain_text("Fixture computer A")
    assert "pi-fixture-gateway" not in _config(runtime)["providers"]
    assert (
        "pi-fixture-gateway"
        not in yaml.safe_load((runtime.root / "host-b/config/config.yaml").read_text())[
            "providers"
        ]
    )

    save.click()
    gateway = page.get_by_test_id("agent-provider-row-pi-fixture-gateway")
    expect(gateway).to_be_visible()
    gateway.get_by_role("button", name="Use for new Pi sessions", exact=True).click()
    expect(gateway.get_by_text("Used for new sessions", exact=True)).to_be_visible()
    page.reload()
    _agent(page, "pi")
    expect(page.get_by_test_id("agent-provider-row-pi-fixture-gateway")).to_contain_text(
        "Used for new sessions"
    )
    saved = _config(runtime)
    assert saved["providers"]["pi-fixture-gateway"]["default"] == "pi"
    response = httpx.get(f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup", timeout=10)
    response.raise_for_status()
    assert response.json()["effective_defaults"] == {
        "anthropic": "claude-subscription",
        "openai": "codex-subscription",
        "gemini": None,
        "pi": "pi-fixture-gateway",
    }
    assert "default" not in saved["providers"]["pi-subscription"]
    assert "fixture-only-secret" not in json.dumps(saved)

    _back(page)
    page.get_by_role("button", name="More agents", exact=True).click()
    antigravity = page.get_by_test_id("setup-agent-antigravity")
    expect(antigravity).to_contain_text("Installation needed")
    antigravity.click()
    expect(page.get_by_text("Installation needed", exact=True)).to_be_visible()
    expect(page.locator("body")).not_to_contain_text(CLAUDE_NOTICE)
    with (
        page.expect_request(f"**/v1/hosts/{HOST_IDS[0]}/setup/detect") as status_request,
        page.expect_response(
            lambda response: (
                response.url.endswith(f"/v1/hosts/{HOST_IDS[0]}/setup/detect")
                and response.request.method == "POST"
            )
        ) as status_response,
    ):
        page.get_by_role("button", name="Check status", exact=True).click()
    assert status_request.value.post_data_json == {"harness": "antigravity-native"}
    assert status_response.value.json()["harness_status"] == {
        "harness": "antigravity-native",
        "availability": False,
    }
    expect(page.get_by_text("Installation needed", exact=True)).to_be_visible()
    expect(page.locator("body")).not_to_contain_text(CLAUDE_NOTICE)
    page.screenshot(path=str(recordings / "antigravity-dark.png"), full_page=True)

    host_a = runtime.processes[3]
    os.killpg(host_a.pid, signal.SIGTERM)
    _wait_for_host_status(runtime, HOST_IDS[0], "offline")
    page.reload()
    expect(page.get_by_test_id("settings-providers-host")).to_contain_text("Fixture computer A")
    expect(page.get_by_role("status")).to_contain_text(
        "Fixture computer A is offline. Its settings cannot be read or changed "
        "until it reconnects; "
        "this selection will stay on Fixture computer A.",
    )
    expect(page.get_by_test_id("setup-agent-pi")).to_have_count(0)
    expect(page.get_by_test_id("setup-agent-antigravity")).to_have_count(0)
    page.screenshot(path=str(recordings / "selected-host-offline-dark.png"), full_page=True)
