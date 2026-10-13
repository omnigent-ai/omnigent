"""E2E: a slow browser download must not make the server buffer the whole file.
The viewer's capped preview read is awaited and server RSS settled before the
link is throttled and baselined, so the measured growth is the download's alone."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Page, Response, expect

from tests.e2e_ui.conftest import open_right_rail

# ``_server_rss_kib`` reads ``/proc/<pid>/status``, which only exists on Linux.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="server RSS probe reads /proc, Linux-only"
)

_FILE_NAME = "big-download.bin"
_FILE_BYTES = 48 * 1024 * 1024
_LINK_RATE_BYTES_PER_S = 1_500_000
# Headroom for ordinary RSS jitter over the download; buffering even half
# of the file would exceed it.
_MAX_SERVER_GROWTH_MIB = 16.0
_RSS_SETTLE_WINDOW_S = 1.0
_RSS_SETTLE_TIMEOUT_S = 15.0


class SlowLinkProxy:
    """Loopback TCP proxy that caps server->browser throughput.
    It reads one rate slice from the server per tick, so TCP backpressure
    reaches the server exactly as a slow client link would."""

    def __init__(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port
        self._rate: int | None = None
        self.port = 0
        self.bytes_to_client = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="slow-link-proxy", daemon=True)

    def start(self) -> int:
        self._thread.start()
        assert self._ready.wait(5), "proxy did not start"
        return self.port

    def stop(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.create_task, self._shutdown())
        self._thread.join(5)

    async def _shutdown(self) -> None:
        assert self._server is not None and self._loop is not None
        self._server.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        self._loop.stop()

    def set_rate(self, rate_bytes_per_s: int | None) -> None:
        self._rate = rate_bytes_per_s

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", 0)
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _handle(
        self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            await self._pump(client_r, client_w)
        finally:
            self._handlers.discard(task)

    async def _pump(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", self._upstream_port)
        except OSError:
            client_w.close()
            return

        async def to_upstream() -> None:
            try:
                while data := await client_r.read(65536):
                    up_w.write(data)
                    await up_w.drain()
            finally:
                up_w.close()

        async def to_client() -> None:
            try:
                while True:
                    rate = self._rate
                    data = await up_r.read(65536 if rate is None else max(1024, rate // 20))
                    if not data:
                        break
                    started = time.monotonic()
                    client_w.write(data)
                    await client_w.drain()
                    self.bytes_to_client += len(data)
                    if rate is not None:
                        delay = len(data) / rate - (time.monotonic() - started)
                        if delay > 0:
                            await asyncio.sleep(delay)
            finally:
                client_w.close()

        await asyncio.gather(to_upstream(), to_client(), return_exceptions=True)


def _server_rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError(f"no VmRSS for pid {pid}")


def _settled_server_rss_kib(pid: int) -> int:
    """Return the server RSS once it has held still for a short window.

    Fails loudly if RSS never settles, so a drifting baseline can't quietly
    skew the absolute-growth assertion into a flaky pass or fail.
    """
    deadline = time.monotonic() + _RSS_SETTLE_TIMEOUT_S
    last = _server_rss_kib(pid)
    still_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.2)
        current = _server_rss_kib(pid)
        if abs(current - last) > 1024:
            still_since = time.monotonic()
        last = current
        if time.monotonic() - still_since >= _RSS_SETTLE_WINDOW_S:
            return last
    raise AssertionError(
        f"server RSS never settled within {_RSS_SETTLE_TIMEOUT_S:.0f}s "
        f"(last {last} KiB); baseline unreliable"
    )


def _is_preview_read(file_name: str) -> Callable[[Response], bool]:
    def matches(response: Response) -> bool:
        url = urlparse(response.url)
        return (
            response.request.method == "GET"
            and url.path.endswith(f"/filesystem/{file_name}")
            and parse_qs(url.query).get("download", ["false"])[0] != "true"
        )

    return matches


def test_slow_download_keeps_server_memory_flat(
    seeded_session: tuple[str, str],
    server_pid: int,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """Downloading a large file over a slow link leaves server memory flat."""
    base_url, session_id = seeded_session
    # Listing the root materializes a fresh session's workspace on the runner.
    listing = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem",
        timeout=30,
    )
    assert listing.status_code == 200, listing.text
    target = Path(listing.json()["base"]) / _FILE_NAME
    payload = os.urandom(_FILE_BYTES)
    target.write_bytes(payload)
    request.addfinalizer(lambda: target.unlink(missing_ok=True))

    upstream_port = urlparse(base_url).port
    assert upstream_port is not None, f"base_url must carry an explicit port: {base_url}"
    proxy = SlowLinkProxy(upstream_port)
    proxy_url = f"http://127.0.0.1:{proxy.start()}"
    request.addfinalizer(proxy.stop)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{proxy_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    row = rail.get_by_role("button", name=re.compile(re.escape(_FILE_NAME))).filter(
        has_text=_FILE_NAME
    )
    expect(row).to_be_visible(timeout=30_000)

    pre_preview_kib = _server_rss_kib(server_pid)
    with page.expect_response(_is_preview_read(_FILE_NAME), timeout=30_000) as preview_info:
        row.click()
    expect(rail.get_by_test_id("file-viewer")).to_be_visible()
    preview = preview_info.value
    assert preview.ok, f"preview read failed: {preview.status} {preview.url}"
    assert preview.finished() is None, "preview read did not complete"
    post_preview_kib = _server_rss_kib(server_pid)

    # The Download action only appears once the preview has loaded; settling
    # first keeps the preview's release out of the download measurement.
    baseline_kib = _settled_server_rss_kib(server_pid)
    rail.get_by_role("button", name="View settings").click()
    download_item = page.get_by_role("menuitem", name="Download file")
    expect(download_item).to_be_visible()

    proxy.set_rate(_LINK_RATE_BYTES_PER_S)
    received_before = proxy.bytes_to_client
    samples: list[tuple[float, int, int]] = []
    stop = threading.Event()
    started = time.monotonic()

    def _sample() -> None:
        while not stop.is_set():
            samples.append(
                (
                    round(time.monotonic() - started, 2),
                    _server_rss_kib(server_pid),
                    proxy.bytes_to_client - received_before,
                )
            )
            stop.wait(0.5)

    sampler = threading.Thread(target=_sample, name="server-rss-sampler", daemon=True)
    sampler.start()
    try:
        with page.expect_download() as download_info:
            download_item.click()
        download = download_info.value
        saved = tmp_path / _FILE_NAME
        download.save_as(saved)
    finally:
        stop.set()
        sampler.join(2)

    assert samples, "sampler thread recorded no RSS samples (did the server die?)"
    peak_t, peak_kib, received_at_peak = max(samples, key=lambda s: s[1])
    growth_mib = (peak_kib - baseline_kib) / 1024
    summary = {
        "file_mib": _FILE_BYTES / 2**20,
        "link_rate_bytes_per_s": _LINK_RATE_BYTES_PER_S,
        "download_elapsed_s": round(time.monotonic() - started, 2),
        "session_id": session_id,
        "base_url": base_url,
        "server_pid": server_pid,
        "preview_content_type": preview.headers.get("content-type"),
        "preview_content_length": preview.headers.get("content-length"),
        "pre_preview_rss_mib": round(pre_preview_kib / 1024, 1),
        "post_preview_rss_mib": round(post_preview_kib / 1024, 1),
        "baseline_rss_mib": round(baseline_kib / 1024, 1),
        "peak_rss_mib": round(peak_kib / 1024, 1),
        "peak_growth_mib": round(growth_mib, 1),
        "peak_at_s": peak_t,
        "browser_received_at_peak_mib": round(received_at_peak / 2**20, 1),
        "samples": [(t, round(kib / 1024, 1), round(b / 2**20, 1)) for t, kib, b in samples],
    }
    print(json.dumps(summary))
    evidence_dir = os.environ.get("OMNIGENT_REPRO_EVIDENCE_DIR")
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        (Path(evidence_dir) / "slow-download-server-memory.json").write_text(
            json.dumps(summary, indent=2)
        )

    assert download.suggested_filename == _FILE_NAME
    assert saved.read_bytes() == payload
    assert growth_mib <= _MAX_SERVER_GROWTH_MIB, (
        f"server RSS grew {growth_mib:.1f} MiB at t={peak_t}s while the browser had received "
        f"only {received_at_peak / 2**20:.1f} MiB of {_FILE_BYTES / 2**20:.0f} MiB: the tunnel "
        "buffered the undelivered remainder instead of pausing the runner"
    )
