"""Configuration made on one of a user's machines is expected on their other machines."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.host import Host, isolated_host

MACHINE_A_MODEL = "mock-model-from-machine-a"
MACHINE_A_CODEX_ARGS = ["--profile", "machine-a"]


def _mock_providers(mock_llm_server_url: str) -> dict[str, object]:
    return {
        "e2e-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": mock_llm_server_url,
                "api_key": "mock-key",
                "models": {"default": "claude-sonnet-4-20250514"},
            },
        },
        "e2e-openai": {
            "kind": "key",
            "default": ["openai"],
            "openai": {
                "base_url": f"{mock_llm_server_url}/v1",
                "api_key": "mock-key",
                "wire_api": "responses",
                "models": {"default": "gpt-4o"},
            },
        },
    }


def _wait_for_config(
    machine: Host, predicate: Callable[[dict[str, object]], bool], *, timeout: float = 30
) -> None:
    """Settings arrive shortly after a host connects; wait for them instead of racing them."""
    path = machine.home / ".omnigent" / "config.yaml"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and predicate(yaml.safe_load(path.read_text()) or {}):
            return
        time.sleep(0.5)
    raise AssertionError(f"{machine.name}: config.yaml never received the synced settings")


def _config_list(machine: Host, cwd: Path) -> str:
    return _omnigent(machine, cwd, "config", "list")


def _omnigent(machine: Host, cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "HOME": str(machine.home),
        "OMNIGENT_CONFIG_HOME": str(machine.home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(machine.home / ".omnigent"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "omnigent", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _codex_startup_arguments(
    page: Page, base_url: str, machine: Host, screenshot: Path
) -> tuple[list[str], str]:
    page.goto(f"{base_url}/settings/harnesses")
    page.get_by_test_id("harness-host-select").click()
    page.get_by_test_id(f"harness-host-{machine.host_id}").click()
    expect(page.get_by_test_id("harness-host-select")).to_contain_text(machine.name)
    page.get_by_test_id("harness-settings-codex-native").click()
    expect(page.get_by_role("heading", name="Startup configuration")).to_be_visible(timeout=30_000)
    arguments = page.get_by_role("heading", name="Arguments").locator(
        "xpath=following-sibling::*[1]"
    )
    arguments.scroll_into_view_if_needed()
    expect(arguments).to_be_visible()
    page.wait_for_timeout(2_000)
    page.screenshot(path=screenshot, full_page=True)
    return arguments.locator("li code").all_inner_texts(), arguments.inner_text()


def test_codex_startup_setting_made_on_one_machine_is_shown_for_the_users_other_machine(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    providers = _mock_providers(mock_llm_server_url)
    with (
        httpx.Client(base_url=live_server, timeout=30) as client,
        isolated_host(
            tmp_path / "machine-a",
            live_server,
            name="machine-a",
            config_entries={
                "providers": providers,
                "model": MACHINE_A_MODEL,
                "harness": {"codex-native": {"args": MACHINE_A_CODEX_ARGS}},
            },
        ) as machine_a,
        isolated_host(
            tmp_path / "machine-b",
            live_server,
            name="machine-b",
            config_entries={"providers": providers},
        ) as machine_b,
    ):
        machine_a.wait_online(client, timeout=120)
        machine_b.wait_online(client, timeout=120)
        _wait_for_config(machine_b, lambda cfg: cfg.get("model") == MACHINE_A_MODEL)
        defaults_a = _config_list(machine_a, tmp_path)
        defaults_b = _config_list(machine_b, tmp_path)
        assert f"model={MACHINE_A_MODEL}" in defaults_a, defaults_a

        page = request.getfixturevalue("page")
        args_a, shown_a = _codex_startup_arguments(
            page, live_server, machine_a, output / "machine-a-codex-startup.png"
        )
        assert args_a == MACHINE_A_CODEX_ARGS, shown_a

        args_b, shown_b = _codex_startup_arguments(
            page, live_server, machine_b, output / "machine-b-codex-startup.png"
        )
        assert args_b == MACHINE_A_CODEX_ARGS and f"model={MACHINE_A_MODEL}" in defaults_b, (
            "Configuration set on machine-a did not follow the user to machine-b.\n"
            f"machine-b Codex startup arguments shown in Settings: {shown_b!r}\n"
            f"machine-b `omnigent config list`:\n{defaults_b}"
        )

        # A change made while both machines stay connected follows the user too.
        _omnigent(machine_a, tmp_path, "config", "set", "--global", "model=changed-on-machine-a")
        _wait_for_config(machine_b, lambda cfg: cfg.get("model") == "changed-on-machine-a")
        assert "model=changed-on-machine-a" in _config_list(machine_b, tmp_path)
