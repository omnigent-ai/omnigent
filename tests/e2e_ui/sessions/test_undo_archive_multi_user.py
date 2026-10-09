"""Browser e2e: post-archive Undo on a multi-user server whose backend lags.

The single-user ``live_server`` hides these symptoms (rows carry no owner), so the
journeys use a header-auth multi-user server and inject the backend lag in the
browser: parked list reads, a held update-stream frame, held unarchive responses.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import websockets
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Request,
    Route,
    WebSocketRoute,
    async_playwright,
    expect,
)

from tests._helpers.async_thread import run_in_fresh_loop
from tests._helpers.session import post_session_bundle
from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _build_hello_world_bundle

VIEWER_HEADERS = {"X-Forwarded-Email": ADMIN_EMAIL}
_UPDATES_WS = re.compile(r".*/v1/sessions/updates.*")
_VIEWPORT = {"width": 1280, "height": 720}
# Longer than the server's 4 s watched-row rescan, so a lagging read spans a tick.
_LIST_READ_PARK_S = 6.0
_PATCH_HOLD_S = 5.5
_SAMPLE_INTERVAL_S = 0.1
_INSTANT_RESTORE_S = 0.5
# The unarchive write lands after the stream has re-read the restored row.
_PATCH_DELAY_S = 1.5
# The re-watch snapshot follows Undo within the client's 250 ms watch debounce.
_STALE_FRAME_TIMEOUT_S = 3.0

_SAMPLE_JS = """
(ids) => {
  const links = Array.from(document.querySelectorAll('a[href^="/c/"]'));
  const order = links.map((a) => a.getAttribute('href').slice(3));
  const rows = {};
  for (const id of ids) {
    const link = document.querySelector(`a[href="/c/${id}"]`);
    const li = link ? link.closest('li') : null;
    const badge = li ? li.querySelector('[data-testid="session-state-badge"]') : null;
    rows[id] = { visible: Boolean(link), badge: badge ? badge.getAttribute('data-state') : null };
  }
  return { order, rows };
}
"""


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    server_tmp = tmp_path_factory.mktemp("e2e_ui_undo_archive_multi_user")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def create_owned_session(base_url: str, title: str) -> str:
    """Create a hello_world session owned by the viewer identity and title it."""
    resp = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        _build_hello_world_bundle(),
        headers=VIEWER_HEADERS,
        timeout=30.0,
    )
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        headers=VIEWER_HEADERS,
        timeout=10.0,
    ).raise_for_status()
    return session_id


def delete_sessions(base_url: str, session_ids: list[str]) -> None:
    for session_id in session_ids:
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", headers=VIEWER_HEADERS, timeout=10.0)


def new_title(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


class UpdatesRelay:
    """Relay ``WS /v1/sessions/updates`` through a header-authenticated client.

    Chromium drops ``extra_http_headers`` from WebSocket handshakes (the server
    answers 403), and the relay also lets a journey hold specific server frames.
    """

    def __init__(self, base_url: str) -> None:
        self._upstream_url = base_url.replace("http", "ws", 1) + "/v1/sessions/updates"
        self.hold: Callable[[dict[str, Any]], bool] | None = None
        self.held: list[str] = []
        self.delivered: list[tuple[float, dict[str, Any]]] = []
        self.sent: list[tuple[float, str]] = []
        self.connected = asyncio.Event()
        self.errors: list[str] = []
        self._route: WebSocketRoute | None = None
        self._pending: asyncio.Queue[str | bytes] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    async def install(self, page: Page) -> None:
        await page.route_web_socket(_UPDATES_WS, self._handle)

    def _handle(self, route: WebSocketRoute) -> None:
        self._route = route
        route.on_message(lambda message: self._pending.put_nowait(message))
        task = asyncio.create_task(self._pump(route))
        task.add_done_callback(self._record_outcome)
        self._tasks.append(task)

    def _record_outcome(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        self.errors.append(f"pump ended: {exc!r}" if exc else "pump ended: upstream closed")

    async def _pump(self, route: WebSocketRoute) -> None:
        async with websockets.connect(
            self._upstream_url, additional_headers=VIEWER_HEADERS
        ) as upstream:
            self.connected.set()

            async def forward_client() -> None:
                while True:
                    message = await self._pending.get()
                    self.sent.append((time.time(), str(message)))
                    await upstream.send(message)

            forwarder = asyncio.create_task(forward_client())
            try:
                async for raw in upstream:
                    text = raw if isinstance(raw, str) else raw.decode()
                    frame = json.loads(text)
                    if self.hold is not None and self.hold(frame):
                        self.held.append(text)
                        continue
                    self.delivered.append((time.time(), frame))
                    route.send(text)
            finally:
                forwarder.cancel()

    async def release_held(self) -> list[dict[str, Any]]:
        """Deliver every held frame to the page, in arrival order."""
        released: list[dict[str, Any]] = []
        held, self.held = self.held, []
        for text in held:
            frame = json.loads(text)
            self.delivered.append((time.time(), frame))
            released.append(frame)
            assert self._route is not None
            self._route.send(text)
        return released

    async def wait_for_held(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self.held:
            if time.monotonic() > deadline:
                raise AssertionError(f"no frame matched the hold predicate within {timeout}s")
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()


def frame_marks_archived(session_id: str) -> Callable[[dict[str, Any]], bool]:
    def predicate(frame: dict[str, Any]) -> bool:
        if frame.get("type") not in ("snapshot", "changed"):
            return False
        return any(
            item.get("id") == session_id and item.get("archived") is True
            for item in frame.get("items", [])
        )

    return predicate


class ListReadPark:
    """Hold every ``GET /v1/sessions`` list read until released (search-index lag)."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._release = asyncio.Event()
        self.parked: list[tuple[float, str]] = []

    async def install(self) -> None:
        await self._page.route(
            lambda url: urlsplit(url).path.endswith("/v1/sessions"), self._handle
        )

    async def _handle(self, route: Route, request: Request) -> None:
        if request.method != "GET":
            await route.continue_()
            return
        self.parked.append((time.time(), request.url))
        await self._release.wait()
        await route.continue_()

    def release(self) -> None:
        self._release.set()


