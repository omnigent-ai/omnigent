"""E2E: an OpenCode host configured for AWS Bedrock must read as ready.

OpenCode reaches Amazon Bedrock through the AWS credential chain, not
``auth.json`` or a provider API key, so a host with only ``amazon-bedrock``
enabled and AWS credentials in its environment used to report
``opencode-native`` as ``needs-auth`` and the picker disabled its row. Like
``test_credentialless_host_harness_readiness.py`` this drives a REAL
``omnigent host`` daemon (isolated ``HOME``, placeholder AWS values that the
readiness probe never contacts) so the picker renders its actual readiness map.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests.e2e_ui.chat.test_credentialless_host_harness_readiness import (
    _REPO_ROOT,
    _fetch_host_row,
    _register_harness_agent,
    _seed_recent_workspace,
)

_HOST_ONLINE_TIMEOUT_S = 180.0
_HARNESS = "opencode-native"
_OPENCODE_AGENT_NAME = "opencode-native-ui"

_OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "enabled_providers": ["amazon-bedrock"],
    "model": "amazon-bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
}

_AWS_PLACEHOLDER_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIAPLACEHOLDER",
    "AWS_SECRET_ACCESS_KEY": "placeholder-secret-not-a-real-key",
    "AWS_REGION": "us-east-1",
}


@pytest.fixture(scope="module")
def bedrock_opencode_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` whose only OpenCode provider is Bedrock.

    Isolated ``HOME`` with the Bedrock-only ``opencode.json`` and placeholder AWS
    variables, no provider API keys. Yields the ``/v1/hosts`` row plus ``workspace``.
    """
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI not installed on this machine")
    tmp = tmp_path_factory.mktemp("bedrock_opencode_host")
    host_home = tmp / "home"
    config_path = host_home / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(_OPENCODE_CONFIG, indent=2) + "\n", encoding="utf-8")
    workspace = tmp / "workspace"
    workspace.mkdir()
    host_name = f"opencode-bedrock-{uuid.uuid4().hex[:8]}"

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(host_home),
        "PYTHONPATH": os.pathsep.join(
            [
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
            ]
        ),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "OMNIGENT_HOST_NAME": host_name,
        "OMNIGENT_HOST_ID": uuid.uuid4().hex,
        **_AWS_PLACEHOLDER_ENV,
    }
    log_path = tmp / "host.log"
    with log_path.open("w") as log_handle:
        argv = [sys.executable, "-m", "omnigent", "host", "--server", live_server]
        proc = subprocess.Popen(
            [*argv, "--non-interactive"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        row: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            row = _fetch_host_row(live_server, host_name)
            if row is not None:
                break
            if proc.poll() is not None:
                raise RuntimeError(
                    f"omnigent host exited early ({proc.returncode}):\n"
                    f"{log_path.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        if row is None:
            raise RuntimeError(
                f"Bedrock OpenCode host never came online:\n{log_path.read_text()[-2000:]}"
            )
        yield {**row, "workspace": str(workspace)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_bedrock_configured_opencode_reads_ready(
    live_server: str,
    bedrock_opencode_host: dict[str, Any],
) -> None:
    """The picker must not warn that OpenCode needs auth on a Bedrock-configured host."""
    _run_in_fresh_loop(_drive_opencode_readiness(live_server, bedrock_opencode_host))


async def _drive_opencode_readiness(base_url: str, host: dict[str, Any]) -> None:
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json().get("data", [])
    builtin = next((a for a in agents if a.get("name") == _OPENCODE_AGENT_NAME), None)
    agent_id = (
        str(builtin["id"])
        if builtin is not None
        else _register_harness_agent(
            base_url, f"opencode-bedrock-{uuid.uuid4().hex[:6]}", _HARNESS, "claude-sonnet-4-5"
        )
    )

    host_row = _fetch_host_row(base_url, host["name"])
    assert host_row is not None, "Bedrock OpenCode host dropped offline before the UI drive"
    readiness = (host_row.get("configured_harnesses") or {}).get(_HARNESS)
    assert readiness is True, (
        "host with OpenCode configured for amazon-bedrock must report opencode-native "
        f"ready, got {readiness!r}"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _seed_recent_workspace(page, host["host_id"], host["workspace"])
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await expect(page.get_by_role("menu").first).to_be_visible()
            # OpenCode is a secondary harness: its row lives behind the "Other..." flyout.
            more = page.get_by_test_id("new-chat-landing-harness-more")
            if await more.count():
                await more.hover()
            row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
            await expect(row).to_be_visible(timeout=10_000)
            await expect(row).to_be_enabled()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-warning-{agent_id}")
            ).to_have_count(0)
        finally:
            await page.close()
            await browser.close()
