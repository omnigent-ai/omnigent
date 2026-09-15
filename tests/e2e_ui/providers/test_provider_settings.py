"""Browser settings writes over real host tunnels and SDK provider execution.

Build the SPA first, then run this module with ``--ui-skip-build``. All app
processes use the disposable fixture; no vendor executable or credential is used.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.provider_setup_runtime import HOST_IDS, ProviderSetupRuntime


@pytest.fixture
def provider_runtime(tmp_path: Path, built_spa: None) -> Iterator[ProviderSetupRuntime]:
    runtime = ProviderSetupRuntime(
        tmp_path / "provider-runtime", Path(__file__).resolve().parents[3]
    )
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()


def _select_host(page: Page, name: str) -> None:
    page.get_by_test_id("settings-providers-host").click()
    page.get_by_role("option", name=f"Fixture computer {name} · online", exact=True).click()
    expect(page.get_by_test_id("setup-agent-codex")).to_be_visible()


def _open_codex(page: Page) -> None:
    page.get_by_test_id("setup-agent-codex").click()
    expect(page.get_by_role("button", name="Compatible gateway", exact=True)).to_be_visible()


def _config(runtime: ProviderSetupRuntime, host: str) -> dict:
    return yaml.safe_load((runtime.root / host / "config/config.yaml").read_text())


def _create_session(runtime: ProviderSetupRuntime) -> str:
    bundle = io.BytesIO()
    spec = b"""name: provider_fixture
prompt: Reply briefly without tools.
executor:
  harness: openai-agents
  model: gpt-4o-mini
