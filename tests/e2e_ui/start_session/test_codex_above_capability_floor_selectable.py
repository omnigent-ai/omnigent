"""E2E: a signed-in Codex above the native harness's capability floor stays selectable.

Journey from the bug report: a user's machine has a working, signed-in ``codex``
whose version sits between the native harness's 0.129.0 policy-hook floor and a
newer release. They connect it as an ``omnigent host``, open the new-chat screen
on that host and choose Codex in the harness picker. The daemon's readiness map
drives the picker, so the test registers a REAL ``omnigent host`` daemon
(isolated ``HOME`` holding a Codex login, stub-first ``PATH``) against the live
e2e server rather than stubbing the wire body.

With a floor above the capability requirement the daemon reports the CLI
``version-too-low``: the Codex row is disabled with a warning badge and the
composer swaps the remembered agent away with an "outdated" notice. With the
capability floor the host reads ready, the row is enabled, choosing it keeps
Codex selected, and no notice appears.

The async-in-a-fresh-thread shape is inherited from
``start_session/test_start_session.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOST_ONLINE_TIMEOUT_S = 180.0
# Above the 0.129.0 policy-hook floor, below the 0.137.0 date-derived floor
# that once rejected it.
_STUB_CODEX_VERSION = "0.133.0"
# Long enough for a recording's sampled frames to catch each picker state.
_HOLD_MS = 3_000

_STUB_CODEX = f"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "codex-cli {_STUB_CODEX_VERSION}"; fi
exit 0
"""


def _fetch_host_row(base_url: str, host_name: str) -> dict[str, Any] | None:
    hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json().get("hosts", [])
    return next(
        (h for h in hosts if h.get("name") == host_name and h.get("status") == "online"),
        None,
    )


def _builtin_codex_agent(base_url: str) -> dict[str, Any]:
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json().get("data", [])
    agent = next(
        (a for a in agents if a.get("harness") == "codex-native" and a.get("builtin")), None
    )
    assert agent is not None, "the server exposes no built-in codex-native agent"
    return agent


