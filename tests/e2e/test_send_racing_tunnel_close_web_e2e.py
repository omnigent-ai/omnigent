"""E2E (web): a send whose runner forward is cut by a tunnel close is delivered silently.

A real server and a real runner talk over the production WebSocket tunnel through
a TCP ingress proxy, and headless Chromium drives the SPA the way a user would. The
proxy holds the runner-tunnel bytes while the user sends, then severs the tunnel
with the forward in flight; the runner reconnects about a second later. The send
must ride out the drop: no error pill, no 503 on the POST, and the reply arrives
exactly once.

Run::

    uv run --no-sync pytest tests/e2e/test_send_racing_tunnel_close_web_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from tests.e2e import test_runner_tunnel_mid_turn_reconnect_grace_e2e as reconnect_lab
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
)
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _poll_until,
    _ReconnectStack,
    _session_snapshot,
    _TunnelIngressProxy,
)

_ANSWER = "TUNNEL_CLOSE_WEB_E2E_REPLY_ARRIVED"
_WEB_UI = Path(__file__).resolve().parents[2] / "omnigent" / "server" / "static" / "web-ui"

pytestmark = [pytest.mark.timeout(300, method="signal")]


class _HoldableTunnelProxy(_TunnelIngressProxy):
    """Ingress proxy that can also keep bytes in flight before severing."""

    def __init__(self, backend_host: str, backend_port: int) -> None:
        super().__init__(backend_host, backend_port)
        self._hold = threading.Event()
        # Chunks parked by a hold, so a test can see bytes are actually in flight.
        self.held_chunks = 0

    def hold(self) -> None:
        """Stop forwarding bytes while keeping every socket open."""
        self._hold.set()

    def release_hold(self) -> None:
        self._hold.clear()

    def _pipe(self, source: socket.socket, destination: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                if self._hold.is_set():
                    self.held_chunks += 1
                while self._hold.is_set():
                    if source.fileno() == -1 or destination.fileno() == -1:
                        return
                    time.sleep(0.02)
                destination.sendall(chunk)
        except OSError:
            return
        finally:
            self._close_socket(source)
            self._close_socket(destination)


@dataclass
class _UserView:
    """What the session page showed, sampled while the journey ran."""

    pill_texts: list[str] = field(default_factory=list)
    toast_texts: list[str] = field(default_factory=list)
    post_statuses: list[int] = field(default_factory=list)
    timeline: list[tuple[float, str]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def mark(self, event: str) -> None:
        self.timeline.append((round(time.monotonic() - self.started, 2), event))

    def sample(self, page: Page) -> None:
        pills = page.get_by_test_id("error-pill").all_inner_texts()
        state = f"pills={len(pills)} answer={'yes' if page.get_by_text(_ANSWER).count() else 'no'}"
        if not self.timeline or self.timeline[-1][1] != state:
            self.mark(state)
        for text in pills:
            if text not in self.pill_texts:
                self.pill_texts.append(text)
        for text in page.locator("[data-sonner-toast]").all_inner_texts():
            if text not in self.toast_texts:
                self.toast_texts.append(text)


@pytest.fixture
def web_stack(
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_ReconnectStack]:
    """A dedicated real server/runner stack whose runner tunnel can be held or severed."""
    monkeypatch.setattr(reconnect_lab, "_TunnelIngressProxy", _HoldableTunnelProxy)
    stack = _ReconnectStack(mock_llm_server_url, tmp_path)
    stack.start()
    try:
        yield stack
    finally:
        stack.teardown()


@pytest.fixture
def browser() -> Iterator[Browser]:
    # The browser-driven suite needs the built SPA and a Chromium binary; the
    # non-UI e2e lane ships neither, so skip there like the other web journeys.
    if not (_WEB_UI / "index.html").is_file():
        pytest.skip("web UI is not built; run `pnpm --filter web run build`")
    with sync_playwright() as playwright:
        args = ["--no-sandbox"] if os.environ.get("OMNIGENT_PW_NO_SANDBOX") else []
        try:
            browser = playwright.chromium.launch(headless=True, args=args)
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        try:
            yield browser
        finally:
            browser.close()


def _open_session_page(browser: Browser, url: str) -> Page:
    """Open the session page on a fresh (optionally recorded) context."""
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        record_video_dir=record_dir or None,
        record_video_size={"width": 1280, "height": 800} if record_dir else None,
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_role("textbox", name="Message the agent").wait_for(state="visible", timeout=30_000)
    return page


def _caption(page: Page, text: str) -> None:
    """Overlay a fault-timeline caption so a recording shows when the tunnel was cut."""
    page.evaluate(
        """(text) => {
            let el = document.getElementById('omni-fault-caption');
            if (!el) {
                el = document.createElement('div');
                el.id = 'omni-fault-caption';
                el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
                    'background:#1f2937;color:#fbbf24;font:14px monospace;padding:6px 12px;' +
                    'pointer-events:none;text-align:center';
                document.body.appendChild(el);
            }
            el.textContent = text;
        }""",
        text,
    )


def _send_from_composer(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.click()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _watch(
    page: Page, view: _UserView, until: Callable[[], bool], *, timeout: float, what: str
) -> None:
    """Poll *until* while sampling the page, so nothing shown mid-journey is missed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view.sample(page)
        if until():
            return
        page.wait_for_timeout(250)
    view.sample(page)
    raise AssertionError(f"Timed out after {timeout:.0f}s waiting for {what}")


def _answer_visible(page: Page) -> bool:
    return page.get_by_text(_ANSWER).count() > 0


