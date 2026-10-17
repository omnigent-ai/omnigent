"""E2E: a fresh managed-sandbox host (arclet) offers Pi with no manual setup.
Real ``omnigent host`` daemons drive the picker's readiness; ``pi`` is off their
PATH and ``npm`` is an offline stand-in that links this runner's own ``pi``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Page, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests._helpers.browser import async_user_page
from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests.e2e_ui.chat.test_credentialless_host_harness_readiness import (
    _dismiss_agent_menu,
    _register_harness_agent,
    _reveal_agent_row,
    _seed_recent_workspace,
)

_HOST_ONLINE_TIMEOUT_S = 180.0
_READINESS_PUBLISH_TIMEOUT_S = 150.0
_PI_PREPARE_TIMEOUT_S = 90.0
_RUNNER_ONLINE_TIMEOUT_S = 120.0
# Two independent fresh hosts verify that installation does not rely on another
# host's state.
_FRESH_HOST_COUNT = 2
_PI_PACKAGE = "@earendil-works/pi-coding-agent"


def path_without_pi(root: Path) -> str:
    """Keep ``node`` and the other harness CLIs reachable while ``pi`` resolves nowhere;
    the linked ``pi`` still needs ``node`` even when both share an install directory."""
    keep = root / "bin"
    keep.mkdir()
    for name in ("claude", "codex", "node"):
        found = shutil.which(name)
        if found:
            (keep / name).symlink_to(found)
    entries = [
        entry
        for entry in os.environ["PATH"].split(os.pathsep)
        if entry and shutil.which("pi", path=entry) is None
    ]
    return os.pathsep.join([str(keep), *entries])


def install_offline_npm(bin_dir: Path, real_pi: str) -> None:
    """Put an ``npm`` stand-in first on *bin_dir*: ``install -g <pi package>`` links
    *real_pi* into ``$HOME/.npm-global/bin`` (a global dir the daemon resolves CLIs
    from), every call is logged to ``$HOME/npm-invocations.log``, anything else fails."""
    script = bin_dir / "npm"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$HOME/npm-invocations.log"\n'
        'case "$*" in\n'
        f'  "install -g {_PI_PACKAGE}")\n'
        '    mkdir -p "$HOME/.npm-global/bin" || exit 1\n'
        f'    ln -sf {shlex.quote(real_pi)} "$HOME/.npm-global/bin/pi" ;;\n'
        '  *) echo "offline npm stand-in: unsupported arguments: $*" >&2; exit 1 ;;\n'
        "esac\n"
    )
    script.chmod(0o755)


def host_base_env(path: str) -> dict[str, str]:
    """Explicit base env (no ambient credentials) retaining *path*. ``IS_SANDBOX=1``
    marks the daemon as a managed sandbox: the host image bakes it in, a laptop
    never sets it, and it scopes the automatic Pi preparation to arclets."""
    return {
        "PATH": path,
        "IS_SANDBOX": "1",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


def host_command(base_url: str) -> list[str]:
    return [
        runner_executable(),
        "-m",
        "omnigent",
        "host",
        "--server",
        base_url,
        "--non-interactive",
    ]


def pi_provider_config(mock_url: str) -> dict[str, Any]:
    """A ``providers`` block routing Pi's Anthropic surface at the mock model."""
    return {
        "providers": {
            "mock": {
                "kind": "key",
                "default": ["anthropic", "pi"],
                "anthropic": {
                    "base_url": mock_url,
                    "api_key": "local-e2e-test-key",
                    "models": {"default": "claude-sonnet-4-5"},
                },
            }
        }
    }


def host_row(client: httpx.Client, host_id: str) -> dict[str, Any] | None:
    """The host's ``/v1/hosts`` row; ``None`` when absent or momentarily unreadable."""
    try:
        rows = client.get("/v1/hosts", timeout=10.0).json().get("hosts", [])
    except (httpx.HTTPError, ValueError):
        return None
    return next((row for row in rows if row.get("host_id") == host_id), None)


def wait_readiness_published(
    client: httpx.Client, host_id: str, *, timeout: float
) -> dict[str, Any]:
    """Return the host row once its daemon has published a readiness map (a slow
    startup probe registers with readiness unknown and publishes shortly after)."""
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = host_row(client, host_id)
        if row and row.get("configured_harnesses"):
            return row
        time.sleep(1.0)
    raise AssertionError(f"host {host_id} never published configured_harnesses; last row: {row!r}")


