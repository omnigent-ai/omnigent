"""Idle browser-notification path: agent finishes while the tab is backgrounded.

The web UI raises an OS notification when a session finishes a turn
(``running`` -> ``idle``) *while the user isn't looking at the tab*. The
signal is derived client-side from the conversations cache
(``useConversations`` -> ``useIdleNotifications``); the OS-level
``Notification`` constructor is the only browser API involved.

These tests drive a **real** session end-to-end via the ``seeded_session``
fixture: a genuine prompt is sent, the server's relay-fed status cache
moves the session ``running`` -> ``idle`` as the real LLM turn completes,
and the browser observes that transition through the real app traffic.
Nothing about the session or its status is faked.

What the test *does* control:

- ``window.Notification`` is replaced with a recording stub via an init
  script, because Playwright cannot observe a real OS notification
  surface. The stub records each constructed notification so the test can
  assert on title/body/tag.
- ``document.visibilityState`` / ``hasFocus()`` are made controllable so
  the test can put the tab into the backgrounded state the notification is
  gated on (and that the foreground-suppression case requires).

The client-observed status is read off real browser traffic made by the
app: ``/v1/sessions`` list responses and the active session's
``/v1/sessions/{id}/stream`` SSE events. ``useIdleNotifications`` consumes
the conversations cache that these signals update, so the test
synchronizes on the same status source that drives notifications instead
of on wall-clock guesses or sidebar badges that are hidden for some row
groups.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# Records constructed notifications and makes visibility/focus controllable
# from the test via window.__hidden. Runs before any app script on every
# navigation (add_init_script), so the SPA's feature detection and
# permission read see the stub, not the real (unobservable) API.
_HARNESS_INIT_SCRIPT = """
window.__notifs = [];
// The live notification instances (kept out of __notifs, which is read via
// page.evaluate and so must stay JSON-serializable). The click test invokes
// __notifObjects[i].onclick() to exercise the app's click->navigate handler.
window.__notifObjects = [];
window.__hidden = false;
window.__sessionStatuses = [];
window.__streamStatuses = [];
const __origFetch = window.fetch.bind(window);
function __recordSessionStatus(sessionId, status, source) {
  const statuses = {};
  statuses[sessionId] = status;
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
              window.__streamStatuses.push({
                sessionId,
                status: data.status,
                time: Date.now(),
              });
              __recordSessionStatus(sessionId, data.status, "stream");
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
        window.__sessionStatuses.push({
          search: url.search,
          statuses,
          time: Date.now(),
        });
      }).catch(() => {});
    } else {
      const match = url.pathname.match(/^\\/v1\\/sessions\\/([^/]+)\\/stream$/);
      if (match) __recordStreamStatuses(response.clone(), decodeURIComponent(match[1]));
    }
  } catch (_) {}
  return response;
};
class FakeNotification {
  constructor(title, options) {
    this.title = title;
    this.options = options || {};
    this.onclick = null;
    window.__notifs.push({ title: title, options: options || {} });
    window.__notifObjects.push(this);
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

# Kept deliberately tiny: these tests only need a real ``running`` ->
# ``idle`` turn to observe, not a long generation. A short reply still
# exercises the notification body's preview contract (non-empty, capped)
# while cutting the LLM turn from ~15s of essay tokens to a couple of
# seconds -- the dominant cost in this suite's slow shard.
_PROMPT = "Reply with a one-sentence greeting and nothing else."


def _reset_session_status_probe(page: Page) -> None:
    """
    Clear recorded status observations before sending a prompt.

    :param page: Playwright page.
    """
    page.evaluate("window.__sessionStatuses = []")
    page.evaluate("window.__streamStatuses = []")


def _wait_for_observed_session_status(
    page: Page,
    session_id: str,
    status: str,
    *,
    timeout: int,
) -> None:
    """
    Wait until app-owned browser traffic reports a session status.

    The probe wraps ``window.fetch`` before the SPA loads and records real
    ``/v1/sessions`` list responses plus ``session.status`` events from the
    active session stream. Waiting here proves the browser observed the
    transition through a source that updates the conversations cache that
    ``useIdleNotifications`` consumes.

    :param page: Playwright page.
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


def _send_prompt(page: Page) -> None:
    """
    Type the standard prompt into the composer and click Send.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()


def test_idle_notification_fires_when_backgrounded(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """
    A real ``running`` -> ``idle`` turn observed while the tab is
    backgrounded raises exactly one OS notification for the session.

    Flow: open the seeded session, send a real prompt, wait until app
    traffic reports the session as ``running`` (which seeds
    ``useIdleNotifications``' baseline), background the tab, then wait for
    the real turn to finish. The browser-observed transition fires the
    notification; the test asserts on its title, body, and per-session
    dedupe tag.

    A failure means the browser did not observe the real status transition
    or the transition/gate logic in ``useIdleNotifications`` broke.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a real session
        bound to the spawned runner.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_HARNESS_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    # A user gesture mirrors the real lazy-permission flow; harmless here
    # since the stub already reports "granted".
    page.mouse.click(5, 5)

    _reset_session_status_probe(page)
    _send_prompt(page)

    # Synchronize on the browser actually seeing the turn start. This is
    # what seeds the prev-status baseline, so we must reach it before
    # backgrounding.
    _wait_for_observed_session_status(page, session_id, "running", timeout=30_000)

    # Background the tab: this gates the notification.
    page.evaluate(
        "window.__hidden = true;"
        "document.dispatchEvent(new Event('visibilitychange'));"
        "window.dispatchEvent(new Event('blur'));"
    )

    # Wait for the real turn to finish and the backgrounded transition to
    # fire the notification. 90s budget covers cold-start LLM latency under
    # Databricks routing without masking a true hang.
    page.wait_for_function("window.__notifs.length > 0", timeout=90_000)
    _wait_for_observed_session_status(page, session_id, "idle", timeout=90_000)
    # One more observation window catches duplicate-notification
    # regressions: the single running -> idle transition should produce
    # exactly one notification. A duplicate fires off a re-render right
    # after the first, so a short settle is enough to catch it.
    page.wait_for_timeout(3_000)
    notifs = page.evaluate("window.__notifs")
    assert len(notifs) == 1, f"expected exactly one notification, got {notifs}"
    first = notifs[0]
    assert first["title"] == _PROMPT, notifs
    # Body contract: the agent's final words as a trimmed,
    # capped preview when the best-effort fetch succeeds, else the
    # generic fallback. The preview text is real LLM output, so assert
    # the contract rather than exact content: non-empty either way, and
    # a non-fallback body must respect the preview caps
    # (``previewText`` in web/src/lib/lastAssistantText.ts:
    # ≤160 chars including the "…" elision marker, ≤3 lines).
    body = first["options"]["body"]
    assert isinstance(body, str) and body.strip(), notifs
    if body != "Agent finished and is ready for your input.":
        assert len(body) <= 160, f"preview exceeds its 160-char cap: {notifs}"
        assert body.count("\n") <= 2, f"preview exceeds its 3-line cap: {notifs}"
    assert first["options"]["tag"] == f"omnigent:session:{session_id}", notifs


def test_idle_notification_suppressed_when_foreground(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """
    A real ``running`` -> ``idle`` turn observed while the tab stays
    foregrounded must NOT raise a notification (the sidebar blue dot
    already covers the foreground case).

    Flow: open the seeded session, send a real prompt, keep the tab
    visible, wait for the turn to fully complete (app traffic reports
    ``idle``), then assert no notification was constructed.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` of a real session
        bound to the spawned runner.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_HARNESS_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    page.mouse.click(5, 5)

    _reset_session_status_probe(page)
    _send_prompt(page)

    # Observe the full running -> idle cycle while staying foreground.
    _wait_for_observed_session_status(page, session_id, "running", timeout=30_000)
    _wait_for_observed_session_status(page, session_id, "idle", timeout=90_000)

    # Leave a short post-transition window to be sure no delayed
    # notification slips through.
    page.wait_for_timeout(3_000)
    assert page.evaluate("window.__notifs.length") == 0, "foreground transition must not notify"


def test_idle_notification_click_navigates_to_chat(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """
    Clicking the OS notification routes into the session it was raised for.

    This is the user-facing contract behind the notification's click
    handler: ``useIdleNotifications`` builds ``/c/{id}`` and wires it as
    the notification's ``onClick`` (and, under the desktop shell, as the
    ``navigatePath`` forwarded over IPC). The browser path runs that
    closure directly; this test exercises it end-to-end.

    Flow: open the seeded session, send a real prompt, wait until app
    traffic reports it ``running`` (seeding the baseline), then navigate
    AWAY to the new-session screen ("/") via the in-app sidebar link — a
    client-side navigation that keeps ``useIdleNotifications`` mounted, and
    leaves no conversation actively viewed so the turn-end still notifies.
    When the real turn finishes the notification fires; invoking its
    ``onclick`` must route the app back to ``/c/{session_id}``.

    A failure means the notification's click handler no longer navigates to
    its conversation (the desktop "click does nothing but focus" bug, or a
    regression in the shared path-building wiring).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a real session
        bound to the spawned runner.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_HARNESS_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    # User gesture mirrors the real lazy-permission flow (stub reports
    # "granted", so this is harmless).
    page.mouse.click(5, 5)

    _reset_session_status_probe(page)
    _send_prompt(page)

    # Reach the running baseline while still viewing the session, then leave
    # for the new-session screen so the turn-end isn't suppressed as
    # actively-viewed and a click has somewhere to navigate FROM.
    _wait_for_observed_session_status(page, session_id, "running", timeout=30_000)
    page.get_by_test_id("new-chat-button").click()
    page.wait_for_url(lambda url: f"/c/{session_id}" not in url, timeout=10_000)

    # The real turn completes off-screen and raises the notification.
    page.wait_for_function("window.__notifObjects.length > 0", timeout=90_000)

    # Click it: the app's onClick focuses then navigates to the session.
    page.evaluate("window.__notifObjects[0].onclick()")
    page.wait_for_url(f"**/c/{session_id}", timeout=10_000)
