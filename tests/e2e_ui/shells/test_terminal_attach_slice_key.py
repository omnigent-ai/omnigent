"""E2E: the terminal-attach WebSocket routing key is gated on the deployment.

The host_id slice key (``?omnigent_slice_key=<host_id>`` on the attach WS —
``buildAttachUrl`` in ``web/src/components/blocks/TerminalView.tsx``) pins a
session's terminal to the replica holding its runner tunnel on a host-sharded
deployment. It is deliberately gated: only a deployment that runs the sharding
layer (the embedded/managed UI, which installs a host fetcher, or ``npm run
dev`` against a workspace) emits it — an unsharded, single-replica server must
emit NO key, or it would just dirty the access log with a header nothing reads.

This drives the real flow — open a shell from the rail's "+" menu — and asserts
the attach WebSocket the browser actually opens carries **no** slice key. The
e2e harness serves the standalone SPA build (``built_spa``), which installs no
host fetcher, so this is exactly the unsharded case, and it is the observable
half of the contract in this environment. The complementary keyed emission
(embedded / workspace, where a fetcher IS installed) is not reachable from the
standalone build, so it is covered by the ``TerminalView`` / ``identity`` unit
tests (which mock the fetcher) rather than end-to-end.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def _open_new_shell(page: Page) -> None:
    """Create a shell via the tab strip's "+" -> Shell menu.

    Mirrors ``shells/test_new_shell.py``: opens the desktop Workspace rail and
    picks the single declared terminal type, which launches it directly.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def test_terminal_attach_ws_omits_slice_key_on_unsharded_server(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Opening a shell attaches without a slice key on an unsharded server.

    Captures every WebSocket the page opens, drives a real shell launch, and
    asserts the attach socket's URL carries no ``omnigent_slice_key`` — the
    standalone SPA installs no host fetcher, so the routing-key gate must
    suppress it. A regression that emitted the key unconditionally (dirtying an
    unsharded server, or mis-routing) would add the selector here.
    """
    base_url, session_id = terminal_session

    # Capture attach WebSockets (registered before nav so the attach WS, opened
    # asynchronously once the shell's xterm mounts, can't slip through).
    attach_ws_urls: list[str] = []

    def _on_ws(ws: object) -> None:
        url = ws.url  # type: ignore[attr-defined]
        if "/attach" in url:
            attach_ws_urls.append(url)

    page.on("websocket", _on_ws)

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    # Wait for the shell's xterm to connect — the attach WS is open by then.
    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    # The websocket event can trail the connected state by a tick; poll briefly.
    deadline = time.monotonic() + 10
    while not attach_ws_urls and time.monotonic() < deadline:
        page.wait_for_timeout(200)

    assert attach_ws_urls, "no terminal-attach WebSocket was opened"
    assert all("omnigent_slice_key" not in url for url in attach_ws_urls), (
        "an unsharded standalone server (no host fetcher) must not emit a slice "
        f"key on the terminal-attach WS; saw: {attach_ws_urls!r}"
    )