def wait_pi_native_ready(client: httpx.Client, host_id: str, *, timeout: float) -> bool:
    """Poll until the host reports ``pi-native`` ready; ``False`` on timeout. The
    caller still asserts the user-visible outcome, so an unfixed host simply falls
    through to the disabled picker row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = host_row(client, host_id) or {}
        if (row.get("configured_harnesses") or {}).get("pi-native") is True:
            return True
        time.sleep(1.0)
    return False


def wait_runner_online(client: httpx.Client, session_id: str, *, timeout: float) -> None:
    """Wait until the host has launched the session's runner and it registered."""
    deadline = time.monotonic() + timeout
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError, ValueError):  # transient; keep polling
            snapshot = client.get(f"/v1/sessions/{session_id}", timeout=10.0).json()
        if snapshot.get("runner_online") is True:
            return
        time.sleep(1.0)
    try:
        items = client.get(f"/v1/sessions/{session_id}/items", timeout=10.0).json()
        errors = [
            item.get("message") for item in items.get("data", []) if item.get("type") == "error"
        ]
    except Exception as exc:  # diagnostics only; keep the primary assertion
        errors = [f"<failed to fetch items: {exc}>"]
    raise AssertionError(
        f"runner for session {session_id} never came online; runner_online="
        f"{snapshot.get('runner_online')!r} host_online={snapshot.get('host_online')!r} "
        f"errors={errors!r}"
    )


@pytest.fixture(scope="module")
def fresh_hosts_without_pi(
    live_server: str, mock_llm_server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[list[dict[str, Any]]]:
    """Two real ``omnigent host`` daemons that have never had Pi installed: isolated
    HOME with a Pi provider block, no ambient keys, ``pi`` off PATH, offline ``npm``.
    Yields each ``/v1/hosts`` row plus its ``home`` and a ``workspace`` directory."""
    real_pi = shutil.which("pi")
    if real_pi is None:
        pytest.skip("pi CLI not installed on this runner")
    if shutil.which("node") is None:
        pytest.skip("node not installed on this runner; the linked pi needs it")
    root = tmp_path_factory.mktemp("fresh_hosts_without_pi")
    base_env = host_base_env(path_without_pi(root))
    install_offline_npm(root / "bin", real_pi)
    with (
        contextlib.ExitStack() as stack,
        httpx.Client(base_url=live_server, trust_env=False) as client,
    ):
        hosts = []
        for index in range(_FRESH_HOST_COUNT):
            host_root = root / f"host{index}"
            workspace = host_root / "workspace"
            workspace.mkdir(parents=True)
            host = stack.enter_context(
                isolated_host(
                    host_root,
                    live_server,
                    base_env=base_env,
                    name=f"fresh-{index}-{uuid.uuid4().hex[:8]}",
                    command=host_command(live_server),
                    config=pi_provider_config(mock_llm_server_url),
                )
            )
            hosts.append((host, workspace))
        rows = []
        for host, workspace in hosts:
            host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
            row = wait_readiness_published(
                client, host.host_id, timeout=_READINESS_PUBLISH_TIMEOUT_S
            )
            rows.append({**row, "workspace": str(workspace), "home": str(host.home)})
        yield rows


async def select_host(page: Page, host_id: str) -> None:
    """Pick *host_id* from the landing page's host chip."""
    await page.get_by_test_id("new-chat-landing-host-chip").click()
    entry = page.get_by_test_id(f"new-chat-landing-host-{host_id}")
    await expect(entry).to_be_visible(timeout=15_000)
    await entry.click()
    # Wait for the host menu to unmount so it cannot steal focus from the picker.
    await expect(page.locator('[data-slot="dropdown-menu-content"]')).to_have_count(0)


def _setup_still_required(
    base_url: str, host: dict[str, Any], agent_id: str, badge: str | None
) -> str:
    with httpx.Client(base_url=base_url, trust_env=False) as client:
        row = host_row(client, host["host_id"]) or {}
        readiness = (row.get("configured_harnesses") or {}).get("pi-native")
        response = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host["host_id"],
                "workspace": host["workspace"],
            },
            timeout=30.0,
        )
        if response.is_success:
            with contextlib.suppress(Exception):
                client.delete(f"/v1/sessions/{response.json()['id']}", timeout=10.0)
            verdict = f"{response.status_code} (session created)"
        else:
            try:
                error = response.json().get("error") or {}
            except ValueError:
                error = {"code": "non-JSON body", "message": response.text[:200]}
            verdict = f"{response.status_code} {error.get('code')}: {error.get('message')}"
    return (
        f"Pi is not usable on fresh host {host['name']!r} without manual setup: the picker "
        f"row is disabled (badge {badge!r}), the host reports pi-native={readiness!r}, and "
        f"POST /v1/sessions -> {verdict}"
    )


