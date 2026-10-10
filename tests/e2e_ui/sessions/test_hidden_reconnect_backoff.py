"""Hidden-page reconnect churn: the session-updates WebSocket redials forever.

The sidebar's session-updates WebSocket (``web/src/lib/sessionUpdatesSocket.ts``)
reconnects with an exponential backoff capped at 5 s and has no visibility
awareness at all. When the server is unreachable and the page is hidden (a
backgrounded browser tab, or the Android/iOS WebView shells while the app is in
the background), the page keeps waking the radio to redial roughly every 2.5-5 s
indefinitely -- a sustained battery drain the user never sees.

Desired behavior (the regression contract this test encodes): while the
document is hidden, reconnect attempts must slow to a background-friendly
cadence (e.g. a ~60 s cap), so a 16 s hidden window sees at most one dial
(a timer armed before the page hid may still fire once).

The test drives the real SPA against the spawned live server. The updates
socket itself is made unreachable by rewriting its URL to a dead local port in
a ``WebSocket`` constructor wrapper (a genuine refused TCP connect, exactly
what an unreachable server looks like to the client), and document visibility
is emulated with a controllable ``document.hidden`` override that dispatches
``visibilitychange`` -- the same signal a real backgrounded tab receives. The
socket code under test reads only those standard APIs, so the emulation
exercises the real code path end to end. A small on-page badge visualizes each
dial for the recorded footage; it plays no part in the assertions.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"

# Wraps window.WebSocket to (a) count session-updates dials with the page's
# hidden state at dial time, and (b) point them at a dead local port so every
# handshake is refused -- the client-side signature of an unreachable server.
# Also installs a controllable document-visibility override: the socket code
# under test only ever reads document.hidden / visibilitychange, so flipping
# these is exactly what backgrounding the tab/app looks like to it.
_INIT_SCRIPT = """
(() => {
  let hidden = false;

  const badge = () => {
    let el = document.getElementById("__updates-dial-badge");
    if (!el) {
      el = document.createElement("div");
      el.id = "__updates-dial-badge";
      el.style.cssText =
        "position:fixed;bottom:8px;left:8px;z-index:2147483647;" +
        "background:#b91c1c;color:#fff;font:13px/1.4 monospace;" +
        "padding:8px 12px;border-radius:8px;pointer-events:none";
      document.documentElement.appendChild(el);
    }
    el.textContent =
      "session-updates WS dial #" + window.__updatesDials.length +
      " \\u2014 page " + (hidden ? "HIDDEN" : "visible");
  };

  Object.defineProperty(Document.prototype, "hidden", {
    configurable: true,
    get: () => hidden,
  });
  Object.defineProperty(Document.prototype, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  window.__setHidden = (value) => {
    hidden = value;
    document.dispatchEvent(new Event("visibilitychange"));
    badge();
  };

  window.__updatesDials = [];
  const OriginalWebSocket = window.WebSocket;
  window.WebSocket = new Proxy(OriginalWebSocket, {
    construct(target, args) {
      const url = String(args[0]);
      if (url.includes("/v1/sessions/updates")) {
        window.__updatesDials.push({ t: Date.now(), hidden });
        badge();
        // Dead local port: the connect is refused immediately, driving the
        // client's normal onerror/onclose -> reconnect-backoff path.
        return Reflect.construct(target, ["ws://127.0.0.1:1/v1/sessions/updates"]);
      }
      return Reflect.construct(target, args);
    },
  });
})();
"""


def test_hidden_page_slows_session_updates_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A hidden page must not keep redialing the updates socket every <=5 s.

    Journey: open a session -> the session-updates socket cannot reach the
    server (refused connect) and enters its reconnect loop -> the page is
    hidden (backgrounded tab / backgrounded WebView shell) -> reconnect
    attempts must slow down instead of continuing at the 5 s foreground cap.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.add_init_script(_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible()

    # Let the reconnect loop ramp through its exponential backoff to the 5 s
    # cap (>= 6 refused dials) while the page is still visible, so the hidden
    # window below measures steady-state cadence, not the fast early ramp.
    page.wait_for_function("() => window.__updatesDials.length >= 6", timeout=60_000)

    # Background the page: the socket module sees document.hidden === true
    # and a visibilitychange event, exactly like a backgrounded tab.
    page.evaluate("() => window.__setHidden(true)")
    dials_before = page.evaluate("() => window.__updatesDials.length")

    page.wait_for_timeout(16_000)

    hidden_dials = page.evaluate("() => window.__updatesDials.length") - dials_before
    assert hidden_dials <= 1, (
        f"session-updates socket dialed {hidden_dials} times in 16 s while the "
        "page was hidden (foreground 5 s reconnect cap applied in the "
        "background); expected at most one residual dial from a timer armed "
        "before the page hid"
    )
