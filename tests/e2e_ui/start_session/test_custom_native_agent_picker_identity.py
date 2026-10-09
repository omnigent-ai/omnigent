"""E2E: custom claude-native agents registered at startup keep their own New Chat picker rows.

Boots a real ``omnigent server`` with ``--agent autoresearch.yaml`` (a user
template) and ``OMNIGENT_BUILTIN_AGENT_DIRS=teamresearch.yaml`` (a seeded
built-in), plus a real ``omnigent host`` so the landing picker is enabled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.live_server import terminate_process
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _find_free_port,
)

TEMPLATE_AGENT_NAME = "autoresearch"
SEEDED_AGENT_NAME = "teamresearch"
STOCK_AGENT_NAME = "claude-native-ui"
STOCK_LABEL = "Claude Code"

_HOST_ONLINE_TIMEOUT_S = 120.0


def _agent_yaml(name: str) -> str:
    return (
        f"name: {name}\n"
        f"description: {name} wraps Claude Code.\n"
        f"prompt: You are {name}, a research agent.\n"
        "\n"
        "executor:\n"
        "  harness: claude-native\n"
    )


@dataclass
class ServerRig:
    proc: subprocess.Popen[bytes]
    base_url: str
    log_path: Path


def spawn_server(server_tmp: Path) -> ServerRig:
    """Start ``omnigent server --agent autoresearch.yaml`` with ``teamresearch`` seeded by env."""
    template_path = server_tmp / f"{TEMPLATE_AGENT_NAME}.yaml"
    template_path.write_text(_agent_yaml(TEMPLATE_AGENT_NAME))
    seeded_path = server_tmp / f"{SEEDED_AGENT_NAME}.yaml"
    seeded_path.write_text(_agent_yaml(SEEDED_AGENT_NAME))
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = server_tmp / "server.log"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(seeded_path),
    }
    apply_server_env(env, _REPO_ROOT)
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [
                server_executable(),
                "-c",
                "from omnigent.cli import main; main()",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{server_tmp / 'test.db'}",
                "--artifact-location",
                str(artifact_dir),
                "--agent",
                str(template_path),
            ],
            env=env,
            cwd=compat_server_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"server exited early with code {proc.returncode}"
            break
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return ServerRig(proc, base_url, log_path)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    terminate_process(proc)
    raise RuntimeError(
        f"omnigent server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on {base_url} "
        f"({last_error}).\n{log_path.read_text()[-3000:]}"
    )


@dataclass
class HostRig:
    proc: subprocess.Popen[bytes]
    host: dict[str, object]
    log_path: Path


def spawn_host(base_url: str, host_tmp: Path) -> HostRig:
    """Register this machine on *base_url* with a real ``omnigent host`` and wait until online."""
    claude_dir = host_tmp / "claude-config"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_CONFIG_HOME": str(host_tmp / "config"),
        "CLAUDE_CONFIG_DIR": str(claude_dir),
        # A gateway-style token makes the host report claude-native as configured;
        # no session is ever launched.
        "ANTHROPIC_AUTH_TOKEN": "mock-token",
        "PYTHONPATH": os.pathsep.join(
            p
            for p in [
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
                os.environ.get("PYTHONPATH", ""),
            ]
            if p
        ),
    }
    log_path = host_tmp / "host.log"
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                base_url,
                "--non-interactive",
                "--no-open",
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

    deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"omnigent host exited with {proc.returncode}: {log_path.read_text()[-3000:]}"
            )
        try:
            hosts = httpx.get(f"{base_url}/v1/hosts", timeout=5.0).json().get("hosts", [])
        except (httpx.HTTPError, ValueError):
            hosts = []
        online = [h for h in hosts if h.get("status") == "online"]
        if online:
            return HostRig(proc, online[0], log_path)
        time.sleep(1.0)
    terminate_process(proc)
    raise RuntimeError(f"omnigent host never came online: {log_path.read_text()[-3000:]}")


def catalog_rows(base_url: str) -> dict[str, dict[str, object]]:
    """The live ``GET /v1/agents`` catalog keyed by agent name."""
    resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=10.0)
    resp.raise_for_status()
    return {row["name"]: row for row in resp.json()["data"]}


@pytest.fixture(scope="module")
def custom_native_agent_server(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ServerRig]:
    rig = spawn_server(tmp_path_factory.mktemp("custom_native_agents"))
    try:
        yield rig
    finally:
        terminate_process(rig.proc)


@pytest.fixture(scope="module")
def registered_host(
    custom_native_agent_server: ServerRig, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[HostRig]:
    rig = spawn_host(
        custom_native_agent_server.base_url, tmp_path_factory.mktemp("custom_native_agents_host")
    )
    try:
        yield rig
    finally:
        terminate_process(rig.proc)


def open_landing_picker(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    expect(trigger).to_be_visible(timeout=30_000)
    expect(trigger).to_be_enabled(timeout=60_000)
    trigger.click()
    expect(page.get_by_text("Harnesses", exact=True)).to_be_visible(timeout=30_000)


def reveal_row(page: Page, agent_id: str) -> Locator:
    """The picker row for *agent_id*, opening the Other... submenus when it is not inline."""
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    # count() alone matches an attached-but-hidden row; require visibility so a
    # row still inside a closed submenu falls through to the hover path below.
    if row.count() > 0 and row.is_visible():
        return row
    for submenu in ("new-chat-landing-harness-more", "new-chat-landing-custom-agents"):
        trigger = page.get_by_test_id(submenu)
        if trigger.count() == 0:
            continue
        trigger.hover()
        try:
            row.wait_for(state="visible", timeout=4_000)
            return row
        except PlaywrightTimeoutError:
            continue
    raise AssertionError(
        f"picker row for agent {agent_id} not found inline or in either Other... submenu"
    )


def test_custom_native_agents_keep_their_own_picker_rows(
    request: pytest.FixtureRequest,
    custom_native_agent_server: ServerRig,
    registered_host: HostRig,
) -> None:
    """Both startup-registered claude-native agents get their own row, named by their own name."""
    base_url = custom_native_agent_server.base_url
    rows = catalog_rows(base_url)
    expected = {TEMPLATE_AGENT_NAME, SEEDED_AGENT_NAME, STOCK_AGENT_NAME}
    missing = expected - rows.keys()
    assert not missing, f"agents {sorted(missing)} missing from catalog: {sorted(rows)}"
    # The current server always serializes 'builtin'; a missing flag is the
    # catalog regression this test guards, so fail loudly rather than skip.
    assert all("builtin" in rows[name] for name in expected), (
        f"catalog omitted the 'builtin' flag this identity check depends on: {sorted(rows)}"
    )
    template, seeded, stock = (
        rows[TEMPLATE_AGENT_NAME],
        rows[SEEDED_AGENT_NAME],
        rows[STOCK_AGENT_NAME],
    )
    assert template["harness"] == seeded["harness"] == "claude-native"
    assert template["builtin"] is False
    assert seeded["builtin"] is True
    assert stock["builtin"] is True

    page: Page = request.getfixturevalue("page")
    open_landing_picker(page, base_url)
    stock_row = page.get_by_test_id(f"new-chat-landing-agent-{stock['id']}")
    expect(stock_row).to_be_visible(timeout=30_000)
    expect(stock_row).to_contain_text(STOCK_LABEL)

    template_row = reveal_row(page, str(template["id"]))
    expect(template_row).to_be_visible(timeout=10_000)
    expect(template_row).to_contain_text(re.compile(TEMPLATE_AGENT_NAME, re.IGNORECASE))
    seeded_row = reveal_row(page, str(seeded["id"]))
    expect(seeded_row).to_be_visible(timeout=10_000)
    expect(seeded_row).to_contain_text(re.compile(SEEDED_AGENT_NAME, re.IGNORECASE))
    expect(page.get_by_role("menuitem", name=STOCK_LABEL, exact=True)).to_have_count(1)
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        # Hold the resolved picker so a recording ends on the three distinct rows.
        page.wait_for_timeout(2_000)