class PatchDelay:
    """Delay unarchive PATCH requests before they reach the server (slow write)."""

    def __init__(self, page: Page, session_ids: list[str], seconds: float) -> None:
        self._page = page
        self._paths = {f"/v1/sessions/{sid}" for sid in session_ids}
        self._seconds = seconds
        self.delayed: list[tuple[float, str]] = []

    async def install(self) -> None:
        await self._page.route(lambda url: urlsplit(url).path in self._paths, self._handle)

    async def _handle(self, route: Route, request: Request) -> None:
        if request.method != "PATCH" or '"archived":false' not in (request.post_data or ""):
            await route.continue_()
            return
        self.delayed.append((time.time(), request.url))
        await asyncio.sleep(self._seconds)
        await route.continue_()


class PatchHold:
    """Let unarchive PATCHes reach the server but hold their responses (slow write ack)."""

    def __init__(self, page: Page, server: MultiUserServer, session_ids: list[str]) -> None:
        self._page = page
        self._server = server
        self._paths = {f"/v1/sessions/{sid}" for sid in session_ids}
        self._release = asyncio.Event()
        self.held: list[tuple[float, str]] = []

    async def install(self) -> None:
        await self._page.route(lambda url: urlsplit(url).path in self._paths, self._handle)

    async def _handle(self, route: Route, request: Request) -> None:
        if request.method != "PATCH" or '"archived":false' not in (request.post_data or ""):
            await route.continue_()
            return
        # The driver process cannot resolve the browser-only loopback alias.
        upstream = request.url.replace(self._server.public_url, self._server.base_url, 1)
        response = await route.fetch(url=upstream)
        self.held.append((time.time(), request.url))
        await self._release.wait()
        await route.fulfill(response=response)

    def release(self) -> None:
        self._release.set()


