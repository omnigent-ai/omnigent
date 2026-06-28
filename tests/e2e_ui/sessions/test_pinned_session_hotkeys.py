"""Desktop-only pinned-session hotkeys: Cmd/Ctrl+digit jumps to a pinned row.

The sidebar binds ``Cmd+1..9/0`` (``Ctrl`` on Win/Linux) to the first ten
pinned sessions in render order — 1–9 to the first nine, 0 to the tenth
(``usePinnedSessionHotkeys``). It is **desktop-only**: a browser tab
reserves ``Cmd/Ctrl+digit`` for native tab-switching, so the hook is inert
outside the Electron shell (gated on ``isNativeShell()`` ->
``window.omnigentDesktop.kind === "electron"`` — see
``ap-web/src/lib/nativeBridge.ts``).

The e2e_ui harness runs the SPA in a plain Chromium browser, not Electron,
so by default ``isNativeShell()`` is false and this behavior can't fire. To
exercise it end-to-end we inject a minimal ``window.omnigentDesktop`` stub
via ``add_init_script`` *before any app script runs* — the same
feature-detection-stubbing pattern ``test_idle_notifications.py`` uses for
the OS-notification path. With the stub in place the SPA believes it is
under the desktop shell, so the keydown handler routes.

These drive the real chain the ``usePinnedSessionHotkeys`` unit tests mock
out: the live ``GET /v1/sessions`` list -> ``useConversations`` -> the
Pinned section peel -> the window keydown handler -> a client-side
navigation to ``/c/{id}``. A regression in the shell gate, the digit->slot
mapping, or the list shape would surface here.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect

# Minimal stand-in for the Electron preload bridge. Runs before any app
# script on every navigation (add_init_script), so the SPA's feature
# detection (`electronApi()` in nativeBridge.ts, which checks
# `window.omnigentDesktop?.kind === "electron"`) sees a native shell. Every
# method the web layer may call is a guarded no-op: `kind` is all the
# pinned-hotkey path needs, and the rest keep unrelated native calls (badge,
# notify, the title-bar server picker) from throwing under the stub.
_NATIVE_SHELL_INIT_SCRIPT = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  getServerPicker: function () { return Promise.resolve(null); },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
};
"""


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}``.

    The seeded session has no title (renders as "New session"), which is
    ambiguous when other tests' sessions accumulate in the shared server.
    A unique title makes each pinned row trivially identifiable.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id to rename.
    :param title: The new title to set.
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose header reads *title*.

    Each ``ConversationSection`` renders an ``<h2>`` with a collapse button
    whose accessible name is the section title (e.g. "Pinned"). Scoping
    lookups to the matching section keeps them off the "Chats" rows.

    :param page: Playwright page with the sidebar open.
    :param title: Section header text, e.g. ``"Pinned"``.
    :returns: A locator for the section element.
    """
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _pin(page: Page, session_id: str) -> None:
    """Pin *session_id* via its hover-revealed quick action.

    Mirrors the user gesture exercised by ``test_sidebar_pin_unpin``: hover
    the row so the desktop hover-revealed control is interactable, click it,
    and wait until the row has moved under the "Pinned" header.

    :param page: Playwright page already showing the sidebar.
    :param session_id: The conversation id to pin.
    """
    row = _row(page, session_id)
    expect(row).to_be_visible()
    row.hover()
    pin_button = row.get_by_test_id("quick-pin-conversation")
    expect(pin_button).to_have_attribute("aria-label", "Pin conversation")
    pin_button.click()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_be_visible()


def _pinned_slot_ids(page: Page) -> list[str]:
    """Return the pinned conversation ids in sidebar render order.

    The Nth entry (0-based) is the row the hotkey jumps to with digit
    ``PINNED_HOTKEY_DIGITS[N]`` (1..9 then 0). Reading the live DOM order
    rather than assuming an insertion order keeps the digit->slot assertions
    honest to what the user actually sees.

    :param page: Playwright page with the Pinned section rendered.
    :returns: Ordered ``/c/{id}`` -> ``id`` for each pinned row.
    """
    hrefs = (
        _section(page, "Pinned")
        .locator('a[href^="/c/"]')
        .evaluate_all("els => els.map(e => e.getAttribute('href'))")
    )
    return [h[len("/c/") :] for h in hrefs]


def test_pinned_hotkey_navigates_under_native_shell(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Under the desktop shell, Cmd/Ctrl+<digit> jumps to that pinned slot.

    Pins two sessions, leaves the open conversation for the new-session
    screen ("/") so a jump is observable, then presses the platform modifier
    + the digit for each pinned slot and asserts the route lands on that
    slot's session.

    Catches regressions the mocked unit tests can't: the live list shape
    drifting so a pinned row never reaches the Pinned section, or the
    digit->id mapping breaking against real render order.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` — two
        runner-bound sessions in the same server.
    """
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, f"e2e-hotkey-a-{uuid.uuid4().hex[:8]}")
    _set_title(base_url, session_b, f"e2e-hotkey-b-{uuid.uuid4().hex[:8]}")

    page.add_init_script(_NATIVE_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_a}")

    # Pin both so the Pinned section has two ordered slots.
    _pin(page, session_a)
    _pin(page, session_b)

    slots = _pinned_slot_ids(page)
    assert session_a in slots and session_b in slots, slots
    assert len(slots) == 2, f"expected exactly two pinned rows, got {slots}"

    # Leave the open session so a hotkey jump is an observable navigation.
    page.get_by_test_id("new-chat-button").click()
    page.wait_for_url(lambda url: f"/c/{session_a}" not in url, timeout=10_000)

    # Slot 1 (digit "1") -> the first pinned row. "ControlOrMeta" maps to the
    # real platform modifier the hook accepts (metaKey on macOS, ctrlKey
    # elsewhere); the keydown is dispatched at the body and bubbles to the
    # window listener the hook binds.
    page.locator("body").press("ControlOrMeta+1")
    page.wait_for_url(f"**/c/{slots[0]}", timeout=10_000)

    # Slot 2 (digit "2") -> the second pinned row, from a different active
    # session — proves the jump isn't a no-op when already on a pinned page.
    page.locator("body").press("ControlOrMeta+2")
    page.wait_for_url(f"**/c/{slots[1]}", timeout=10_000)


def test_pinned_hotkey_inert_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """In a plain browser tab the hotkey does nothing.

    No native-shell stub: ``isNativeShell()`` is false, so the SPA must not
    hijack ``Cmd/Ctrl+digit`` (which the browser reserves for tab-switching).
    Pins a session, navigates to the new-session screen, presses the
    modifier+1, and asserts the route did NOT jump to the pinned session.

    This is the half of the contract that only an end-to-end browser run can
    prove — the gate that keeps the feature desktop-only.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound
        session.
    """
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, f"e2e-hotkey-off-{uuid.uuid4().hex[:8]}")

    page.goto(f"{base_url}/c/{session_id}")
    _pin(page, session_id)

    # Leave the session; if the hotkey fired it would jump straight back.
    page.get_by_test_id("new-chat-button").click()
    page.wait_for_url(lambda url: f"/c/{session_id}" not in url, timeout=10_000)
    landed = page.url

    page.locator("body").press("ControlOrMeta+1")
    # Give any (erroneous) navigation a beat to occur, then assert we stayed
    # put: the inert hook must not route to the pinned conversation.
    page.wait_for_timeout(500)
    assert f"/c/{session_id}" not in page.url, (
        f"plain-browser Cmd/Ctrl+1 must not jump to the pinned session "
        f"(was {landed!r}, now {page.url!r})"
    )