def _prepare_session(stack: _ReconnectStack, mock_url: str) -> str:
    reset_mock_llm(mock_url)
    model = f"tunnel-close-web-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_url, [{"text": _ANSWER}], key=model)
    agent_name = register_inline_agent(
        stack.client,
        name=f"tunnel-close-web-{uuid.uuid4().hex[:8]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Return the configured answer.",
        mock_llm_base_url=f"{mock_url}/v1",
    )
    return create_runner_bound_session(
        stack.client, agent_name=agent_name, runner_id=stack.runner_id
    )


def _dump_view(
    page: Page, view: _UserView, stack: _ReconnectStack, session_id: str, out: Path
) -> None:
    """Persist what the user saw next to the stack logs for later review."""
    snapshot = _session_snapshot(stack.client, session_id)
    (out / "user-view.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "page_url": page.url,
                "page_text": page.locator("main").inner_text()
                if page.locator("main").count()
                else page.inner_text("body"),
                "error_pills": view.pill_texts,
                "error_pills_at_end": page.get_by_test_id("error-pill").all_inner_texts(),
                "timeline": view.timeline,
                "toasts": view.toast_texts,
                "post_events_statuses": view.post_statuses,
                "answer_rendered": _answer_visible(page),
                "answer_persisted_count": json.dumps(snapshot.get("items", [])).count(_ANSWER),
                "snapshot_status": snapshot.get("status"),
                "snapshot_last_error": snapshot.get("last_task_error") or snapshot.get("error"),
                "video": str(page.video.path()) if page.video else None,
            },
            indent=1,
        )
    )


def test_send_racing_the_tunnel_close_is_delivered_without_an_error(
    web_stack: _ReconnectStack,
    mock_llm_server_url: str,
    browser: Browser,
    tmp_path: Path,
) -> None:
    """A message whose runner forward is cut by the tunnel close is delivered once, silently."""
    session_id = _prepare_session(web_stack, mock_llm_server_url)
    proxy = web_stack.proxy
    assert isinstance(proxy, _HoldableTunnelProxy)
    view = _UserView()
    page = _open_session_page(browser, f"{web_stack.base_url}/c/{session_id}")
    page.on(
        "response",
        lambda response: (
            view.post_statuses.append(response.status)
            if response.request.method == "POST"
            and response.url.endswith(f"/sessions/{session_id}/events")
            else None
        ),
    )
    try:
        _caption(page, "ingress proxy holding runner-tunnel bytes; sending now")
        proxy.hold()
        view.mark("send clicked with tunnel bytes held")
        _send_from_composer(page, "Deliver this across the recycle.")
        # Hold the outbound request inside the proxy, then cut the tunnel below:
        # this exercises recovery from interrupted delivery, not a lost response.
        _watch(
            page,
            view,
            lambda: proxy.held_chunks > 0,
            timeout=10.0,
            what="tunnel bytes to be parked inside the proxy",
        )
        view.sample(page)
        started = time.monotonic()
        _caption(page, "runner tunnel severed with the send in flight; runner reconnecting")
        view.mark("tunnel severed")
        proxy.begin_blackout()
        proxy.release_hold()
        try:
            _watch(
                page,
                view,
                lambda: proxy.rejected_connections > 0,
                timeout=10.0,
                what="the runner to attempt a reconnect through the 503 ingress",
            )
            _watch(page, view, lambda: time.monotonic() - started >= 1.0, timeout=5.0, what="1s")
        finally:
            proxy.end_blackout()
        _watch(
            page, view, web_stack._runner_online, timeout=90.0, what="the runner to re-register"
        )
        view.mark("runner re-registered")
        _caption(page, "runner reconnected ~1s after the close")
        page.wait_for_timeout(4_000)
        view.sample(page)
        failed_visibly = bool(view.pill_texts or view.toast_texts) or any(
            status >= 400 for status in view.post_statuses
        )
        if not failed_visibly:
            _watch(
                page, view, lambda: _answer_visible(page), timeout=45.0, what="the reply to render"
            )
            _caption(page, "reply arrived after the reconnect; no error was shown")
            page.wait_for_timeout(2_500)
            view.sample(page)
    finally:
        # Capture debug artifacts and stop filming even when the run fails above.
        with contextlib.suppress(Exception):
            _dump_view(page, view, web_stack, session_id, tmp_path)
        page.context.close()

    def _persisted_replies() -> int:
        return json.dumps(_session_snapshot(web_stack.client, session_id).get("items", [])).count(
            _ANSWER
        )

    with contextlib.suppress(AssertionError):
        _poll_until(
            lambda: _persisted_replies() >= 1, timeout=45.0, what="the reply to be persisted"
        )
    replies = _persisted_replies()
    server_log = web_stack.process_log.read_text()
    problems = []
    if not view.post_statuses:
        problems.append("no message send was observed, so the race was never exercised")
    if any(status >= 400 for status in view.post_statuses):
        problems.append(f"the send was answered {view.post_statuses!r}")
    if view.pill_texts or view.toast_texts:
        problems.append(f"the page showed pills={view.pill_texts!r} toasts={view.toast_texts!r}")
    if "Forward to runner failed for session=" in server_log:
        problems.append("the server logged 'Forward to runner failed'")
    if replies != 1:
        problems.append(f"the reply was persisted {replies} times")
    elif problems:
        problems.append(
            "yet the message was still delivered and answered once after the reconnect"
        )
    assert not problems, (
        "A send that raced a ~1s runner-tunnel close was not held across the reconnect:\n- "
        + "\n- ".join(problems)
    )