async def open_viewer_page(
    browser: Browser, server: MultiUserServer, session_id: str
) -> tuple[BrowserContext, Page, UpdatesRelay]:
    """Open the SPA as the owning viewer with the updates stream relayed."""
    context_kwargs: dict[str, Any] = {
        "extra_http_headers": VIEWER_HEADERS,
        "viewport": _VIEWPORT,
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_kwargs["record_video_dir"] = record_dir
        context_kwargs["record_video_size"] = _VIEWPORT
    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()
    relay = UpdatesRelay(server.base_url)
    await relay.install(page)
    await page.goto(f"{server.public_url}/c/{session_id}")
    return context, page, relay


async def select_filter(page: Page, value: str) -> None:
    await page.get_by_test_id("session-filter").click()
    await page.get_by_test_id(f"session-filter-{value}").click()


def row_link(page: Page, session_id: str) -> Locator:
    return page.locator(f'a[href="/c/{session_id}"]')


async def archive_from_row(page: Page, session_id: str) -> None:
    row = page.locator("li").filter(has=row_link(page, session_id))
    await row.hover()
    await row.get_by_test_id("conversation-actions").click()
    await page.get_by_test_id("archive-conversation").click()
    await expect(row_link(page, session_id)).to_have_count(0)


async def undo_toast(page: Page, count: int) -> Locator:
    toast = page.get_by_test_id("archive-undo-toast-item")
    await expect(toast).to_contain_text(f"Archived {count} session")
    # Hovering the pill pauses its auto-dismiss, like a user resting on Undo.
    await toast.hover()
    return toast


async def sample_rows(
    page: Page, session_ids: list[str], seconds: float, into: list[dict[str, Any]]
) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        state = await page.evaluate(_SAMPLE_JS, session_ids)
        into.append({"t": time.time(), **state})
        await asyncio.sleep(_SAMPLE_INTERVAL_S)


def describe_timeline(samples: list[dict[str, Any]], session_ids: list[str], t0: float) -> str:
    """Compact transition log: one line per state change, relative to *t0*."""
    lines: list[str] = []
    previous: str | None = None
    for sample in samples:
        parts = []
        for sid in session_ids:
            row = sample["rows"][sid]
            parts.append(
                f"{sid[:6]}:{'shown' if row['visible'] else 'HIDDEN'}"
                f"{'/' + row['badge'] if row['badge'] else ''}"
            )
        order = ">".join(sid[:6] for sid in sample["order"] if sid in session_ids)
        state = f"{' '.join(parts)} order={order}"
        if state != previous:
            lines.append(f"  +{sample['t'] - t0:5.2f}s {state}")
            previous = state
    return "\n".join(lines)


def first_visible_after(samples: list[dict[str, Any]], session_id: str, t0: float) -> float | None:
    for sample in samples:
        if sample["rows"][session_id]["visible"]:
            return sample["t"] - t0
    return None


def test_undo_restores_owned_rows_to_mine_while_list_reads_lag(
    multi_user_server: MultiUserServer, browser_type_launch_args: dict[str, Any]
) -> None:
    """Undo repaints the owner's rows on "My sessions" before the lagging list read lands."""
    run_in_fresh_loop(_drive_mine_tab_restore(multi_user_server, browser_type_launch_args))


async def _drive_mine_tab_restore(server: MultiUserServer, launch_args: dict[str, Any]) -> None:
    base = server.base_url
    sid_a = create_owned_session(base, new_title("undo-mine-a"))
    sid_b = create_owned_session(base, new_title("undo-mine-b"))
    ids = [sid_a, sid_b]
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_args)
            context, page, relay = await open_viewer_page(browser, server, server.session_id)
            try:
                await expect(page.get_by_test_id("session-filter")).to_be_visible(timeout=30_000)
                await select_filter(page, "mine")
                for sid in ids:
                    await expect(row_link(page, sid)).to_be_visible(timeout=30_000)

                await archive_from_row(page, sid_a)
                await archive_from_row(page, sid_b)
                toast = await undo_toast(page, 2)

                park = ListReadPark(page)
                await park.install()
                samples: list[dict[str, Any]] = []
                t_undo = time.time()
                await toast.get_by_role("button", name="Undo").click()
                await sample_rows(page, ids, _LIST_READ_PARK_S, samples)
                park.release()
                t_release = time.time()
                await sample_rows(page, ids, 2.0, samples)

                latency = {sid: first_visible_after(samples, sid, t_undo) for sid in ids}
                print(
                    f"list reads parked {len(park.parked)} (released +{t_release - t_undo:.2f}s); "
                    f"first visible after Undo: {latency}\n"
                    + describe_timeline(samples, ids, t_undo)
                )
                for sid in ids:
                    assert latency[sid] is not None and latency[sid] < _INSTANT_RESTORE_S, (
                        f"{sid} returned to 'My sessions' {latency[sid]}s after Undo "
                        f"(list reads parked until +{t_release - t_undo:.2f}s)"
                    )
                    assert samples[-1]["rows"][sid]["visible"], f"{sid} missing after release"
            finally:
                await relay.close()
                await context.close()
                await browser.close()
    finally:
        delete_sessions(base, ids)


def test_stale_archived_frame_after_undo_keeps_restored_row(
    multi_user_server: MultiUserServer, browser_type_launch_args: dict[str, Any]
) -> None:
    """A late archived=true stream frame must not re-hide a row the user just restored."""
    run_in_fresh_loop(_drive_stale_frame(multi_user_server, browser_type_launch_args))


