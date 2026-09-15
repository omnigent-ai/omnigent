"""Exercise CLI readiness and required catalog models through guarded host APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests._helpers.provider_setup_readiness import ProviderSetupReadinessRuntime
from tests._helpers.provider_setup_runtime import HOST_IDS, ProviderSetupRuntime


def select_host(page: Page, letter: str) -> None:
    page.get_by_test_id("settings-providers-host").click()
    page.get_by_role("option", name=f"Fixture computer {letter} · online", exact=True).click()


def check(page: Page, runtime: ProviderSetupRuntime, recordings: Path) -> dict[str, object]:
    hosts_response = httpx.get(f"{runtime.url}/v1/hosts", timeout=10)
    hosts_response.raise_for_status()
    hosts = hosts_response.json()
    rows = hosts if isinstance(hosts, list) else hosts["hosts"]
    observed = {row["host_id"]: row["configured_harnesses"]["codex-native"] for row in rows}
    (recordings / "readiness.json").write_text(json.dumps(observed, indent=2))
    assert observed == {
        HOST_IDS[0]: "version-too-low",
        HOST_IDS[1]: "binary-missing",
    }, observed

    inventories = []
    before = {}
    for index, host in enumerate(("host-a", "host-b")):
        response = httpx.get(f"{runtime.url}/v1/hosts/{HOST_IDS[index]}/setup", timeout=10)
        response.raise_for_status()
        inventories.append(response.json())
        assert {p["name"] for p in inventories[-1]["providers"]} == {
            "fixture-primary",
            "fixture-secondary",
        }
        before[host] = (runtime.root / host / "config/config.yaml").read_bytes()
    assert "codex-login" in inventories[0]["supported_operations"]
    assert "codex-login" not in inventories[1]["supported_operations"]
    (recordings / "inventory.json").write_text(json.dumps(inventories, indent=2))
    probes = [
        json.loads(line)
        for line in (runtime.root / "codex-version-probes.jsonl").read_text().splitlines()
    ]
    assert probes and all(probe == {"argv": ["--version"]} for probe in probes)
    (recordings / "version-probes.json").write_text(json.dumps(probes, indent=2))

    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}
            else None
        ),
    )
    page.goto(runtime.url + "/settings/providers")
    for letter, label, guidance, filename in (
        ("A", "Update needed", "Update Codex", "outdated-codex"),
        ("B", "Installation needed", "Install Codex", "missing-codex"),
    ):
        select_host(page, letter)
        row = page.get_by_test_id("setup-agent-codex")
        expect(row).to_contain_text(label)
        expect(row).not_to_contain_text("saved connection")
        page.screenshot(path=str(recordings / f"{filename}-overview.png"), full_page=True)
        row.click()
        expect(page.get_by_test_id("agent-provider-row-fixture-primary")).to_be_visible()
        expect(page.get_by_text(guidance, exact=True)).to_be_visible()
        if letter == "A":
            expect(
                page.get_by_text(
                    "Update Codex on Fixture computer A, then check setup status again.",
                    exact=True,
                )
            ).to_be_visible()
        sign_in = page.get_by_role("button", name="ChatGPT subscription", exact=True)
        expect(sign_in).to_be_disabled()
        sign_in.dispatch_event("click")
        expect(page.get_by_test_id("setup-terminal")).to_have_count(0)
        page.screenshot(path=str(recordings / f"{filename}-detail.png"), full_page=True)
        page.get_by_role("button", name="Back to agents", exact=True).click()
    assert writes == [], writes
    for host, content in before.items():
        assert (runtime.root / host / "config/config.yaml").read_bytes() == content

    select_host(page, "A")
    page.get_by_test_id("setup-agent-codex").click()
    with page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/detect") as detection:
        page.get_by_role("button", name="Find credentials on this computer").click()
    response = detection.value
    assert response.status == 200, response.text()
    detected = response.json()
    (recordings / "discovery.json").write_text(json.dumps(detected, indent=2))
    assert detected["models"]["openai"] == []
    assert detected["default_models"]["openai"] is None
    expect(page.get_by_text("No additional credentials were found.", exact=True)).to_be_visible()
    page.get_by_role("button", name="API key", exact=True).click()
    model = page.get_by_placeholder("Required for this vendor", exact=True)
    expect(model).to_be_visible()
    expect(model).to_have_attribute("required", "")
    expect(page.get_by_role("button", name="More options", exact=True)).to_have_attribute(
        "aria-expanded", "false"
    )
    expect(
        page.get_by_text("This vendor has no catalog default on this computer.", exact=True)
    ).to_be_visible()
    page.get_by_label("API key", exact=True).fill("fixture-readiness-key")
    save = page.get_by_role("button", name="Save provider", exact=True)
    expect(save).to_be_disabled()
    model.fill("   ")
    expect(save).to_be_disabled()
    save.dispatch_event("click")
    assert writes == [response.url], writes
    assert (runtime.root / "host-a/config/config.yaml").read_bytes() == before["host-a"]
    page.screenshot(path=str(recordings / "required-model-blank.png"), full_page=True)
    model.fill("fixture-explicit-model")
    expect(save).to_be_enabled()
    with page.expect_response(f"**/v1/hosts/{HOST_IDS[0]}/setup/actions") as saved:
        save.click()
    saved_response = saved.value
    assert saved_response.status == 200, saved_response.text()
    assert saved_response.request.post_data_json == {
        "action": "add_key",
        "provider": "openai",
        "model": "fixture-explicit-model",
        "secret": "fixture-readiness-key",
    }
    expect(page.get_by_test_id("agent-provider-row-openai")).to_be_visible()
    page.reload()
    page.get_by_test_id("setup-agent-codex").click()
    expect(page.get_by_test_id("agent-provider-row-openai")).to_be_visible()
    summary = runtime.read_cli_summary("host-a")
    provider = next(row for row in summary["providers"] if row["name"] == "openai")
    assert provider["models"]["openai"]["default"] == "fixture-explicit-model"
    assert (runtime.root / "host-b/config/config.yaml").read_bytes() == before["host-b"]
    assert all("/setup-operations" not in url for url in writes)
    page.screenshot(path=str(recordings / "explicit-model-reloaded.png"), full_page=True)
    return {
        "result": "passed",
        "outdated_cli_with_advertised_login_blocked": True,
        "readiness_precedes_saved_connections_on_both_hosts": True,
        "explicit_empty_catalog_requires_nonblank_model": True,
        "model_persisted_through_browser_and_cli_loader": True,
        "other_host_unchanged": True,
        "guard_selfcheck_passed": True,
        "dummy_codex_version_executed_by_host": True,
        "http_responses_intercepted": False,
        "real_vendor_authentication": "not tested; controlled fixture only",
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
    runtime = ProviderSetupReadinessRuntime(args.state, checkout)
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
        print(json.dumps(proof))
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
