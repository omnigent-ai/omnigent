"""E2E: a configured generic-ACP agent (``acp:<slug>``) must not read "needs setup"."""

# A real server and ``omnigent host`` daemon share one HOME whose ``acp:`` block
# registers TraeX, so the picker row, the readiness map, and the launch all come
# from the same config. The ACP agent is a stdlib stdio stand-in (no vendor CLI).

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ACP_SLUG = "traex"
_ACP_HARNESS = f"acp:{_ACP_SLUG}"
_ACP_DISPLAY_NAME = "TraeX"
_ACP_REPLY_TEXT = "TraeX stand-in reply: the configured ACP agent launched and answered."

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 180.0
_REPLY_TIMEOUT_MS = 180_000

# Minimal ACP agent over stdio: answers initialize / session/new, streams one
# text chunk per prompt, and acknowledges anything else so the executor never
# stalls on an unexpected request.
_STANDIN_ACP_AGENT = f"""\
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {{}}
    if method == "initialize":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{
            "protocolVersion": 1,
            "agentCapabilities": {{"promptCapabilities": {{"image": False}}}},
            "authMethods": [],
        }}}})
    elif method == "session/new":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"sessionId": "traex-standin-1"}}}})
    elif method == "session/prompt":
        send({{"jsonrpc": "2.0", "method": "session/update", "params": {{
            "sessionId": params["sessionId"],
            "update": {{"sessionUpdate": "agent_message_chunk",
                       "content": {{"type": "text", "text": {_ACP_REPLY_TEXT!r}}}}},
        }}}})
        send({{"jsonrpc": "2.0", "id": mid, "result": {{
            "stopReason": "end_turn",
            "usage": {{"inputTokens": 3, "outputTokens": 5, "totalTokens": 8}},
        }}}})
    elif mid is not None:
        send({{"jsonrpc": "2.0", "id": mid, "result": {{}}}})"""


@dataclass(frozen=True)
class AcpSlugStack:
    """A server and host daemon sharing one HOME that configures ``acp:traex``."""

    base_url: str
    host_id: str
    host_name: str
    agent_id: str
    home: Path
    workspace: Path