@pytest.fixture(scope="module")
def signed_in_stub_codex_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` daemon whose ``codex`` is a signed-in CLI at 0.133.0.

    The stub answers ``--version`` like the real CLI and sits first on the
    daemon's PATH, ahead of any real codex. The daemon's isolated ``HOME``
    carries a Codex ``auth.json`` credential, so the only thing standing
    between this host and a ready Codex is the version floor.
    """
    tmp = tmp_path_factory.mktemp("signed_in_stub_codex_host")
    stub_bin = tmp / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "codex"
    stub.write_text(_STUB_CODEX, encoding="utf-8")
    stub.chmod(0o755)
    host_home = tmp / "home"
    codex_home = host_home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "codex-login-test-credential"}), encoding="utf-8"
    )
    host_name = f"codex-stub-{uuid.uuid4().hex[:8]}"

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
        "OMNIGENT_HOST_NO_OPEN": "1",
    }
    probe = subprocess.run(["codex", "--version"], env=env, capture_output=True, text=True)
    assert probe.returncode == 0 and _STUB_CODEX_VERSION in probe.stdout, probe

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
                f"stub-codex host never came online:\n{log_path.read_text()[-2000:]}"
            )
        yield row
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_signed_in_codex_above_capability_floor_stays_selectable(
    live_server: str,
    signed_in_stub_codex_host: dict[str, Any],
) -> None:
    """Codex at 0.133.0 with a login must read ready and be choosable on its host.

    The native harness only needs codex >= 0.129.0, so a working, signed-in CLI
    in that window must be reported ready, keep its picker row enabled, stay
    selected once chosen, and raise no outdated notice.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    evidence_dir = Path(record_dir).parent / "screenshots" if record_dir else None
    _run_in_fresh_loop(_drive_codex_picker(live_server, signed_in_stub_codex_host, evidence_dir))


async def _select_host(page: Any, host: dict[str, Any]) -> None:
    chip = page.get_by_test_id("new-chat-landing-host-chip")
    menu = page.get_by_test_id("new-chat-landing-host-menu")
    row = page.get_by_test_id(f"new-chat-landing-host-{host['host_id']}")
    await chip.click()
    await expect(menu).to_be_visible()
    await expect(row).to_be_enabled(timeout=15_000)
    await row.hover()
    await page.wait_for_timeout(_HOLD_MS)
    await row.click()
    await expect(menu).to_be_hidden()
    # The chip is icon-only, so confirm the pick through the menu's active row.
    await chip.click()
    await expect(row).to_have_attribute("data-active", "true", timeout=15_000)
    await page.wait_for_timeout(_HOLD_MS)
    await page.keyboard.press("Escape")
    await expect(menu).to_be_hidden()


async def _reveal_agent_row(page: Any, agent_id: str) -> Any:
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if await more.count() > 0:
            await more.hover()
            if await row.count() == 0:
                await more.click()
    await row.wait_for(state="visible", timeout=10_000)
    return row


async def _notice_text(page: Any, test_id: str, *, wait: bool) -> str | None:
    """Return a composer notice's text, waiting for it only when one is expected."""
    notice = page.get_by_test_id(test_id)
    if wait:
        try:
            await notice.wait_for(state="visible", timeout=10_000)
        except Exception:
            return None
    elif not await notice.is_visible():
        return None
    return (await notice.inner_text()).strip()


async def _drive_codex_picker(
    base_url: str, host: dict[str, Any], evidence_dir: Path | None
) -> None:
    agent = _builtin_codex_agent(base_url)
    readiness = (host.get("configured_harnesses") or {}).get("codex-native")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # The one-time import-review modal for a newly connected host is
            # unrelated to readiness and would intercept the picker clicks.
            await page.add_init_script(
                "window.localStorage.setItem("
                f'"omnigent:imports-reviewed:{host["host_id"]}", new Date().toISOString())'
            )
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _select_host(page, host)

            row = await _reveal_agent_row(page, agent["id"])
            row_enabled = await row.is_enabled()
            badge_count = await page.get_by_test_id(
                f"new-chat-landing-agent-warning-{agent['id']}"
            ).count()
            await row.hover()
            await page.wait_for_timeout(_HOLD_MS)
            if evidence_dir is not None:
                evidence_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(evidence_dir / "picker-codex-row.png"))

            codex_selected = False
            if row_enabled:
                await row.click()
                # The trigger is icon-led, so confirm the pick through the
                # picker's active row, then leave the composer settled.
                row = await _reveal_agent_row(page, agent["id"])
                codex_selected = (await row.get_attribute("data-active")) == "true"
                await page.wait_for_timeout(_HOLD_MS)
                await page.keyboard.press("Escape")
            else:
                # The row cannot be chosen; land on Codex the way a returning
                # user does, via the persisted last pick, so the composer's
                # verdict on it is captured in the failure.
                await page.keyboard.press("Escape")
                await page.add_init_script(
                    f'window.localStorage.setItem("omnigent:last-agent-id", "{agent["id"]}")'
                )
                await page.reload()
                await page.get_by_test_id("new-chat-landing-input").wait_for(
                    state="visible", timeout=30_000
                )

            warning = await _notice_text(
                page, "new-chat-landing-harness-warning", wait=not row_enabled
            )
            fallback = await _notice_text(
                page, "new-chat-landing-harness-fallback", wait=not row_enabled
            )
            trigger_warning_count = await page.get_by_test_id(
                "new-chat-landing-agent-warning"
            ).count()
            await page.wait_for_timeout(_HOLD_MS)
            if evidence_dir is not None:
                await page.screenshot(path=str(evidence_dir / "composer-codex-selected.png"))
        finally:
            await context.close()
            await browser.close()

    notices = " | ".join(n for n in (warning, fallback) if n)
    assert readiness is True, (
        f"host with signed-in codex-cli {_STUB_CODEX_VERSION} reports configured_harnesses"
        f"['codex-native'] == {readiness!r}; picker row enabled={row_enabled}, "
        f"badges={badge_count}, composer notice={notices!r}"
    )
    assert row_enabled and badge_count == 0, (
        f"Codex picker row must be selectable without a warning badge "
        f"(enabled={row_enabled}, badges={badge_count}); composer notice={notices!r}"
    )
    assert codex_selected, "choosing the Codex row must leave Codex as the active agent"
    assert not notices and trigger_warning_count == 0, (
        f"Codex must stay selected on {host['name']} without a readiness notice; "
        f"got notice={notices!r}, trigger warnings={trigger_warning_count}"
    )