def _assert_pi_installed_by_host(base_url: str, host: dict[str, Any]) -> None:
    """The daemon, not a person, ran the installer and now reports Pi ready."""
    home = Path(host["home"])
    installed = home / ".npm-global" / "bin" / "pi"
    log = home / "npm-invocations.log"
    assert installed.exists(), f"host {host['name']!r} never installed pi: {installed} is absent"
    invocations = log.read_text() if log.exists() else ""
    assert f"install -g {_PI_PACKAGE}" in invocations, (
        f"host {host['name']!r} did not run the pi installer; npm saw: {invocations!r}"
    )
    with httpx.Client(base_url=base_url, trust_env=False) as client:
        row = host_row(client, host["host_id"]) or {}
    assert (row.get("configured_harnesses") or {}).get("pi-native") is True, (
        f"host {host['name']!r} reports pi-native="
        f"{(row.get('configured_harnesses') or {}).get('pi-native')!r} after installing pi"
    )


def _delete_sessions(base_url: str, session_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, trust_env=False) as client:
        for session_id in session_ids:
            with contextlib.suppress(Exception):
                client.delete(f"/v1/sessions/{session_id}", timeout=10.0)


async def _drive(base_url: str, hosts: list[dict[str, Any]], agent_id: str) -> None:
    created: list[str] = []
    try:
        async with async_playwright() as pw, await pw.chromium.launch() as browser:
            for host in hosts:
                # The picker reads readiness at render, so wait for the host's Pi
                # flip first; on an unfixed host this times out and the row stays disabled.
                with httpx.Client(base_url=base_url, trust_env=False) as client:
                    await asyncio.to_thread(
                        wait_pi_native_ready,
                        client,
                        host["host_id"],
                        timeout=_PI_PREPARE_TIMEOUT_S,
                    )
                async with async_user_page(
                    browser, headers={}, viewport={"width": 1280, "height": 800}
                ) as page:
                    await _seed_recent_workspace(page, host["host_id"], host["workspace"])
                    await page.goto(f"{base_url}/")
                    await page.get_by_test_id("new-chat-landing-input").wait_for(
                        state="visible", timeout=30_000
                    )
                    await select_host(page, host["host_id"])
                    row = await _reveal_agent_row(page, agent_id)
                    if not await row.is_enabled():
                        badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{agent_id}")
                        label = (
                            await badge.get_attribute("aria-label")
                            if await badge.count()
                            else None
                        )
                        pytest.fail(
                            await asyncio.to_thread(
                                _setup_still_required, base_url, host, agent_id, label
                            )
                        )
                    await row.click()
                    await _dismiss_agent_menu(page)
                    await expect(
                        page.get_by_test_id("new-chat-landing-harness-warning")
                    ).to_have_count(0)
                    await page.get_by_test_id("new-chat-landing-input").fill("Reply with READY.")
                    await page.get_by_test_id("new-chat-landing-submit").click()
                    await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
                    match = re.search(r"/c/([0-9a-f]+)", page.url)
                    assert match is not None, page.url
                    created.append(match.group(1))
                    with httpx.Client(base_url=base_url, trust_env=False) as client:
                        await asyncio.to_thread(
                            wait_runner_online,
                            client,
                            match.group(1),
                            timeout=_RUNNER_ONLINE_TIMEOUT_S,
                        )
                    await asyncio.to_thread(_assert_pi_installed_by_host, base_url, host)
    finally:
        await asyncio.to_thread(_delete_sessions, base_url, created)


def test_fresh_host_offers_pi_without_manual_setup(
    live_server: str, fresh_hosts_without_pi: list[dict[str, Any]]
) -> None:
    """Pi is selectable and a session starts on each fresh host nobody set up."""
    agent_id = _register_harness_agent(
        live_server, f"fresh-pi-{uuid.uuid4().hex[:6]}", "pi-native", "claude-sonnet-4-5"
    )
    _run_in_fresh_loop(_drive(live_server, fresh_hosts_without_pi, agent_id))
