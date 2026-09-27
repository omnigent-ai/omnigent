"""E2E: a host whose ``agy`` login lives only in the OS keyring is ready for antigravity-native.

A wrapper ``agy`` (``agy models`` exits 0, no token file, no ``GEMINI_API_KEY``) stands in
for the keyring sign-in, which cannot run in CI; the host daemon and the web UI are real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from omnigent._platform import resolve_cli_binary
from tests.e2e_ui.conftest import _register_agent_yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HARNESS = "antigravity-native"
_HOST_ONLINE_TIMEOUT_S = 180.0
_PICKER_SETTLE_MS = 1_500

# The legacy token files older agy builds wrote; a keyring login writes neither.
_TOKEN_FILES = (
    Path(".gemini") / "oauth_creds.json",
    Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token",
)

_AGY_WRAPPER = """#!/usr/bin/env bash
# Signed-in stand-in: list models and exit 0 for `agy models`; run the real agy otherwise.
if [ "$1" = "models" ]; then
  printf 'gemini-3-pro\\ngemini-3-flash\\n'
  exit 0
fi
exec {real_agy} "$@"
"""


def _run_in_fresh_loop(coro: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """Run *coro* in a fresh thread; the main thread cannot host a new asyncio loop here."""
    captured: dict[str, Any] = {}

    def _worker() -> None:
        try:
            captured["result"] = asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]
    return captured["result"]


def _fetch_host_row(base_url: str, host_name: str) -> dict[str, Any] | None:
    hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json().get("hosts", [])
    return next(
        (h for h in hosts if h.get("name") == host_name and h.get("status") == "online"),
        None,
    )


@pytest.fixture(scope="module")
def keyring_agy_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` whose ``agy`` reports a login that no token file backs."""
    if sys.platform == "darwin":
        pytest.skip("macOS already asks the agy CLI; the reported gap is every other platform")
    real_agy = resolve_cli_binary("agy")
    if real_agy is None:
        pytest.skip("the 'agy' CLI is required to drive the readiness probe")

    tmp = tmp_path_factory.mktemp("keyring_agy_host")
    host_home = tmp / "home"
    host_home.mkdir()
    workspace = tmp / "workspace"
    workspace.mkdir()
    stub_bin = tmp / "bin"
    stub_bin.mkdir()
    wrapper = stub_bin / "agy"
    wrapper.write_text(_AGY_WRAPPER.format(real_agy=shlex.quote(real_agy)))
    wrapper.chmod(0o755)
    host_name = f"keyring-agy-{uuid.uuid4().hex[:8]}"

    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
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
    }

    models = subprocess.run(
        [str(wrapper), "models"], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert models.returncode == 0, f"the stand-in agy must report a signed-in CLI: {models.stderr}"
    version = subprocess.run(
        [str(wrapper), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert version.returncode == 0, f"agy --version failed through the wrapper: {version.stderr}"
    agy_version = version.stdout.strip()
    assert not any((host_home / rel).exists() for rel in _TOKEN_FILES)

    log_path = tmp / "host.log"
    with log_path.open("w") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
                "--no-open",
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        row: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            candidate = _fetch_host_row(live_server, host_name)
            if candidate is not None and _HARNESS in (candidate.get("configured_harnesses") or {}):
                row = candidate
                break
            if proc.poll() is not None:
                raise RuntimeError(
                    f"omnigent host exited early ({proc.returncode}):\n"
                    f"{log_path.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        if row is None:
            raise RuntimeError(
                "host never reported antigravity-native readiness:\n"
                f"{log_path.read_text()[-2000:]}"
            )
        availability = row["configured_harnesses"][_HARNESS]
        if availability in ("binary-missing", "version-too-low"):
            pytest.skip(f"agy {agy_version} does not pass the install gate ({availability!r})")
        yield {
            **row,
            "workspace": str(workspace),
            "agy_version": agy_version,
            "agy_models_exit": models.returncode,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _antigravity_agent_id(base_url: str) -> str:
    """The picker's Antigravity agent: the built-in one, or a registered stand-in."""
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json().get("data", [])
    builtin = next((a for a in agents if a.get("harness") == _HARNESS), None)
    if builtin is not None:
        return str(builtin["id"])
    agent_id = _register_agent_yaml(
        base_url,
        (
            "spec_version: 1\n"
            f"name: antigravity-keyring-{uuid.uuid4().hex[:6]}\n"
            "prompt: You are a test agent.\n"
            "executor:\n"
            "  config:\n"
            f"    harness: {_HARNESS}\n"
        ),
    )
    assert agent_id is not None, "antigravity-native agent failed to register"
    return agent_id


async def _select_host(page: Any, host_id: str) -> None:
    await page.get_by_test_id("new-chat-landing-host-chip").click()
    option = page.get_by_test_id(f"new-chat-landing-host-{host_id}")
    await expect(option).to_be_visible(timeout=15_000)
    await option.click()
    await expect(page.locator('[data-slot="dropdown-menu-content"]')).to_have_count(0)


async def _reveal_agent_row(page: Any, agent_id: str) -> Any:
    """Open the landing picker's agent menu and reveal *agent_id*'s row without clicking it."""
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        custom = page.get_by_test_id("new-chat-landing-custom-agents")
        if await custom.count() > 0:
            await custom.hover()
            with contextlib.suppress(Exception):
                await row.wait_for(state="visible", timeout=5_000)
    if await row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if await more.count() > 0:
            await more.click()
    await row.wait_for(state="visible", timeout=10_000)
    return row


async def _drive_picker(base_url: str, host: dict[str, Any], agent_id: str) -> dict[str, Any]:
    observed: dict[str, Any] = {"badge_count": None, "row_disabled": None, "tooltip": None}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        try:
            await page.add_init_script(
                "window.localStorage.setItem("
                '"omnigent:recent-workspaces", '
                f"JSON.stringify({json.dumps({host['host_id']: [host['workspace']]})}))"
            )
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _select_host(page, host["host_id"])

            row = await _reveal_agent_row(page, agent_id)
            await row.hover()
            await page.wait_for_timeout(_PICKER_SETTLE_MS)
            observed["badge_count"] = await page.get_by_test_id(
                f"new-chat-landing-agent-warning-{agent_id}"
            ).count()
            observed["row_disabled"] = (
                await row.get_attribute("aria-disabled") == "true"
                or await row.get_attribute("data-disabled") is not None
            )
            tooltip = page.get_by_test_id(f"new-chat-landing-agent-tooltip-{agent_id}")
            if await tooltip.count() > 0:
                observed["tooltip"] = " ".join((await tooltip.first.inner_text()).split())
            record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
            if record_dir:
                await page.screenshot(path=str(Path(record_dir) / "antigravity-picker.png"))
            await page.wait_for_timeout(_PICKER_SETTLE_MS)
        finally:
            await context.close()
            await browser.close()
    return observed


def test_keyring_login_is_ready_for_antigravity_native(
    live_server: str,
    keyring_agy_host: dict[str, Any],
) -> None:
    """A keyring-only agy login reads as ready: host row True, picker row enabled, no badge."""
    agent_id = _antigravity_agent_id(live_server)
    observed = _run_in_fresh_loop(_drive_picker(live_server, keyring_agy_host, agent_id))

    row = _fetch_host_row(live_server, keyring_agy_host["name"])
    assert row is not None, "keyring-login host dropped offline mid-test"
    availability = (row.get("configured_harnesses") or {}).get(_HARNESS)
    detail = (
        f"agy {keyring_agy_host['agy_version']} on {sys.platform}, `agy models` exit "
        f"{keyring_agy_host['agy_models_exit']}, no GEMINI_API_KEY, no token file; the picker "
        f"showed badge_count={observed['badge_count']}, row_disabled={observed['row_disabled']}, "
        f"tooltip={observed['tooltip']!r}"
    )
    assert availability is True, (
        f"host must report {_HARNESS} ready for a keyring-only agy login, got "
        f"{availability!r} ({detail})"
    )
    assert observed["badge_count"] == 0 and observed["row_disabled"] is False, (
        f"the Antigravity picker row must be selectable without a warning badge ({detail})"
    )
