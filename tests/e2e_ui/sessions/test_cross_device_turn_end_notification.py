"""Cross-device turn-end notification behavior.

A user with the app open on two devices sends a message from their laptop
and watches the agent finish there (session open, window focused the whole
time). Their phone — same account, app loaded and backgrounded — must NOT
raise an "agent finished" OS notification for that turn: the user initiated
it and actively watched it complete on another device.

Each client decides notifications locally: ``useIdleNotifications`` diffs
the conversations cache for ``running`` -> ``idle`` edges and suppresses
the conversation *this* window is focused on. The cross-device signal is
the per-viewer read state: the watching device keeps raising the session's
``viewer_last_seen`` past ``updated_at`` (``useMarkConversationSeen`` ->
``PUT /read-state``), and the server redistributes it to the user's other
clients over the list and ``WS /v1/sessions/updates``, which must keep the
phone quiet.

This test reproduces that journey with two real browser clients against the
live server:

- **laptop** — a desktop-profile context that opens the session, sends a
  real prompt, and stays foregrounded on it until the turn completes;
- **phone** — an iPhone-class mobile-profile context on the same account
  with the app loaded and backgrounded (the in-pocket state).

The turn is held open on the mock LLM's gate until BOTH clients have
observed the session ``running`` (the phone via its own traffic — the
updates WebSocket pushes watched-session diffs on a ~4 s rescan), then
released. That makes the phone deterministically observe the full
``running`` -> ``idle`` cycle exactly like a real multi-second turn,
without racing the mock model's sub-second reply.

What the test controls (same probe pattern as
``tests/e2e_ui/sessions/test_idle_notifications.py``):

- ``window.Notification`` is replaced with a recording stub, because
  Playwright cannot observe a real OS notification surface;
- ``document.visibilityState`` / ``hasFocus()`` are made controllable via
  ``window.__hidden`` so the phone can be put into the backgrounded state;
- the session statuses each client observed are recorded off the app's own
  traffic: ``/v1/sessions`` list responses, the active session's
  ``/stream`` SSE events, and ``WS /v1/sessions/updates`` frames.

EXPECTED (asserted) BEHAVIOR: the phone records no notification for the
session. A failure means the phone fired the session's turn-end
notification (tag ``omnigent:session:<id>``) about 10 s (the client's
idle-settle window) after the turn the user sent and watched on the laptop
completed — the cross-device suppression regressed.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid

import httpx
from playwright.sync_api import Browser, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm

# Records constructed notifications, makes visibility/focus controllable via
# window.__hidden, and records every session status this client observes
# through the app's own traffic: /v1/sessions list responses, the active
# session's /stream SSE events, and WS /v1/sessions/updates frames. Runs
# before any app script on every navigation (add_init_script), so the SPA's
# feature detection and permission read see the stub, not the real
# (unobservable) API.
_PROBE_INIT_SCRIPT = """
window.__notifs = [];
window.__hidden = false;
window.__sessionStatuses = [];
const __origFetch = window.fetch.bind(window);
function __recordStatuses(statuses, source) {
  window.__sessionStatuses.push({
    search: source,
    statuses,
    time: Date.now(),
  });
}
function __recordStreamStatuses(response, sessionId) {
  if (!response.body || !sessionId) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let currentEvent = null;
  function drain() {
    reader.read().then(({ done, value }) => {
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\\n");
      while (newline !== -1) {
        let line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.endsWith("\\r")) line = line.slice(0, -1);
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7);
        } else if (line.startsWith("data: ") && currentEvent !== null) {
          try {
            const data = JSON.parse(line.slice(6));
            if (currentEvent === "session.status" && typeof data.status === "string") {
              const statuses = {};
              statuses[sessionId] = data.status;
              __recordStatuses(statuses, "stream");
            }
          } catch (_) {}
          currentEvent = null;
        }
        newline = buffer.indexOf("\\n");
      }
      drain();
    }).catch(() => {});
  }
  drain();
}
window.fetch = async function(input, init) {
  const response = await __origFetch(input, init);
  try {
    const rawUrl =
      typeof input === "string" ? input : input && input.url ? input.url : "";
    const url = new URL(rawUrl, window.location.href);
    const contentType = response.headers.get("content-type") || "";
    if (url.pathname === "/v1/sessions" && contentType.includes("application/json")) {
      response.clone().json().then((body) => {
        const rows = Array.isArray(body && body.data) ? body.data : [];
        const statuses = {};
        for (const row of rows) statuses[row.id] = row.status;
        __recordStatuses(statuses, url.search || "list");
      }).catch(() => {});
    } else {
      const match = url.pathname.match(/^\\/v1\\/sessions\\/([^/]+)\\/stream$/);
      if (match) __recordStreamStatuses(response.clone(), decodeURIComponent(match[1]));
    }
  } catch (_) {}
  return response;
};
// WS /v1/sessions/updates is how a client NOT viewing the session (the
// phone) observes watched-session status changes; record snapshot/changed
// frames into the same observation log as the fetch/SSE probes.
const __OrigWebSocket = window.WebSocket;
function __ProbeWebSocket(url, protocols) {
  const ws = protocols === undefined
    ? new __OrigWebSocket(url)
    : new __OrigWebSocket(url, protocols);
  try {
    const parsed = new URL(url, window.location.href);
    if (parsed.pathname === "/v1/sessions/updates") {
      ws.addEventListener("message", (event) => {
        try {
          const frame = JSON.parse(event.data);
          if (
            (frame.type === "snapshot" || frame.type === "changed") &&
            Array.isArray(frame.items)
          ) {
            const statuses = {};
            let any = false;
            for (const item of frame.items) {
              if (item && typeof item.id === "string" && typeof item.status === "string") {
                statuses[item.id] = item.status;
                any = true;
              }
            }
            if (any) __recordStatuses(statuses, "ws");
          }
        } catch (_) {}
      });
    }
  } catch (_) {}
  return ws;
}
__ProbeWebSocket.prototype = __OrigWebSocket.prototype;
__ProbeWebSocket.CONNECTING = __OrigWebSocket.CONNECTING;
__ProbeWebSocket.OPEN = __OrigWebSocket.OPEN;
__ProbeWebSocket.CLOSING = __OrigWebSocket.CLOSING;
__ProbeWebSocket.CLOSED = __OrigWebSocket.CLOSED;
window.WebSocket = __ProbeWebSocket;
class FakeNotification {
  constructor(title, options) {
    this.title = title;
    this.options = options || {};
    this.onclick = null;
    window.__notifs.push({ title: title, options: options || {} });
  }
  close() {}
  static permission = "granted";
  static requestPermission(cb) {
    if (typeof cb === "function") { cb("granted"); return; }
    return Promise.resolve("granted");
  }
}
window.Notification = FakeNotification;
Object.defineProperty(document, "visibilityState", {
  configurable: true,
  get() { return window.__hidden ? "hidden" : "visible"; },
});
Object.defineProperty(document, "hidden", {
  configurable: true,
  get() { return window.__hidden; },
});
document.hasFocus = function () { return !window.__hidden; };
"""

# iPhone-12/13-class portrait viewport — the repo's mobile-profile
# convention (see tests/e2e_ui/mobile/).
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# The client defers a turn-end notification by IDLE_SETTLE_MS (10s in
# web/src/hooks/useIdleNotifications.ts) before showing it. Wait through
# that window plus a wide margin: if the phone is going to notify, it does
# so ~10s after it observed idle; staying quiet for the full grace proves
# it will not.
_IDLE_SETTLE_GRACE_MS = 30_000


def _wait_for_mock_gate_pending(mock_llm_server_url: str, *, timeout_s: float = 30.0) -> None:
    """Poll the mock LLM until a request is actually blocked on its gate.

    A session flips to ``running`` the moment the turn is dispatched to the
    runner, which is BEFORE the runner's harness has issued the LLM request
    and blocked on the mock's gate. Releasing off the ``running`` status
    alone races that window (the release finds nothing pending, then the
    request arrives and blocks with no one to free it). Synchronize on the
    gate itself instead.

    :param mock_llm_server_url: Mock LLM server base URL.
    :param timeout_s: Max seconds to wait for a request to reach the gate.
    :raises AssertionError: If no request blocks on the gate in time.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0, trust_env=False)
        if resp.json().get("pending") is True:
            return
        time.sleep(0.25)
    raise AssertionError("mock LLM turn never blocked on its gate")


