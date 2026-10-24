"""E2E: the session-updates WebSocket must back off while the page is hidden.

Runs at a Pixel 7 profile with the Android shell bridge stub so the SPA takes
its Android path. The updates endpoint is made unreachable by redirecting that
one WebSocket to a closed loopback port (refused at once, as an unreachable
server looks to the client); a labelled test-probe badge counts each dial so
the loop is visible on film. Headless Chromium cannot background a tab, so
hiding is synthesised by overriding ``document.visibilityState`` and
dispatching ``visibilitychange``, the input a real backgrounded page receives.
"""

from __future__ import annotations

import os
import socket

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect

from tests.e2e_ui.mobile.test_android_shell import _ANDROID_SHELL_INIT_SCRIPT

# These windows mirror web/src/lib/reconnectBackoff.ts; re-check them if its
# constants change. Visible, the socket ramps 250 ms -> 5 s (RECONNECT_BASE_MS ->
# RECONNECT_MAX_MS), so a 15 s window sees at least two fresh dials.
_RAMP_MS = 15_000
_MIN_RAMP_DIALS = 2
# Hidden, retries wait a 45 s floor (HIDDEN_RECONNECT_MS - HIDDEN_RECONNECT_JITTER_MS);
# keeping this window below that floor means a hidden-aware loop fires at most the
# one retry already pending when the page hid.
_HIDDEN_WINDOW_MS = 20_000
_MAX_HIDDEN_DIALS = 1

_WS_PROBE_SCRIPT = """
(() => {
  const dials = (window.__updatesDials = []);
  const NativeWebSocket = window.WebSocket;
  const render = () => {
    let badge = document.getElementById("updates-dial-probe");
    if (!badge) {
      badge = document.createElement("div");
      badge.id = "updates-dial-probe";
      badge.style.cssText =
        "position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483647;" +
        "pointer-events:none;padding:8px 10px;border-radius:8px;" +
        "font:600 13px/1.3 ui-monospace,monospace;color:#fff;" +
        "background:rgba(185,28,28,0.92);box-shadow:0 2px 8px rgba(0,0,0,0.3)";
      document.body.appendChild(badge);
    }
    const hiddenAt = window.__hiddenAt;
    const sinceHidden =
      hiddenAt === undefined ? null : dials.filter((t) => t > hiddenAt).length;
    badge.textContent =
      "TEST PROBE \\u2014 session-updates WebSocket dials (server unreachable): " +
      dials.length +
      (sinceHidden === null
        ? " \\u00b7 page visible"
        : " \\u00b7 page HIDDEN \\u00b7 " + sinceHidden + " since hidden");
  };
  window.__renderDialProbe = render;
  window.WebSocket = class extends NativeWebSocket {
    constructor(url, protocols) {
      let target = url;
      if (String(url).includes("/v1/sessions/updates")) {
        const rewritten = new URL(String(url));
        // Force loopback so a remote --ui-base-url still dials the locally
        // reserved, never-listening port and is refused at once.
        rewritten.hostname = "127.0.0.1";
        rewritten.port = String(__CLOSED_PORT__);
        target = rewritten.toString();
        dials.push(performance.now());
        if (document.body) render();
      }
      super(target, protocols);
    }
  };
})();
"""

_HIDE_PAGE_SCRIPT = """
() => {
  Object.defineProperty(Document.prototype, "visibilityState", {
    configurable: true,
    get: () => "hidden",
  });
  Object.defineProperty(Document.prototype, "hidden", { configurable: true, get: () => true });
  window.__hiddenAt = performance.now();
  document.dispatchEvent(new Event("visibilitychange"));
  window.__renderDialProbe();
  return window.__hiddenAt;
}
"""


def _reserve_closed_loopback_port() -> socket.socket:
    """Bind a loopback port without listening and keep it reserved for the test's
    lifetime, so dials to it are refused at once and no other process can take it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    return probe


def _new_android_phone_page(
    playwright: Playwright, browser: Browser, closed_port: int
) -> tuple[BrowserContext, Page]:
    """Open a Pixel 7 context with the Android bridge stub and the dial probe installed."""
    options = {
        key: value
        for key, value in playwright.devices["Pixel 7"].items()
        if key != "default_browser_type"
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        options["record_video_dir"] = record_dir
        options["record_video_size"] = options["viewport"]
    context = browser.new_context(**options)
    try:
        context.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
        context.add_init_script(_WS_PROBE_SCRIPT.replace("__CLOSED_PORT__", str(closed_port)))
        page = context.new_page()
    except Exception:
        context.close()
        raise
    return context, page


def test_hidden_page_stretches_session_updates_reconnect(
    playwright: Playwright,
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A hidden page stops redialing the unreachable updates endpoint at the visible cadence.

    :param playwright: Playwright handle (for the Pixel 7 device descriptor).
    :param browser: Session-scoped browser; the test opens its own phone context.
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session
    reserved_port = _reserve_closed_loopback_port()
    context: BrowserContext | None = None
    try:
        context, page = _new_android_phone_page(
            playwright, browser, int(reserved_port.getsockname()[1])
        )
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
        expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")

        # Count only the dials fired during the ramp window, not those accrued
        # since page load, so the gate proves the loop is still redialing now.
        dials_before_ramp = page.evaluate("() => window.__updatesDials.length")
        page.wait_for_timeout(_RAMP_MS)
        ramp_dials = page.evaluate("() => window.__updatesDials.length") - dials_before_ramp
        assert ramp_dials >= _MIN_RAMP_DIALS, (
            "expected the socket to keep redialing the unreachable updates endpoint "
            f"while visible, saw {ramp_dials} new dials in {_RAMP_MS} ms"
        )

        hidden_at = page.evaluate(_HIDE_PAGE_SCRIPT)
        page.wait_for_timeout(_HIDDEN_WINDOW_MS)
        dials: list[float] = page.evaluate("() => window.__updatesDials")
        hidden_dials = [round((t - hidden_at) / 1000, 1) for t in dials if t > hidden_at]
        assert len(hidden_dials) <= _MAX_HIDDEN_DIALS, (
            f"expected at most {_MAX_HIDDEN_DIALS} session-updates dial while the page was "
            f"hidden for {_HIDDEN_WINDOW_MS} ms, but the loop kept its visible cadence: "
            f"{len(hidden_dials)} dials at +{hidden_dials} s after hiding "
            f"({ramp_dials} dials during the visible ramp)"
        )
    finally:
        if context is not None:
            context.close()
        reserved_port.close()
