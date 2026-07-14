"""E2E: the sidebar session-scope tabs stay inside their background on narrow widths.

Regression guard for the sidebar tab overflow fix. The "My sessions" /
"Shared with me" tabs are ``flex-1`` triggers inside a ``TabsList`` that draws
the rounded ``bg-muted`` background strip. The triggers carry ``whitespace-nowrap``
labels; without ``min-w-0`` a flex item keeps its ``min-width: auto`` intrinsic
size and refuses to shrink, so on a narrow sidebar the two nowrap labels spill
past the strip's right edge — the tabs render outside their own background.
Adding ``min-w-0`` (plus a truncating label span) lets them shrink to fit.

The tabs only render on a multi-user server (``!isCurrentServerLocal()``), so
the page is served through the public-looking loopback alias — a loopback host
would hide the tabs entirely. The sidebar width is pinned to its floor via the
persisted ``sidebarWidthPx`` preference and a narrow viewport, then the test
asserts the observable invariant: each trigger's right edge sits within the
``TabsList`` box (``getBoundingClientRect``), i.e. the tab does not overflow its
background. Comparing geometry rather than class names keeps the test tied to
what the user actually sees. This fails before the fix (the trigger overflows)
and passes after it.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _PUBLIC_LOOPBACK_HOST

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_TAB_MINE = '[data-testid="sidebar-tab-mine"]'
_TAB_SHARED = '[data-testid="sidebar-tab-shared"]'

# A viewport narrow enough that the sidebar sits at its resize floor (220px, see
# useResizableSidebar) — the width at which the un-fixed tabs overflow. Still
# desktop (>= 768px) so the sidebar stays an inline rail, not the mobile overlay.
_NARROW_VIEWPORT = {"width": 800, "height": 720}

# Force the persisted sidebar width to the narrowest the hook allows, so the
# strip is as tight as it ever gets regardless of any prior stored preference.
_PIN_NARROW_SIDEBAR = """
window.localStorage.setItem(
  "omnigent:panel-size-preferences",
  JSON.stringify({ sidebarWidthPx: 220 })
);
"""

# Does a trigger overflow its TabsList horizontally (beyond a 1px rounding
# tolerance)? Reads the live layout boxes the user sees. Returns true when the
# trigger extends past either edge of the list that draws its background. The
# trigger's own enclosing list is found via ``closest`` — the page has other
# TabsList strips (the workspace rail's Files/Agents pills), so a global lookup
# would be ambiguous.
_OVERFLOWS = """
(sel) => {
  const trigger = document.querySelector(sel);
  const list = trigger && trigger.closest('[data-slot="tabs-list"]');
  if (!trigger || !list) return null;
  const t = trigger.getBoundingClientRect();
  const l = list.getBoundingClientRect();
  return t.right - l.right > 1 || l.left - t.left > 1;
}
"""


def _public_loopback_url(base_url: str) -> str:
    """Return *base_url* through the browser's public-looking loopback alias.

    The tabs only render on a multi-user server; a loopback origin reads as
    local and hides them. The alias resolves to 127.0.0.1 (see the
    ``--host-resolver-rules`` browser flag) but presents a non-loopback
    hostname, so ``isCurrentServerLocal()`` is false.
    """
    parsed = urlsplit(base_url)
    if parsed.port is None:
        raise AssertionError(f"e2e base URL missing port: {base_url!r}")
    return urlunsplit((parsed.scheme, f"{_PUBLIC_LOOPBACK_HOST}:{parsed.port}", "", "", ""))


def test_sidebar_tabs_do_not_overflow_background_when_narrow(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The session-scope tabs stay within their background strip at min width.

    Fails before the fix: the ``whitespace-nowrap`` triggers can't shrink and
    overflow the ``TabsList`` (their rounded background). Passes after ``min-w-0``
    lets them shrink and truncate inside the strip.
    """
    base_url, session_id = seeded_session
    public_base_url = _public_loopback_url(base_url)

    page.set_viewport_size(_NARROW_VIEWPORT)
    page.add_init_script(_PIN_NARROW_SIDEBAR)
    page.goto(f"{public_base_url}/c/{session_id}")

    # Multi-user origin → the tabs render. Wait for both before measuring.
    sidebar = page.locator(_CONVERSATIONS)
    expect(sidebar).to_be_visible(timeout=30_000)
    expect(page.locator(_TAB_MINE)).to_be_visible(timeout=30_000)
    expect(page.locator(_TAB_SHARED)).to_be_visible()

    # Neither tab may extend past the background strip that the TabsList draws.
    assert page.evaluate(_OVERFLOWS, _TAB_MINE) is False, (
        "the 'My sessions' tab overflows its TabsList background at narrow width"
    )
    assert page.evaluate(_OVERFLOWS, _TAB_SHARED) is False, (
        "the 'Shared with me' tab overflows its TabsList background at narrow width"
    )
