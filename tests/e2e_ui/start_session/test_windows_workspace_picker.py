"""E2E: the New Chat workspace picker on a native Windows host.

On native Windows the workspace picker cannot open a folder
that lives directly under the user profile. Clicking a home-directory
child (e.g. ``work``) is rejected (the listing 404s), and going Up from
that state lands on ``/`` — which the Windows host resolves to the drive
root ``C:\\`` — so sessions started from that state use ``C:\\``.

Unlike the rest of this directory, these tests do NOT stub
``/v1/hosts`` or the filesystem endpoints with ``page.route``: doing so
would bake the expected failure into the stub. Instead a **fake Windows
host** connects over the real host WebSocket tunnel
(``/v1/hosts/{id}/tunnel``) and answers ``host.list_dir`` /
``host.create_dir`` frames with faithful Windows semantics (backslash
``os.scandir`` entry paths, ``~`` expanding to ``C:\\Users\\alice``,
``/`` resolving to the current drive root, ``/C:...`` not existing).
The real server routes and the real SPA then do whatever they do — the
exact production path a Windows machine exercises.

Covered facets (each is a claim from the bug report):

- ``test_windows_home_child_click_navigates_into_folder`` — clicking a
  home child must list that folder's contents (bug: the request becomes
  ``/C:\\Users\\alice\\work`` and 404s, the picker shows an error).
- ``test_windows_up_from_home_child_does_not_fall_to_drive_root`` —
  Up from a home child must return to home, never the drive root (bug:
  the parent of a backslash path computes to ``/`` → ``C:\\``).
- ``test_filesystem_api_addresses_drive_letter_path`` — the browse API
  must be able to address a drive-letter path at all (bug: the route
  always prefixes ``/``, so no Windows path is ever listable).
- ``test_create_directory_accepts_drive_letter_path`` — creating a
  folder under a Windows path must work (bug: the route only accepts
  paths starting with ``/`` or ``~``).

The async-in-a-fresh-thread shape is inherited from
``test_start_session.py`` (pytest-asyncio can't start a loop on the main
thread once a sync pytest-playwright test has run in the session).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import httpx
from playwright.async_api import async_playwright, expect

from omnigent.host.frames import (
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostHelloFrame,
    HostListDirEntry,
    HostListDirFrame,
    HostListDirResultFrame,
    HostListWorktreesFrame,
    HostListWorktreesResultFrame,
    HostModelOptionsFrame,
    HostModelOptionsResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)

_HOST_NAME = "win11-e2e"
_WIN_HOME = "C:\\Users\\alice"

# Simulated Windows filesystem: directory → (name, type) children.
# Entry paths on the wire are native backslash paths, exactly what the
# host daemon's ``os.scandir``-based list_dir produces on Windows.
# Copied per host connection so a create_dir in one test can't leak
# into another.
_FS_TEMPLATE: dict[str, list[tuple[str, str]]] = {
    "C:\\": [("Program Files", "directory"), ("Users", "directory"), ("Windows", "directory")],
    "C:\\Users": [("alice", "directory")],
    "C:\\Users\\alice": [("Documents", "directory"), ("work", "directory")],
    "C:\\Users\\alice\\Documents": [("notes.txt", "file")],
    "C:\\Users\\alice\\work": [("omnigent-app", "directory")],
    "C:\\Users\\alice\\work\\omnigent-app": [("README.md", "file")],
}


def _win_resolve(path: str) -> str:
    """Resolve a wire path the way Windows would.

    Mirrors ``os.path.expanduser`` + path normalization on Windows:
    ``~`` expands against the host process owner's home, forward
    slashes are accepted as separators, and a bare ``/`` (or ``\\``)
    means the current drive's root. A path like ``/C:/Users/alice``
    (the server's forced leading slash) normalizes to
    ``\\C:\\Users\\alice`` — which does not exist, same as on a real
    Windows machine.

    :param path: Path as received in a host frame.
    :returns: The normalized native path used as a filesystem key.
    """
    p = path
    if p == "~":
        p = _WIN_HOME
    elif p.startswith(("~/", "~\\")):
        p = _WIN_HOME + "\\" + p[2:]
    p = p.replace("/", "\\")
    if p in ("\\", ""):
        # Windows resolves a bare root against the current drive.
        p = "C:\\"
    # Tolerate a trailing separator (except the drive root itself).
    if len(p) > 3 and p.endswith("\\"):
        p = p.rstrip("\\")
    return p


def _win_join(parent: str, name: str) -> str:
    """Join a native Windows directory and a child name.

    :param parent: Native parent path, e.g. ``"C:\\Users\\alice"``.
    :param name: Child name, e.g. ``"work"``.
    :returns: The joined native path.
    """
    return parent + name if parent.endswith("\\") else parent + "\\" + name


def _list_dir_reply(
    frame: HostListDirFrame, fs: dict[str, list[tuple[str, str]]]
) -> HostListDirResultFrame:
    """Answer a ``host.list_dir`` frame with Windows semantics.

    :param frame: The incoming list_dir request.
    :param fs: This connection's simulated filesystem.
    :returns: The result frame a Windows host daemon would produce.
    """
    key = _win_resolve(frame.path)
    children = fs.get(key)
    if children is None:
        return HostListDirResultFrame(
            request_id=frame.request_id,
            status="ok",
            error="path does not exist",
        )
    entries = [
        HostListDirEntry(
            name=name,
            path=_win_join(key, name),
            type=entry_type,
            bytes=0 if entry_type == "file" else None,
            modified_at=0,
        )
        for name, entry_type in sorted(children)
    ]
    return HostListDirResultFrame(
        request_id=frame.request_id,
        status="ok",
        entries=entries[: frame.limit],
        has_more=len(entries) > frame.limit,
    )


def _create_dir_reply(
    frame: HostCreateDirFrame, fs: dict[str, list[tuple[str, str]]]
) -> HostCreateDirResultFrame:
    """Answer a ``host.create_dir`` frame with Windows semantics.

    Mirrors ``os.makedirs``: creates the directory (and any missing
    parents) in the simulated filesystem and returns the created
    native path.

    :param frame: The incoming create_dir request.
    :param fs: This connection's simulated filesystem (mutated).
    :returns: The result frame a Windows host daemon would produce.
    """
    key = _win_resolve(frame.path)
    if not key.startswith("C:\\") or key == "C:\\":
        return HostCreateDirResultFrame(
            request_id=frame.request_id,
            status="ok",
            error="permission denied",
        )
    if key in fs:
        return HostCreateDirResultFrame(
            request_id=frame.request_id,
            status="ok",
            error="directory already exists",
        )
    # os.makedirs semantics: create missing parents too.
    parts = key[len("C:\\") :].split("\\")
    parent = "C:\\"
    for part in parts:
        child = _win_join(parent, part)
        siblings = fs.setdefault(parent, [])
        if not any(n == part for n, _ in siblings):
            siblings.append((part, "directory"))
        fs.setdefault(child, [])
        parent = child
    return HostCreateDirResultFrame(request_id=frame.request_id, status="ok", path=key)


async def _serve_windows_host(ws: Any, fs: dict[str, list[tuple[str, str]]]) -> None:
    """Answer host frames on the tunnel like a Windows host daemon.

    Handles list_dir / create_dir / stat / worktrees / model options /
    credential detection, and replies to tunnel keepalive pings.

    :param ws: The connected ``websockets`` client connection.
    :param fs: This connection's simulated filesystem.
    """
    async for raw in ws:
        if not isinstance(raw, str):
            continue
        try:
            frame = decode_host_frame(raw)
        except ValueError:
            # Tunnel keepalive: the server pings with the runner-tunnel
            # encoding; answer with a pong the same way the real daemon does.
            try:
                runner_frame = decode_frame(raw)
            except ValueError:
                continue
            if isinstance(runner_frame, PingFrame):
                await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))
            continue
        reply: Any = None
        if isinstance(frame, HostListDirFrame):
            reply = _list_dir_reply(frame, fs)
        elif isinstance(frame, HostCreateDirFrame):
            reply = _create_dir_reply(frame, fs)
        elif isinstance(frame, HostStatFrame):
            key = _win_resolve(frame.path)
            exists = key in fs
            reply = HostStatResultFrame(
                request_id=frame.request_id,
                status="ok",
                exists=exists,
                type="directory" if exists else None,
                canonical_path=key if exists else None,
            )
        elif isinstance(frame, HostListWorktreesFrame):
            reply = HostListWorktreesResultFrame(
                request_id=frame.request_id,
                status="failed",
                error="not a git repository",
            )
        elif isinstance(frame, HostModelOptionsFrame):
            reply = HostModelOptionsResultFrame(request_id=frame.request_id, status="ok")
        elif isinstance(frame, HostDetectCredentialsFrame):
            reply = HostDetectCredentialsResultFrame(request_id=frame.request_id)
        if reply is not None:
            await ws.send(encode_host_frame(reply))


@contextlib.asynccontextmanager
async def _windows_host(base_url: str) -> AsyncIterator[str]:
    """Connect a fake Windows host to the live server's host tunnel.

    :param base_url: The live server's base URL, e.g.
        ``"http://127.0.0.1:51234"``.
    :returns: Async context manager yielding the host id the server's
        REST surface reports for this host (used in URLs and testids).
    """
    import websockets

    host_id = uuid.uuid4().hex
    fs = {key: list(children) for key, children in _FS_TEMPLATE.items()}
    ws_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{host_id}/tunnel"
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.0.0-e2e",
                    frame_protocol_version=1,
                    name=_HOST_NAME,
                )
            )
        )
        serve_task = asyncio.create_task(_serve_windows_host(ws, fs))
        try:
            # The tunnel registers the host after the hello; wait until the
            # REST surface reports it online (by its e2e-unique name) so the
            # composer can pick it. Yield the REST-reported id — that's the
            # spelling the SPA's testids and the HTTP routes use.
            rest_host_id: str | None = None
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    resp = await client.get(f"{base_url}/v1/hosts")
                    hosts = resp.json().get("hosts", [])
                    match = next(
                        (h for h in hosts if h["name"] == _HOST_NAME and h["status"] == "online"),
                        None,
                    )
                    if match is not None:
                        rest_host_id = match["host_id"]
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("fake Windows host never came online")
            assert rest_host_id is not None
            yield rest_host_id
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task


def _video_kwargs() -> dict[str, Any]:
    """Page kwargs that record a video when the recording lane asks for one.

    :returns: ``record_video_dir`` kwargs when ``OMNI_E2E_VIDEO_DIR`` is
        set (the repro/fix recording lanes), else no kwargs.
    """
    video_dir = os.environ.get("OMNI_E2E_VIDEO_DIR")
    return {"record_video_dir": video_dir} if video_dir else {}


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    Same rationale as ``test_start_session.py``: once a pytest-playwright
    sync test has run in the session, pytest-asyncio can't start a loop on
    the main thread. Exceptions (assertion failures included) re-raise on
    the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _open_picker_at_windows_home(page: Any, base_url: str, host_id: str) -> None:
    """Open the landing composer's workspace picker on the Windows host.

    Navigates to the landing screen, selects the fake Windows host from
    the host chip's dropdown, opens the picker, and waits for the home
    listing (the entries under ``C:\\Users\\alice``) to render.

    :param page: The Playwright page.
    :param base_url: The live server's base URL.
    :param host_id: REST-reported id of the fake Windows host.
    """
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    # Pick the fake Windows host explicitly. (When it's the only online host
    # on this local server the SPA labels it "This machine", so the chip text
    # can't be asserted by name — the picker's home listing below is the
    # signal that the right host is selected.)
    await page.get_by_test_id("new-chat-landing-host-chip").click()
    await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click(timeout=15_000)
    # Let the host dropdown finish its exit animation and unmount before
    # opening the picker. Opening the popover while the dropdown's exit is
    # still in flight leaves the dropdown mounted; its deferred unmount
    # auto-focuses the host chip, and that focus-outside closes the
    # workspace popover mid-test — a Radix timing artifact of clicking
    # faster than any human, not the behavior under test.
    await expect(page.locator('[data-slot="dropdown-menu-content"]')).to_have_count(0)
    await page.get_by_test_id("new-chat-landing-workspace-chip").click()
    await expect(page.get_by_test_id("workspace-picker")).to_be_visible()
    await expect(page.get_by_test_id("workspace-picker-entry-work")).to_be_visible(timeout=15_000)


def test_windows_home_child_click_navigates_into_folder(live_server: str) -> None:
    """Clicking a home-directory child folder lists that folder.

    On a Windows host the home listing's entries carry native backslash
    paths (``C:\\Users\\alice\\work``). Clicking one must navigate the
    picker INTO that directory — its contents become the listing. The
    reported bug: the picker fetches the entry path with a forced
    leading slash (``/C:\\Users\\alice\\work``), which doesn't exist, so
    the click is rejected with an error instead of navigating.
    """
    _run_in_fresh_loop(_drive_home_child_click(live_server))


async def _drive_home_child_click(base_url: str) -> None:
    async with _windows_host(base_url) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so a recorded video is finalized on context.close()
        # even when the drive fails mid-way.
        context = await browser.new_context(**_video_kwargs())
        page = await context.new_page()
        try:
            await _open_picker_at_windows_home(page, base_url, host_id)

            # The listing re-renders continuously (host polling), so a
            # position-checked click never sees a "stable" element — fire the
            # click event on the row directly instead.
            await page.get_by_test_id("workspace-picker-entry-work").dispatch_event("click")

            # Navigation succeeded ⇔ the child folder's contents are listed.
            # (Buggy build: a "doesn't exist on this host" error renders
            # instead and this entry never appears.)
            await expect(page.get_by_test_id("workspace-picker-entry-omnigent-app")).to_be_visible(
                timeout=10_000
            )
            await expect(page.get_by_test_id("workspace-picker-error")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()


def test_windows_up_from_home_child_does_not_fall_to_drive_root(live_server: str) -> None:
    """Going Up from a home child returns home — never the drive root.

    The reported bug: the picker computes parents with
    ``lastIndexOf("/")`` only, so a backslash path's parent is ``/`` —
    which the Windows host resolves to the drive root ``C:\\``. The
    listing jumps to ``C:\\`` and, because that root is what the picker
    then reports as the browsed directory, a session started from that
    state uses the drive root as its workspace.
    """
    _run_in_fresh_loop(_drive_up_from_home_child(live_server))


async def _drive_up_from_home_child(base_url: str) -> None:
    async with _windows_host(base_url) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so a recorded video is finalized on context.close()
        # even when the drive fails mid-way.
        context = await browser.new_context(**_video_kwargs())
        page = await context.new_page()
        try:
            await _open_picker_at_windows_home(page, base_url, host_id)

            # Enter the home child and wait for the click to settle — on a
            # correct build the child listing renders; on the buggy build the
            # "doesn't exist" error renders instead. Either way the picker is
            # ready for Up.
            await page.get_by_test_id("workspace-picker-entry-work").dispatch_event("click")
            settled = page.get_by_test_id("workspace-picker-entry-omnigent-app").or_(
                page.get_by_test_id("workspace-picker-error")
            )
            await expect(settled.first).to_be_visible(timeout=10_000)

            await page.get_by_test_id("workspace-picker-up").dispatch_event("click")

            # Let the post-Up listing land: the picker keeps the previous
            # listing on screen as placeholder data while the new directory
            # loads, so an immediate assertion would check the stale home
            # listing and false-pass on a build that jumped to the drive root.
            await page.wait_for_timeout(2_500)

            # Up must land back on the home listing (Documents / work) …
            await expect(page.get_by_test_id("workspace-picker-entry-Documents")).to_be_visible(
                timeout=10_000
            )
            # … and must NOT show the drive root (the buggy build lists
            # ``C:\`` — Program Files / Users / Windows — here).
            await expect(page.get_by_test_id("workspace-picker-entry-Windows")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()


def test_filesystem_api_addresses_drive_letter_path(live_server: str) -> None:
    """The host filesystem browse API can address a drive-letter path.

    ``host.list_dir`` on Windows returns native paths like
    ``C:\\Users\\alice\\work``, and session create already accepts them
    as workspaces — so the browse API must be able to list them. The
    reported bug: ``GET /v1/hosts/{id}/filesystem/{path:path}`` always
    re-adds a leading ``/`` after FastAPI strips the URL slash, so
    ``C:/Users/alice/work`` reaches the host as ``/C:/Users/alice/work``
    and 404s — no Windows path is ever listable over this API.
    """
    _run_in_fresh_loop(_drive_filesystem_api(live_server))


async def _drive_filesystem_api(base_url: str) -> None:
    async with _windows_host(base_url) as host_id, httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/v1/hosts/{host_id}/filesystem/C:/Users/alice/work")
        assert resp.status_code == 200, (
            f"listing C:/Users/alice/work should succeed, got HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )
        names = [entry["name"] for entry in resp.json()["data"]]
        assert names == ["omnigent-app"], names


def test_create_directory_accepts_drive_letter_path(live_server: str) -> None:
    """Creating a directory under a Windows drive-letter path works.

    Backs the picker's "New folder" action. The reported bug:
    ``POST /v1/hosts/{id}/directories`` rejects any path that doesn't
    start with ``/`` or ``~``, so a folder can never be created inside
    a Windows directory (``C:\\Users\\alice\\work\\new-app`` → 400).
    """
    _run_in_fresh_loop(_drive_create_directory(live_server))


async def _drive_create_directory(base_url: str) -> None:
    async with _windows_host(base_url) as host_id, httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/v1/hosts/{host_id}/directories",
            json={"path": "C:\\Users\\alice\\work\\new-app"},
        )
        assert resp.status_code == 200, (
            f"creating C:\\Users\\alice\\work\\new-app should succeed, got HTTP "
            f"{resp.status_code}: {resp.text[:200]}"
        )
        created = resp.json()["path"]
        assert created.endswith("new-app"), created