def _wait_for_observed_session_status(
    page: Page,
    session_id: str,
    status: str,
    *,
    timeout: int,
) -> None:
    """
    Wait until this client's own app traffic reported a session status.

    The probe records real ``/v1/sessions`` list responses, the active
    session stream's ``session.status`` SSE events, and
    ``WS /v1/sessions/updates`` frames — the same sources that update the
    conversations cache ``useIdleNotifications`` consumes. Waiting here
    proves this browser client observed the transition for itself.

    :param page: Playwright page (laptop or phone client).
    :param session_id: Seeded session id.
    :param status: Expected session status, e.g. ``"running"``.
    :param timeout: Playwright wait timeout in milliseconds.
    """
    page.wait_for_function(
        """([sessionId, expected]) => {
          return (window.__sessionStatuses || []).some((entry) => {
            return entry.statuses && entry.statuses[sessionId] === expected;
          });
        }""",
        arg=[session_id, status],
        timeout=timeout,
    )


def test_phone_not_notified_for_turn_sent_and_watched_on_laptop(
    browser: Browser,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Sending a message from the laptop and watching the turn finish there
    must not raise an OS notification on the user's phone.

    Journey: phone has the app loaded and backgrounded → laptop opens the
    session, sends a message, and stays foregrounded on it through the whole
    turn → the turn completes → the phone must stay quiet.

    A failure here is the reported bug: the phone client independently
    observed the ``running`` -> ``idle`` edge and, after its idle-settle
    window, fired the session's turn-end notification even though the user
    sent and actively watched that turn on the laptop.

    :param browser: Playwright browser (contexts are created per device).
    :param seeded_session: ``(base_url, session_id)`` of a real session
        bound to the spawned runner.
    :param mock_llm_server_url: Mock LLM server URL; the reply is held on
        its gate so the turn spans both clients' observation windows.
    """
    base_url, session_id = seeded_session
    tag = f"omnigent:session:{session_id}"
    marker = f"watched-turn-{uuid.uuid4().hex[:8]}"

    # Hold the reply on the mock's gate so the session stays ``running``
    # until both clients have observed it. Content-routed by the unique
    # marker so no other turn can steal (or be stolen by) this queue.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Greeting delivered.", "block": True}],
        key=f"watched-turn-{uuid.uuid4().hex[:8]}",
        match=marker,
    )

    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    laptop_context = browser.new_context(record_video_dir=record_dir)
    phone_context = browser.new_context(
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=record_dir,
    )
    try:
        laptop = laptop_context.new_page()
        laptop.add_init_script(_PROBE_INIT_SCRIPT)
        phone = phone_context.new_page()
        phone.add_init_script(_PROBE_INIT_SCRIPT)

        # Phone: open the app, let it load and observe the seeded session
        # (its initial /v1/sessions read), then background it — the
        # in-pocket state a phone is in while its owner works on the laptop.
        phone.goto(f"{base_url}/")
        phone.wait_for_function(
            "(sid) => (window.__sessionStatuses || []).some("
            "(entry) => entry.statuses && sid in entry.statuses)",
            arg=session_id,
            timeout=30_000,
        )
        phone.evaluate(
            "window.__hidden = true;"
            "document.dispatchEvent(new Event('visibilitychange'));"
            "window.dispatchEvent(new Event('blur'));"
        )

        # Laptop: open the session and send the message. The user stays
        # here, foregrounded on the session, watching the whole turn.
        laptop.goto(f"{base_url}/c/{session_id}")
        laptop.mouse.click(5, 5)  # the real permission flow rides a user gesture
        composer = laptop.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(f"[{marker}] Reply with a one-sentence greeting and nothing else.")
        laptop.get_by_role("button", name="Send", exact=True).click()

        # The turn's LLM request reaches the mock and blocks on its gate,
        # holding the session ``running`` until we release it.
        _wait_for_mock_gate_pending(mock_llm_server_url)

        # Both clients observe the turn running while it's held open — the
        # laptop off its session stream/list reads, the phone off its
        # updates-WS push. The gate guarantees neither wait races the model.
        _wait_for_observed_session_status(laptop, session_id, "running", timeout=30_000)
        _wait_for_observed_session_status(phone, session_id, "running", timeout=30_000)

        # Let the turn finish; both clients observe idle.
        release = httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)
        assert release.json().get("released") is True, "mock gate had no pending turn to release"
        _wait_for_observed_session_status(laptop, session_id, "idle", timeout=90_000)
        _wait_for_observed_session_status(phone, session_id, "idle", timeout=90_000)

        # The reported bug: the phone raises the turn-end OS notification
        # for a turn the user sent and watched to completion on the laptop.
        # Wait through the client's idle-settle window plus margin; the
        # phone must stay quiet the entire time.
        fired: object | None = None
        with contextlib.suppress(PlaywrightTimeoutError):
            phone.wait_for_function(
                "(tag) => (window.__notifs || []).some("
                "(n) => n.options && n.options.tag === tag)",
                arg=tag,
                timeout=_IDLE_SETTLE_GRACE_MS,
            )
            fired = phone.evaluate("window.__notifs")
        assert fired is None, (
            "phone client must not raise an OS notification for a turn the "
            "user sent and actively watched on the laptop; "
            f"phone recorded: {fired}"
        )
    finally:
        # Never leave the shared runner blocked on the gate, and clear this
        # test's content-routed queue, whatever happened above.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)
        with contextlib.suppress(httpx.HTTPError):
            reset_mock_llm(mock_llm_server_url)
        laptop_context.close()
        phone_context.close()