def _run_in_fresh_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run *coro* to completion in a dedicated thread with its own event loop."""
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
    return captured.get("result")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _fetch_host_row(base_url: str, host_name: str) -> dict[str, Any] | None:
    hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json().get("hosts", [])
    return next(
        (h for h in hosts if h.get("name") == host_name and h.get("status") == "online"),
        None,
    )


@pytest.fixture(scope="module")
def acp_slug_stack(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AcpSlugStack]:
    """Spawn a server and a real ``omnigent host`` from one HOME with an ``acp:`` block."""
    # ACP picker rows are seeded from the server's own config at startup, so the
    # shared e2e server cannot be reused; both processes get a minimal, credential-free env.
    tmp = tmp_path_factory.mktemp("acp_slug_stack")
    home = tmp / "home"
    (home / ".omnigent").mkdir(parents=True)
    workspace = tmp / "workspace"
    workspace.mkdir()
    agent_script = tmp / "standin_acp_agent.py"
    agent_script.write_text(_STANDIN_ACP_AGENT)
    (home / ".omnigent" / "config.yaml").write_text(
        "acp:\n"
        "  agents:\n"
        f"    - name: {_ACP_DISPLAY_NAME}\n"
        f"      command: {shlex.join([sys.executable, str(agent_script)])}\n"
    )

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "PYTHONPATH": os.pathsep.join(
            [
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
            ]
        ),
    }
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    artifacts = tmp / "artifacts"
    artifacts.mkdir()
    server_log = (tmp / "server.log").open("w")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp / 'server.db'}",
            "--artifact-location",
            str(artifacts),
        ],
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    host: subprocess.Popen[bytes] | None = None
    host_name = f"traex-host-{uuid.uuid4().hex[:6]}"
    host_id = uuid.uuid4().hex
    host_log = (tmp / "host.log").open("w")
    try:
        deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
        agent_id: str | None = None
        while time.monotonic() < deadline and agent_id is None:
            if server.poll() is not None:
                raise RuntimeError(
                    f"server exited early ({server.returncode}):\n"
                    f"{(tmp / 'server.log').read_text()[-3000:]}"
                )
            try:
                agents = httpx.get(f"{base_url}/v1/agents", timeout=5.0).json().get("data", [])
            except (httpx.HTTPError, ValueError):
                time.sleep(0.5)
                continue
            agent_id = next((a["id"] for a in agents if a.get("harness") == _ACP_HARNESS), None)
            if agent_id is None:
                time.sleep(0.5)
        if agent_id is None:
            raise RuntimeError(
                f"server never seeded the {_ACP_HARNESS!r} picker row:\n"
                f"{(tmp / 'server.log').read_text()[-3000:]}"
            )

        host = subprocess.Popen(
            [sys.executable, "-m", "omnigent", "host", "--server", base_url, "--non-interactive"],
            env={**env, "OMNIGENT_HOST_NAME": host_name, "OMNIGENT_HOST_ID": host_id},
            stdout=host_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        row: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            row = _fetch_host_row(base_url, host_name)
            if row is not None and row.get("configured_harnesses"):
                break
            if host.poll() is not None:
                raise RuntimeError(
                    f"omnigent host exited early ({host.returncode}):\n"
                    f"{(tmp / 'host.log').read_text()[-3000:]}"
                )
            time.sleep(1.0)
        if row is None or not row.get("configured_harnesses"):
            raise RuntimeError(
                f"host never came online with a readiness map:\n"
                f"{(tmp / 'host.log').read_text()[-3000:]}"
            )
        yield AcpSlugStack(
            base_url=base_url,
            host_id=str(row["host_id"]),
            host_name=host_name,
            agent_id=agent_id,
            home=home,
            workspace=workspace,
        )
    finally:
        if host is not None:
            _terminate(host)
        _terminate(server)
        host_log.close()
        server_log.close()


def test_host_readiness_map_keys_the_configured_acp_slug(acp_slug_stack: AcpSlugStack) -> None:
    """The host's readiness map must carry ``acp:traex`` with the ``acp`` readiness."""
    # The launch gate accepts the same slug under this HOME, so the map the picker
    # filters on must agree; a missing key is what the web renders as "needs setup".
    stack = acp_slug_stack
    gate = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.onboarding.harness_readiness import harness_is_configured;"
            f"print(harness_is_configured({_ACP_HARNESS!r}))",
        ],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(stack.home),
            "PYTHONPATH": str(_REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert gate.stdout.strip() == "True", f"launch gate rejected {_ACP_HARNESS!r}: {gate.stdout!r}"

    resp = httpx.get(f"{stack.base_url}/v1/hosts/{stack.host_id}", timeout=10.0)
    assert resp.status_code == 200, resp.text
    configured = resp.json().get("configured_harnesses") or {}
    assert configured.get("acp") is True, f"generic acp readiness: {configured.get('acp')!r}"
    assert configured.get(_ACP_HARNESS) is True, (
        f"host reports {_ACP_HARNESS!r} as {configured.get(_ACP_HARNESS)!r} although "
        f"acp is {configured.get('acp')!r} and the launch gate accepts the slug"
    )


def test_configured_acp_slug_agent_is_not_flagged_needs_setup(
    acp_slug_stack: AcpSlugStack,
) -> None:
    """Selecting the configured TraeX agent shows no setup badge or notice, and it launches."""
    observed = _run_in_fresh_loop(_drive(acp_slug_stack))

    assert observed["reply_visible"], (
        f"the configured ACP agent did not answer the first message: {observed}"
    )
    assert observed["row_badge_count"] == 0 and observed["row_enabled"], (
        f"picker row for the launchable {_ACP_DISPLAY_NAME} agent is flagged: {observed}"
    )
    assert observed["notice_count"] == 0, (
        f"New Chat warned that the launchable {_ACP_DISPLAY_NAME} agent needs setup on "
        f"{acp_slug_stack.host_name}: {observed['notice_text']!r}; observations: {observed}"
    )


async def _seed_recent_workspace(page: Any, host_id: str, workspace: str) -> None:
    await page.add_init_script(
        "window.localStorage.setItem("
        '"omnigent:recent-workspaces", '
        f"JSON.stringify({json.dumps({host_id: [workspace]})}))"
    )


async def _select_host(page: Any, host_id: str) -> None:
    chip = page.get_by_test_id("new-chat-landing-host-chip")
    await chip.click()
    row = page.get_by_test_id(f"new-chat-landing-host-{host_id}")
    await row.click(timeout=15_000)
    await expect(page.get_by_test_id("new-chat-landing-host-menu")).to_have_count(0)


async def _reveal_agent_row(page: Any, agent_id: str) -> Any:
    """Open the landing picker and reveal *agent_id*'s row without clicking it."""
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if await more.count() > 0:
            await more.click()
    if await row.count() == 0:
        custom = page.get_by_test_id("new-chat-landing-custom-agents")
        if await custom.count() > 0:
            await custom.hover()
    await row.wait_for(state="visible", timeout=10_000)
    return row


async def _dismiss_agent_menu(page: Any) -> None:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await page.keyboard.press("Escape")
    if await picker.get_attribute("aria-expanded") == "true":
        await page.keyboard.press("Escape")


async def _open_landing(page: Any, stack: AcpSlugStack) -> None:
    await _seed_recent_workspace(page, stack.host_id, str(stack.workspace))
    await page.goto(f"{stack.base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    await _select_host(page, stack.host_id)


async def _drive(stack: AcpSlugStack) -> dict[str, Any]:
    observed: dict[str, Any] = {"video_paths": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # Separate contexts so the picker state and the composer journey record as distinct clips.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _open_landing(page, stack)
            row = await _reveal_agent_row(page, stack.agent_id)
            await row.hover()
            observed["row_label"] = (await row.inner_text()).strip()
            badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{stack.agent_id}")
            observed["row_badge_count"] = await badge.count()
            observed["row_badge_label"] = (
                await badge.first.get_attribute("aria-label")
                if observed["row_badge_count"]
                else None
            )
            observed["row_enabled"] = await row.is_enabled()
            await page.wait_for_timeout(1_500)
            await _dismiss_agent_menu(page)
        finally:
            if page.video is not None:
                observed["video_paths"].append(await page.video.path())
            await context.close()

        context = await browser.new_context()
        page = await context.new_page()
        session_id: str | None = None
        try:
            await _open_landing(page, stack)
            row = await _reveal_agent_row(page, stack.agent_id)
            await row.click()
            await _dismiss_agent_menu(page)
            notice = page.get_by_test_id("new-chat-landing-harness-warning")
            with contextlib.suppress(PlaywrightTimeoutError):
                await notice.wait_for(state="visible", timeout=10_000)
            observed["notice_count"] = await notice.count()
            observed["notice_text"] = (
                (await notice.inner_text()).strip() if observed["notice_count"] else None
            )
            await page.wait_for_timeout(1_500)

            await page.get_by_test_id("new-chat-landing-input").fill("Say hello")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
            match = re.search(r"/c/([0-9a-f]+)", page.url)
            session_id = match.group(1) if match else None
            observed["session_id"] = session_id
            reply = page.get_by_text(_ACP_REPLY_TEXT)
            error_pill = page.locator('[data-testid="error-pill"][data-level="error"]')
            with contextlib.suppress(AssertionError):
                await expect(reply.or_(error_pill.first).first).to_be_visible(
                    timeout=_REPLY_TIMEOUT_MS
                )
            observed["reply_visible"] = await reply.count() > 0
            observed["error_pill_text"] = (
                (await error_pill.first.inner_text()).strip() if await error_pill.count() else None
            )
            await page.wait_for_timeout(1_500)
        finally:
            if page.video is not None:
                observed["video_paths"].append(await page.video.path())
            await context.close()
            await browser.close()
    return observed
