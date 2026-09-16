"""Exercise simultaneous save and cleanup failures through a real host and browser."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
import yaml
from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests._helpers.provider_setup_runtime import HOST_IDS, ProviderSetupRuntime

SAFE_ERROR = "Setup was not saved; stored secret cleanup did not complete"


class FaultRuntime(ProviderSetupRuntime):
    def prepare(self) -> None:
        super().prepare()
        boot = self.root / "boot/sitecustomize.py"
        source = boot.read_text()
        hook = "\nfrom tests._helpers.provider_setup_fault_injection import install\ninstall()\n"
        if hook not in source:
            boot.write_text(source + hook)


def action(runtime: ProviderSetupRuntime, secret: str) -> httpx.Response:
    return httpx.post(
        f"{runtime.url}/v1/hosts/{HOST_IDS[0]}/setup/actions",
        json={
            "action": "add_key",
            "provider": "openai",
            "name": "fault-original",
            "model": "fixture-model",
            "secret": secret,
        },
        headers={"Origin": runtime.url},
        timeout=30,
    )


def secrets(runtime: ProviderSetupRuntime) -> dict[str, str]:
    path = runtime.root / "host-a/config/secrets.json"
    return json.loads(path.read_text()) if path.exists() else {}


def fill_replacement(page: Page, secret: str) -> None:
    page.get_by_text("Advanced provider tools", exact=True).click()
    page.get_by_text("Add a provider", exact=True).click()
    page.get_by_role("button", name="Add provider", exact=True).click()
    page.get_by_label("Vendor", exact=True).click()
    option = next(
        text
        for text in page.get_by_role("option").all_inner_texts()
        if text.lower().startswith("openai")
    )
    page.get_by_role("option", name=option, exact=True).click()
    page.get_by_label("API key", exact=True).fill(secret)
    page.get_by_role("button", name="More options", exact=True).click()
    page.get_by_label("Connection name", exact=True).fill("fault-original")
    page.get_by_label("Default model", exact=True).fill("fixture-model-new")


def check(page: Page, runtime: FaultRuntime, recordings: Path) -> dict[str, object]:
    baseline = action(runtime, "fixture-original-credential")
    assert baseline.status_code == 200, baseline.text
    config_path = runtime.root / "host-a/config/config.yaml"
    before_config = config_path.read_bytes()
    before_secrets = secrets(runtime)
    old_ref = yaml.safe_load(before_config)["providers"]["fault-original"]["openai"]["api_key_ref"]
    old_slot = old_ref.removeprefix("keychain:")
    assert before_secrets[old_slot] == "fixture-original-credential"

    page.goto(runtime.url + "/settings/providers")
    page.get_by_test_id("settings-providers-host").click()
    page.get_by_role("option", name="Fixture computer A · online", exact=True).click()
    page.get_by_test_id("setup-agent-codex").click()
    fill_replacement(page, "fixture-replacement-credential")

    armed = runtime.root / "host-a/fail-save-and-cleanup"
    armed.write_text("fixture only")
    with page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/actions") as response_info:
        page.get_by_role("button", name="Save provider", exact=True).click()
    response = response_info.value
    assert response.status == 502
    assert response.json() == {"detail": SAFE_ERROR}
    expect(page.get_by_role("alert")).to_contain_text(SAFE_ERROR)
    assert page.get_by_text("Added fault-original", exact=True).count() == 0
    expect(page.get_by_role("button", name="Save provider", exact=True)).to_be_visible()
    assert config_path.read_bytes() == before_config
    after_secrets = secrets(runtime)
    assert after_secrets[old_slot] == "fixture-original-credential"
    orphan_slots = set(after_secrets) - set(before_secrets)
    assert len(orphan_slots) == 1
    assert after_secrets[next(iter(orphan_slots))] == "fixture-replacement-credential"
    assert (runtime.root / "host-a/save-fault-hit").exists()
    assert (runtime.root / "host-a/cleanup-fault-hit").exists()
    for text in (
        "fixture-original-credential",
        "fixture-replacement-credential",
        "fixture-private-",
    ):
        assert text not in response.body().decode()
        assert text not in page.locator("body").inner_text()
    page.screenshot(path=str(recordings / "save-and-cleanup-failed.png"), full_page=True)
    page.get_by_text(SAFE_ERROR, exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(recordings / "error-banner.png"))

    page.reload()
    page.get_by_test_id("setup-agent-codex").click()
    expect(page.get_by_test_id("agent-provider-row-fault-original")).to_be_visible()
    assert config_path.read_bytes() == before_config
    armed.unlink()
    fill_replacement(page, "fixture-recovered-credential")
    with page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/actions") as recovery_info:
        page.get_by_role("button", name="Save provider", exact=True).click()
    recovered = recovery_info.value
    assert recovered.status == 200, recovered.text()
    expect(page.get_by_text("Added fault-original", exact=True)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)
    final_entry = yaml.safe_load(config_path.read_text())["providers"]["fault-original"]
    final_ref = final_entry["openai"]["api_key_ref"]
    assert final_ref != old_ref
    assert final_entry["openai"]["models"]["default"] == "fixture-model-new"
    final_secrets = secrets(runtime)
    assert old_slot not in final_secrets
    assert final_secrets[final_ref.removeprefix("keychain:")] == "fixture-recovered-credential"
    assert orphan_slots.issubset(final_secrets)
    page.reload()
    page.get_by_test_id("setup-agent-codex").click()
    expect(page.get_by_test_id("agent-provider-row-fault-original")).to_be_visible()
    page.screenshot(path=str(recordings / "save-recovered.png"), full_page=True)
    return {
        "host": "Fixture computer A",
        "response_status": response.status,
        "sanitized_error": SAFE_ERROR,
        "original_config_and_credential_retained": True,
        "one_disposable_orphan_after_cleanup_failure": True,
        "recovery_saved_and_reloaded": True,
        "recovery_saved_through_browser": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--recordings", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("OMNIGENT_DISABLE_KEYRING") != "1":
        parser.error("Run with OMNIGENT_DISABLE_KEYRING=1")
    if os.environ.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.null.Keyring":
        parser.error("Run with null Keyring backend")
    if args.state.exists():
        parser.error("--state must name a new disposable directory")
    checkout = Path(__file__).resolve().parents[3]
    runtime = FaultRuntime(args.state, checkout)
    args.recordings.mkdir(parents=True, exist_ok=True)
    try:
        runtime.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                record_video_dir=str(args.recordings),
                record_video_size={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            try:
                proof = check(page, runtime, args.recordings)
            except BaseException:
                page.screenshot(path=str(args.recordings / "failure.png"), full_page=True)
                raise
            finally:
                context.close()
                browser.close()
        (args.recordings / "proof.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps({"result": "passed", "recordings": str(args.recordings)}))
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