async def _drive_stale_frame(server: MultiUserServer, launch_args: dict[str, Any]) -> None:
    base = server.base_url
    sid = create_owned_session(base, new_title("undo-flicker"))
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_args)
            context, page, relay = await open_viewer_page(browser, server, server.session_id)
            try:
                await expect(page.get_by_test_id("session-filter")).to_be_visible(timeout=30_000)
                await select_filter(page, "mine")
                await expect(row_link(page, sid)).to_be_visible(timeout=30_000)

                relay.hold = frame_marks_archived(sid)
                await archive_from_row(page, sid)
                toast = await undo_toast(page, 1)
                delay = PatchDelay(page, [sid], _PATCH_DELAY_S)
                await delay.install()

                samples: list[dict[str, Any]] = []
                t_undo = time.time()
                await toast.get_by_role("button", name="Undo").click()
                await expect(row_link(page, sid)).to_be_visible(timeout=5_000)
                # The stream re-reads the restored row before the delayed write lands.
                await relay.wait_for_held(_STALE_FRAME_TIMEOUT_S)
                await sample_rows(page, [sid], 2.0, samples)
                park = ListReadPark(page)
                await park.install()
                t_release = time.time()
                released = await relay.release_held()
                await sample_rows(page, [sid], 3.0, samples)
                park.release()
                await sample_rows(page, [sid], 3.0, samples)

                hidden = [s["t"] - t_undo for s in samples if not s["rows"][sid]["visible"]]
                print(
                    f"unarchive PATCH delayed {len(delay.delayed)}; stale frames released "
                    f"+{t_release - t_undo:.2f}s: {[f.get('type') for f in released]}; "
                    f"hidden samples: {len(hidden)}\n" + describe_timeline(samples, [sid], t_undo)
                )
                assert not hidden, (
                    f"restored row vanished after the stale archived frame "
                    f"(hidden from +{hidden[0]:.2f}s to +{hidden[-1]:.2f}s after Undo)"
                )
            finally:
                await relay.close()
                await context.close()
                await browser.close()
    finally:
        delete_sessions(base, [sid])


def test_undo_does_not_flash_unread_dot_on_restored_rows(
    multi_user_server: MultiUserServer, browser_type_launch_args: dict[str, Any]
) -> None:
    """Rows the viewer restores must not light 'New messages' while the unarchive settles."""
    run_in_fresh_loop(_drive_unread_dot(multi_user_server, browser_type_launch_args))


async def _drive_unread_dot(server: MultiUserServer, launch_args: dict[str, Any]) -> None:
    base = server.base_url
    sid_a = create_owned_session(base, new_title("undo-dot-a"))
    sid_b = create_owned_session(base, new_title("undo-dot-b"))
    ids = [sid_a, sid_b]
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_args)
            context, page, relay = await open_viewer_page(browser, server, server.session_id)
            try:
                await expect(page.get_by_test_id("session-filter")).to_be_visible(timeout=30_000)
                await select_filter(page, "mine")
                for sid in ids:
                    await expect(row_link(page, sid)).to_be_visible(timeout=30_000)

                await archive_from_row(page, sid_a)
                await archive_from_row(page, sid_b)
                toast = await undo_toast(page, 2)

                hold = PatchHold(page, server, ids)
                await hold.install()
                samples: list[dict[str, Any]] = []
                t_undo = time.time()
                await toast.get_by_role("button", name="Undo").click()
                await sample_rows(page, ids, _PATCH_HOLD_S, samples)
                hold.release()
                t_release = time.time()
                await sample_rows(page, ids, 1.5, samples)

                dots = [
                    (s["t"] - t_undo, sid)
                    for s in samples
                    for sid in ids
                    if s["rows"][sid]["badge"] == "unseen"
                ]
                orders = {
                    ">".join(x for x in s["order"] if x in ids)
                    for s in samples
                    if all(s["rows"][sid]["visible"] for sid in ids)
                }
                assert len(hold.held) == len(ids), f"held {len(hold.held)} unarchive responses"
                print(
                    f"PATCH responses held {len(hold.held)} "
                    f"(released +{t_release - t_undo:.2f}s); unseen-dot samples: {len(dots)}; "
                    f"row orders seen: {sorted(orders)}\n"
                    + describe_timeline(samples, ids, t_undo)
                )
                assert not dots, (
                    f"restored row {dots[0][1]} showed the 'New messages' dot "
                    f"+{dots[0][0]:.2f}s after Undo (last +{dots[-1][0]:.2f}s)"
                )
            finally:
                await relay.close()
                await context.close()
                await browser.close()
    finally:
        delete_sessions(base, ids)