"""
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        entry = tarfile.TarInfo("provider_fixture.yaml")
        entry.size = len(spec)
        archive.addfile(entry, io.BytesIO(spec))
    result = httpx.post(
        f"{runtime.url}/v1/sessions",
        data={
            "metadata": json.dumps(
                {"host_id": HOST_IDS[0], "workspace": str(runtime.socket_root / "host-a")}
            )
        },
        files={"bundle": ("provider.tar.gz", bundle.getvalue(), "application/gzip")},
        timeout=30,
    )
    result.raise_for_status()
    session_id = result.json()["session_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = httpx.get(f"{runtime.url}/v1/sessions/{session_id}", timeout=5)
        state.raise_for_status()
        if state.json().get("runner_online"):
            return session_id
        time.sleep(0.2)
    raise AssertionError("Real host runner did not become ready")


def _send_and_expect(runtime: ProviderSetupRuntime, session: str, marker: str) -> None:
    before = httpx.get(f"{runtime.url}/v1/sessions/{session}/items", timeout=5).text.count(
        "MOCK_PROVIDER_"
    )
    result = httpx.post(
        f"{runtime.url}/v1/sessions/{session}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": marker}]},
        },
        timeout=30,
    )
    result.raise_for_status()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = httpx.get(f"{runtime.url}/v1/sessions/{session}/items", timeout=5)
        result.raise_for_status()
        if result.text.count("MOCK_PROVIDER_") > before:
            return
        time.sleep(0.2)
    raise AssertionError("SDK response did not appear in real session items")


def _requests(runtime: ProviderSetupRuntime, provider: str) -> list[dict]:
    path = runtime.root / f"mock-{provider}-requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _exercise_settings_persist_on_selected_host_and_route_new_sessions(
    page: Page,
    provider_runtime: ProviderSetupRuntime,
) -> None:
    """Settings writes persist, affect only host A, and pin existing SDK sessions."""
    runtime = provider_runtime
    page.goto(runtime.url + "/settings/providers")
    expect(page.get_by_role("heading", name="Providers", exact=True)).to_be_visible()
    expect(page.get_by_test_id("setup-agent-codex")).to_have_count(0)
    _select_host(page, "A")
    _open_codex(page)

    existing = _create_session(runtime)
    httpx.post(
        f"http://127.0.0.1:{runtime.mock_ports[0]}/control/pause", timeout=5
    ).raise_for_status()
    result = httpx.post(
        f"{runtime.url}/v1/sessions/{existing}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "provider-before-switch"}],
            },
        },
        timeout=30,
    )
    result.raise_for_status()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = httpx.get(f"http://127.0.0.1:{runtime.mock_ports[0]}/control/status", timeout=5)
        if status.json()["waiting"]:
            break
        time.sleep(0.2)
    assert status.json()["waiting"], (
        "Provider A must have a live held request before changing defaults"
    )
    old_runner = httpx.get(f"{runtime.url}/v1/sessions/{existing}", timeout=5).json()["runner_id"]
    assert any("provider-before-switch" in json.dumps(row) for row in _requests(runtime, "a"))
    assert not _requests(runtime, "b")

    page.get_by_role("button", name="Compatible gateway", exact=True).click()
    page.get_by_label("Gateway name", exact=True).fill("ui-local-gateway")
    page.get_by_label("Base URL", exact=True).fill(f"http://127.0.0.1:{runtime.mock_ports[1]}/v1")
    page.get_by_label("Anthropic family", exact=True).uncheck()
    page.get_by_label("OpenAI model", exact=True).fill("gpt-4o-mini")
    page.get_by_label("API key or token", exact=True).fill("fixture-new-dummy-key")
    page.get_by_role("button", name="Save gateway", exact=True).click()
    row = page.get_by_test_id("agent-provider-row-ui-local-gateway")
    expect(row).to_be_visible()
    row.get_by_role("button", name="Use for new Codex sessions", exact=True).click()
    expect(row.get_by_text("Used for new sessions", exact=True)).to_be_visible()

    page.reload()
    expect(page.get_by_test_id("settings-providers-host")).to_contain_text("Fixture computer A")
    _open_codex(page)
    row = page.get_by_test_id("agent-provider-row-ui-local-gateway")
    expect(row.get_by_text("Used for new sessions", exact=True)).to_be_visible()
    config_a = _config(runtime, "host-a")
    assert config_a["providers"]["ui-local-gateway"]["default"] in (True, ["openai"], "openai")
    summary = runtime.read_cli_summary("host-a")
    saved = next(
        provider for provider in summary["providers"] if provider["name"] == "ui-local-gateway"
    )
    assert saved["defaults"] == ["openai"]
    assert saved["models"]["openai"]["default"] == "gpt-4o-mini"
    secrets_file = runtime.root / "host-a/config/secrets.json"
    assert "fixture-new-dummy-key" in secrets_file.read_text()
    assert secrets_file.stat().st_mode & 0o777 == 0o600
    assert "fixture-new-dummy-key" not in json.dumps(config_a)
    assert "ui-local-gateway" not in _config(runtime, "host-b")["providers"]
    assert "fixture-new-dummy-key" not in page.locator("body").inner_text()

    _select_host(page, "B")
    _open_codex(page)
    expect(page.get_by_test_id("agent-provider-row-ui-local-gateway")).to_have_count(0)
    _select_host(page, "A")
    _open_codex(page)
    fresh = _create_session(runtime)
    _send_and_expect(runtime, fresh, "provider-after-switch")
    assert any("provider-after-switch" in json.dumps(row) for row in _requests(runtime, "b"))
    assert not any("provider-after-switch" in json.dumps(row) for row in _requests(runtime, "a"))

    httpx.post(
        f"http://127.0.0.1:{runtime.mock_ports[0]}/control/release", timeout=5
    ).raise_for_status()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        items = httpx.get(f"{runtime.url}/v1/sessions/{existing}/items", timeout=5)
        if "MOCK_PROVIDER_A_RESPONSE" in items.text:
            break
        time.sleep(0.2)
    assert "MOCK_PROVIDER_A_RESPONSE" in items.text
    assert (
        httpx.get(f"{runtime.url}/v1/sessions/{existing}", timeout=5).json()["runner_id"]
        == old_runner
    )
    _send_and_expect(runtime, existing, "provider-existing-session")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any(
        "provider-existing-session" in json.dumps(row) for row in _requests(runtime, "a")
    ):
        time.sleep(0.2)
    assert any("provider-existing-session" in json.dumps(row) for row in _requests(runtime, "a"))
    assert not any(
        "provider-existing-session" in json.dumps(row) for row in _requests(runtime, "b")
    )

    row = page.get_by_test_id("agent-provider-row-ui-local-gateway")
    row.get_by_role("button", name="Manage", exact=True).click()
    row.get_by_role("button", name="Remove", exact=True).click()
    page.get_by_role("alertdialog", name="Remove ui-local-gateway").get_by_role(
        "button", name="Remove provider", exact=True
    ).click()
    expect(row).to_have_count(0)
    page.reload()
    _open_codex(page)
    expect(page.get_by_test_id("agent-provider-row-ui-local-gateway")).to_have_count(0)
    assert "ui-local-gateway" not in _config(runtime, "host-a")["providers"]


def test_settings_persist_on_selected_host_and_route_new_sessions(
    page: Page,
    provider_runtime: ProviderSetupRuntime,
) -> None:
    try:
        _exercise_settings_persist_on_selected_host_and_route_new_sessions(page, provider_runtime)
    finally:
        httpx.post(
            f"http://127.0.0.1:{provider_runtime.mock_ports[0]}/control/release", timeout=5
        ).raise_for_status()
