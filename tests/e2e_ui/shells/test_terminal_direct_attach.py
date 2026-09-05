"""E2E: the web terminal attaches over loopback when the runner is local.

A relayed attach costs two WAN legs per keystroke (browser → server →
runner tunnel → tmux). When the browser and the runner share a machine the
runner's loopback listener (``omnigent/runner/direct_attach.py``) serves the
same attach handler directly, and the client swaps onto it — see
``resolveInitialAttachUrl`` / ``watchDirectUpgrade`` in
``web/src/lib/terminals.ts``.

The e2e harness is exactly the local shape: server, runner, and browser all
on one box, with the page served from ``http://127.0.0.1:<port>``. Both
halves of the contract are therefore observable here:

- the terminal ends up on the loopback socket, and
- it still connects and stays connected when that socket is unreachable
  (a different machine, denied Local Network Access, Safari, or a dead
  listener) — the failure mode that would otherwise break the terminal for
  every remote user.

Relay and direct sockets share the loopback host in this harness, so they
are told apart by port (the listener picks its own ephemeral port, never the
server's) plus the direct URL's mandatory ``token``.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Rewrites off-server ``ws://`` dials to a dead port so the runner's loopback
# listener is unreachable while the relay keeps working. Faithful to the real
# failure modes: the browser's own connect fails, rather than a faked event.
_BLOCK_LOOPBACK_DIALS = """
(() => {
  const Native = window.WebSocket;
  const Blocked = function (url, protocols) {
    let target = url;
    try {
      const parsed = new URL(url, location.href);
      if (parsed.protocol === "ws:" && parsed.port !== location.port) {
        parsed.port = "1";
        target = parsed.toString();
      }
    } catch (err) {
      /* leave non-URL inputs to the native constructor */
    }
    return protocols === undefined ? new Native(target) : new Native(target, protocols);
  };
  Blocked.prototype = Native.prototype;
  Blocked.CONNECTING = Native.CONNECTING;
  Blocked.OPEN = Native.OPEN;
  Blocked.CLOSING = Native.CLOSING;
  Blocked.CLOSED = Native.CLOSED;
  window.WebSocket = Blocked;
})();
"""


def _open_new_shell(page: Page) -> None:
    """Create a shell via the tab strip's "+" -> Shell menu.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _capture_attach_sockets(page: Page) -> list[str]:
    """Record the URL of every terminal-attach WebSocket the page opens.

    Registered before navigation so the attach socket — opened
    asynchronously once the shell's xterm mounts — cannot slip through.

    :param page: Playwright page, not yet navigated.
    :returns: A list that fills in as sockets open.
    """
    attach_urls: list[str] = []

    def _on_ws(ws: object) -> None:
        url = ws.url  # type: ignore[attr-defined]
        if "/attach" in url:
            attach_urls.append(url)

    page.on("websocket", _on_ws)
    return attach_urls


def _is_direct_attach(url: str, base_url: str) -> bool:
    """Is *url* the runner's loopback attach socket rather than the relay?

    :param url: An observed attach WebSocket URL.
    :param base_url: The harness server's base URL.
    :returns: True for a token-bearing loopback socket on a non-server port.
    """
    parts = urlsplit(url)
    return (
        parts.scheme == "ws"
        and parts.hostname == "127.0.0.1"
        and parts.port != urlsplit(base_url).port
        and "token=" in (parts.query or "")
    )


def _connected_terminal(page: Page) -> object:
    """Wait for the newest terminal view to report a live attach.

    :param page: Playwright page with a shell opening.
    :returns: The terminal-view locator, connected.
    """
    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def test_terminal_upgrades_to_loopback_attach_when_runner_is_local(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A same-machine runner ends up serving the terminal over loopback.

    The client connects via the relay first (never blocking the terminal on a
    permission prompt) and swaps to the direct socket once the loopback probe
    succeeds, so this polls for the direct socket instead of demanding it on
    the first dial. A regression that dropped the advert, the probe, or the
    re-dial leaves every attach on the relay and fails here.
    """
    base_url, session_id = terminal_session

    attach_urls = _capture_attach_sockets(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)

    deadline = time.monotonic() + 60
    while not any(_is_direct_attach(u, base_url) for u in attach_urls):
        if time.monotonic() >= deadline:
            raise AssertionError(
                "terminal never attached over the runner's loopback listener; "
                f"attach sockets seen: {attach_urls!r}"
            )
        page.wait_for_timeout(250)

    # The swap must leave a working terminal, not a dead pane.
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)


def test_terminal_falls_back_to_relay_when_loopback_is_unreachable(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """An unreachable loopback listener leaves the terminal on the relay.

    This is the path every remote browser takes, so it must degrade in
    silence: the terminal connects over the relay and never lands on a
    direct socket. A regression that dialed direct unconditionally, or failed
    the attach when the probe failed, hangs the terminal here.
    """
    base_url, session_id = terminal_session

    attach_urls = _capture_attach_sockets(page)
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_terminal(page)

    # Give the background upgrade room to (wrongly) succeed before asserting.
    page.wait_for_timeout(3_000)
    expect(terminal_view).to_have_attribute("data-state", "connected")

    relayed = [u for u in attach_urls if urlsplit(u).port == urlsplit(base_url).port]
    assert relayed, f"terminal did not attach over the relay; saw: {attach_urls!r}"
    assert not any(_is_direct_attach(u, base_url) for u in attach_urls), (
        f"terminal used a direct socket despite an unreachable listener: {attach_urls!r}"
    )
