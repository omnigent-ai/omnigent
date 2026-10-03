"""E2E: the Antigravity SDK brain must surface a run_command approval, not a fatal deny.

Polly asks for shell work against a scripted mock Gemini through a real ``omnigent host``."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Coroutine, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_POLLY_AGENT_NAME = "polly"
_PROMPT = "create a file and run ls, then delete it"
# The SDK refuses to start without a Gemini-shaped key; this one only ever reaches the mock.
_FAKE_GEMINI_KEY = "AIzaSyOmnigentE2eFakeKey00000000000000000"

_HOST_ONLINE_TIMEOUT_S = 180.0
_SESSION_URL_TIMEOUT_MS = 90_000
_TURN_OUTCOME_TIMEOUT_MS = 240_000
_VIEWPORT = {"width": 1280, "height": 720}

_APPROVAL_CARD = '[data-testid="approval-card"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("google.antigravity") is None,
    reason="google-antigravity is not installed (pip install 'omnigent[antigravity]')",
)


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop."""
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


class _MockGemini:
    """Gemini ``streamGenerateContent`` mock whose first model turn calls ``run_command``."""

    def __init__(self, workspace: str) -> None:
        self.requests: list[dict[str, Any]] = []
        self._workspace = workspace
        self._lock = threading.Lock()
        mock = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with mock._lock:
                    mock.requests.append(body)
                payload = mock._respond(self.path, body)
                self.send_response(200)
                content_type = (
                    "text/event-stream" if "alt=sse" in self.path else "application/json"
                )
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def _respond(self, path: str, body: dict[str, Any]) -> bytes:
        if ":countTokens" in path:
            return json.dumps({"totalTokens": 16}).encode()
        contents = body.get("contents") or []
        last_parts = (contents[-1].get("parts") if contents else None) or []
        if any("functionResponse" in part for part in last_parts):
            parts: list[dict[str, Any]] = [{"text": "The directory listing is above."}]
        else:
            parts = [
                {
                    "functionCall": {
                        "name": "run_command",
                        "args": {
                            "CommandLine": "ls",
                            "Cwd": self._workspace,
                            "Blocking": True,
                            "SafeToAutoRun": False,
                        },
                    }
                }
            ]
        chunk = {
            "candidates": [
                {"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}
            ],
            "usageMetadata": {
                "promptTokenCount": 32,
                "candidatesTokenCount": 8,
                "totalTokenCount": 40,
            },
            "modelVersion": "gemini-2.5-flash",
            "responseId": f"e2e-{len(self.requests)}",
        }
        data = json.dumps(chunk).encode()
        if "alt=sse" in path:
            return b"data: " + data + b"\r\n\r\n"
        return data

    def run_command_outcomes(self) -> list[str]:
        """The ``run_command`` function responses the SDK relayed back to the model."""
        outcomes: list[str] = []
        with self._lock:
            bodies = list(self.requests)
        for body in bodies:
            for content in body.get("contents") or []:
                for part in content.get("parts") or []:
                    response = part.get("functionResponse")
                    if response and response.get("name") == "run_command":
                        outcomes.append(json.dumps(response.get("response")))
        return outcomes

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _fetch_host_row(base_url: str, host_name: str) -> dict[str, Any] | None:
    hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json().get("hosts", [])
    return next(
        (h for h in hosts if h.get("name") == host_name and h.get("status") == "online"),
        None,
    )


@pytest.fixture(scope="module")
def antigravity_sdk_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` whose Antigravity SDK talks to the local mock Gemini."""
    tmp = tmp_path_factory.mktemp("antigravity_sdk_host")
    host_home = tmp / "home"
    host_home.mkdir()
    # The SDK's harness rejects a workspace under a hidden (dotted) directory.
    workspace = Path(tempfile.mkdtemp(prefix="omnigent-agy-sdk-workspace-"))
    mock = _MockGemini(str(workspace))
    host_name = f"agy-sdk-{uuid.uuid4().hex[:8]}"

    # The checkout under test goes first so the host, runner and harness import
    # this tree's omnigent even when the ambient PYTHONPATH names another checkout.
    pythonpath = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    pythonpath += [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(host_home),
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(pythonpath)),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "OMNIGENT_HOST_NAME": host_name,
        "OMNIGENT_HOST_ID": uuid.uuid4().hex,
        "GEMINI_API_KEY": _FAKE_GEMINI_KEY,
        "GOOGLE_GEMINI_BASE_URL": mock.url,
        # The host forwards only allowlisted vars to its runner; name the endpoint override.
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "GOOGLE_GEMINI_BASE_URL",
    }
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
            ],
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
            raise RuntimeError(f"host never came online:\n{log_path.read_text()[-2000:]}")
        yield {**row, "workspace": str(workspace), "mock": mock, "log_path": log_path}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        mock.close()
        shutil.rmtree(workspace, ignore_errors=True)


def _builtin_polly_id(base_url: str) -> str:
    """The id of the server-seeded Polly the picker lists under **Agents**."""
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json().get("data", [])
    polly = next((a for a in agents if a.get("name") == _POLLY_AGENT_NAME), None)
    assert polly is not None, f"the server did not seed the built-in {_POLLY_AGENT_NAME} agent"
    return str(polly["id"])


async def _reveal_agent_row(page: Any, agent_id: str) -> Any:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await expect(picker).to_be_enabled(timeout=30_000)
    await picker.click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        custom = page.get_by_test_id("new-chat-landing-custom-agents")
        if await custom.count() > 0:
            await custom.hover()
    await row.wait_for(state="visible", timeout=10_000)
    return row


async def _dismiss_menus(page: Any) -> None:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await page.keyboard.press("Escape")
    if await picker.get_attribute("aria-expanded") == "true":
        await page.keyboard.press("Escape")


async def _drive(
    base_url: str, host: dict[str, Any], agent_id: str, result: dict[str, Any]
) -> None:
    context_kwargs: dict[str, Any] = {"viewport": _VIEWPORT}
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        context_kwargs["record_video_size"] = _VIEWPORT
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(**context_kwargs)
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

            row = await _reveal_agent_row(page, agent_id)
            await row.hover()
            await page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}").click()
            harness_select = page.get_by_test_id("new-chat-landing-config-harness")
            await expect(harness_select).to_be_visible()
            await harness_select.click()
            await page.get_by_test_id("new-chat-landing-harness-antigravity").click()
            await expect(harness_select).to_contain_text("Antigravity")
            await _dismiss_menus(page)

            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=_SESSION_URL_TIMEOUT_MS)
            match = re.search(r"/c/([0-9a-f]+)", page.url)
            result["session_id"] = match.group(1) if match else None

            approval = page.locator(_APPROVAL_CARD)
            error_pill = page.locator(_ERROR_PILL)
            assistant = page.locator(_ASSISTANT_BUBBLE)
            await (
                approval.or_(error_pill)
                .or_(assistant)
                .first.wait_for(state="visible", timeout=_TURN_OUTCOME_TIMEOUT_MS)
            )
            # Let a trailing error/approval land after the first visible outcome.
            await page.wait_for_timeout(5_000)

            result["approval_visible"] = (
                await approval.count() > 0 and await approval.first.is_visible()
            )
            if await error_pill.count() > 0:
                pill = error_pill.first
                headline = pill.get_by_test_id("error-headline")
                if await headline.count() > 0:
                    with contextlib.suppress(Exception):
                        await headline.click()
                        await page.wait_for_timeout(500)
                result["error_pill"] = (await pill.inner_text()).strip()
            await page.wait_for_timeout(2_000)
        finally:
            await page.close()
            await context.close()
            await browser.close()


@pytest.mark.timeout(900)
def test_antigravity_sdk_run_command_surfaces_approval_card(
    live_server: str,
    antigravity_sdk_host: dict[str, Any],
) -> None:
    """Polly on the Antigravity brain proposes ``run_command`` → an approval card, not a deny."""
    mock: _MockGemini = antigravity_sdk_host["mock"]
    agent_id = _builtin_polly_id(live_server)
    result: dict[str, Any] = {}
    try:
        _run_in_fresh_loop(_drive(live_server, antigravity_sdk_host, agent_id, result))
    finally:
        session_id = result.get("session_id")
        transcript_errors: list[str] = []
        if session_id is not None:
            with contextlib.suppress(Exception):
                items = httpx.get(
                    f"{live_server}/v1/sessions/{session_id}/items",
                    params={"limit": 100},
                    timeout=10.0,
                ).json()
                transcript_errors = [
                    str(item.get("message", ""))
                    for item in items.get("data", [])
                    if item.get("type") == "error"
                ]
        result["transcript_errors"] = transcript_errors

    sdk_version = importlib.metadata.version("google-antigravity")
    host_log_tail = Path(antigravity_sdk_host["log_path"]).read_text()[-1500:]
    assert mock.requests, (
        "the Antigravity SDK never reached the mock Gemini endpoint — the journey did not "
        f"exercise the model (google-antigravity {sdk_version}); host log:\n{host_log_tail}"
    )

    outcomes = mock.run_command_outcomes()
    pill_text = result.get("error_pill") or ""
    denied = "confirm_run_command" in pill_text or any(
        "confirm_run_command" in err for err in result["transcript_errors"]
    )
    ran_unapproved = any("completed successfully" in outcome for outcome in outcomes)
    if denied:
        observed = "the turn died with the SDK's policy denial and no approval card"
    elif ran_unapproved:
        observed = "the SDK auto-approved the command and it ran with no approval card"
    else:
        observed = "no approval card was rendered"
    assert result.get("approval_visible"), (
        f"Antigravity's run_command must surface Omnigent's approval card; instead {observed}. "
        f"google-antigravity {sdk_version}; error pill: {pill_text!r}; transcript errors: "
        f"{result['transcript_errors']!r}; run_command results relayed to the model: {outcomes!r}"
    )
