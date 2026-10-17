"""Sandbox model previews offer gateway models when no inference binding is configured."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import _BUILD_OUTPUT, _HEALTH_TIMEOUT_S, _REPO_ROOT, _find_free_port

SANDBOX_PROVIDER = "islo"
SANDBOX_LABEL = "Islo Sandbox"
GATEWAY_PROVIDER = "team_gateway"
GATEWAY_LABEL = "Team AI Gateway"
GATEWAY_MODELS = ("gpt-5.5", "gpt-5.5-mini")
CODEX_AGENT_NAME = "codex-native-ui"
# The sandbox resolves its key inside its own environment; only the discovery
# key is resolved by the server when it lists the gateway's models.
_SANDBOX_KEY_ENV = "SANDBOX_GATEWAY_KEY"
_DISCOVERY_KEY_ENV = "GATEWAY_DISCOVERY_KEY"
_LEAKED_ENV_PREFIXES = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_LEAKED_ENV_KEYS = frozenset({"RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN"})


def sandbox_server_config(gateway_url: str) -> dict[str, object]:
    """Server YAML: a managed provider whose Codex is gateway-backed, with no inference binding."""
    return {
        "sandbox": {
            "provider": SANDBOX_PROVIDER,
            # Never dialled: nothing is provisioned before the picker is read.
            "server_url": "http://127.0.0.1:1",
            "host_config": {
                "providers": {
                    GATEWAY_PROVIDER: {
                        "kind": "gateway",
                        "display_name": GATEWAY_LABEL,
                        "openai": {
                            "base_url": f"{gateway_url}/v1",
                            "api_key_ref": f"env:{_SANDBOX_KEY_ENV}",
                            "wire_api": "responses",
                        },
                    }
                }
            },
            "model_discovery": {
                GATEWAY_PROVIDER: {
                    "base_url": f"{gateway_url}/v1",
                    "api_key_ref": f"env:{_DISCOVERY_KEY_ENV}",
                }
            },
        }
    }


def spawn_sandbox_server(
    workdir: Path, gateway_url: str
) -> tuple[subprocess.Popen[bytes], str, IO[str]]:
    """Start ``omnigent server`` with :func:`sandbox_server_config`; no runner or host attaches."""
    config_path = workdir / "server.yaml"
    config_path.write_text(yaml.safe_dump(sandbox_server_config(gateway_url)))
    config_home = workdir / "config-home"
    artifacts = workdir / "artifacts"
    config_home.mkdir()
    artifacts.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_LEAKED_ENV_PREFIXES) and key not in _LEAKED_ENV_KEYS
    }
    env.update(
        {
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
            "OPENAI_BASE_URL": f"{gateway_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
            _DISCOVERY_KEY_ENV: "discovery-test-secret",
        }
    )
    apply_server_env(env, _REPO_ROOT)
    port = _find_free_port()
    log_handle = open(workdir / "server.log", "w")  # noqa: SIM115 - closed by the fixture
    try:
        proc = subprocess.Popen(
            [
                server_executable(),
                "-m",
                "omnigent",
                "server",
                "-c",
                str(config_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{workdir / 'server.db'}",
                "--artifact-location",
                str(artifacts),
            ],
            env=env,
            cwd=compat_server_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    except OSError:
        log_handle.close()
        raise
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                return proc, base_url, log_handle
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    stop_server(proc)
    log_handle.close()
    raise RuntimeError(
        f"sandbox server did not become healthy:\n{(workdir / 'server.log').read_text()[-2000:]}"
    )


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def agent_id_for(base_url: str, name: str) -> str:
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    ids = [agent["id"] for agent in agents if agent["name"] == name]
    assert ids, f"agent {name!r} not offered; found {[agent['name'] for agent in agents]}"
    return ids[0]


@pytest.fixture(scope="module")
def gateway_sandbox_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """A hostless server offering the Islo sandbox; the mock LLM is the ambient gateway."""
    workdir = tmp_path_factory.mktemp("gateway_sandbox_server")
    proc, base_url, log_handle = spawn_sandbox_server(workdir, mock_llm_server_url)
    try:
        httpx.post(
            f"{mock_llm_server_url}/mock/served_models",
            json={"models": list(GATEWAY_MODELS)},
            timeout=10.0,
        ).raise_for_status()
        yield base_url
    finally:
        # The mock LLM server is session-scoped: leave no gateway listing behind.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(
                f"{mock_llm_server_url}/mock/served_models", json={"models": []}, timeout=10.0
            )
        stop_server(proc)
        log_handle.close()


def _open_harness_picker(page: Page) -> None:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    # The picker reloads (disabled, aria-busy) after the host changes, and the
    # previous menu's dismissal layer swallows the next click until it unmounts.
    expect(picker).to_be_enabled(timeout=30_000)
    expect(picker).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)
    picker.click()
    expect(picker).to_have_attribute("aria-expanded", "true")


def _select_codex(page: Page, agent_id: str) -> None:
    _open_harness_picker(page)
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if row.count() == 0:
        page.get_by_test_id("new-chat-landing-harness-more").click()
    expect(row).to_be_visible()
    row.click()
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    if picker.get_attribute("aria-expanded") == "true":
        page.keyboard.press("Escape")
    expect(picker).to_have_attribute("aria-expanded", "false")
    expect(picker).to_have_attribute("aria-label", re.compile("Codex"))


def _open_selected_models(page: Page, agent_id: str) -> None:
    _open_harness_picker(page)
    edit = page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
    edit.hover()
    edit.click()
    expect(page.get_by_test_id("new-chat-landing-agent-models")).to_be_visible()


def test_sandbox_codex_picker_offers_gateway_models_before_a_host_exists(
    request: pytest.FixtureRequest, gateway_sandbox_server: str
) -> None:
    base_url = gateway_sandbox_server
    codex_id = agent_id_for(base_url, CODEX_AGENT_NAME)
    assert httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json()["hosts"] == []

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)

    page.get_by_test_id("new-chat-landing-host-chip").click()
    sandbox_row = page.get_by_test_id("new-chat-landing-sandbox-option")
    expect(sandbox_row).to_contain_text(SANDBOX_LABEL)
    sandbox_row.click()
    expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
        "aria-label", re.compile(SANDBOX_LABEL)
    )

    _select_codex(page, codex_id)
    _open_selected_models(page, codex_id)
    for model in GATEWAY_MODELS:
        expect(page.get_by_role("menuitemcheckbox", name=model, exact=True)).to_be_visible(
            timeout=15_000
        )
    chosen = page.get_by_role("menuitemcheckbox", name=GATEWAY_MODELS[1], exact=True)
    chosen.click()
    expect(chosen).to_have_attribute("aria-checked", "true")
