"""The mobile header switcher layers must fit inside its glass pill.

On a phone (iOS report; any ``max-md`` viewport) the header's right-hand control
cluster is a fully-rounded "liquid glass" pill (``MOBILE_GLASS_PILL`` in
``web/src/shell/mobileGlass.ts``). For terminal-first sessions that pill holds
the Chat/Terminal switcher (``web/src/shell/ViewModeToggle.tsx``): a grey track
(``bg-muted/60``) around two segments, whose active segment renders as a
fully-circular 44px bubble on mobile (``max-md:size-11 max-md:rounded-full``).

The track's 8px rounded rectangle should remain visually distinct from the
outer capsule and selected circle, but its grey paint must be inset from the
glass edge instead of poking out beneath either round end.

This test drives the reported journey — open a terminal-first session at an
iPhone-sized viewport under the iOS shell bridge, tap between Terminal and Chat
in the header switcher — then measures the rendered geometry of the three
layers and asserts their separate roles: the glass pill and selected segment
stay capsules, the track element itself is transparent, and a full-height grey
8px-radius rounded rectangle paints 4px inside its horizontal ends. The
switcher and expanded kebab also keep symmetric glass rims at the pill's outer
edges.

The journey/bridge scaffolding mirrors ``test_ios_switcher_in_header.py`` (the
same terminal-first precondition and iOS WKWebView bridge stub), so the failure
this test reports can only be the shape mismatch, not a missing control.
"""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, Route, ViewportSize, expect

# iPhone-sized viewport (matches the report's surface and keeps the SPA in the
# max-md mobile header layout, where the glass pill and 44px segments render).
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}
_TRACK_HORIZONTAL_INSET_PX = 4
_TRACK_VERTICAL_INSET_PX = 0
_TRACK_RADIUS_PX = 8
_GEOMETRY_TOLERANCE_PX = 1.5
_MIN_GLASS_RIM_PX = 4
_RIM_SYMMETRY_TOLERANCE_PX = 1.5

# Minimal stand-in for the iOS WKWebView bridge (``window.omnigentNative``
# injected by ``web/ios``), copied from test_ios_switcher_in_header.py. Runs
# before any app script so ``isIOSShell()`` sees the iOS shell and the SPA
# renders its iOS chrome — the environment the ticket reports.
_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  setServerSwitcherHidden: function () {},
  setViewMode: function () {},
  onViewModeChanged: function () { return function () {}; },
  onNativeInsets: function (callback) {
    callback({ topBar: 36, bottomBar: 48 });
    return function () {};
  },
};
"""

# Measures the glass pill, transparent track element, inset ::before track
# paint, and active segment. Pseudo-elements have no DOMRect, so their box is
# derived from the track and computed inset values.
_MEASURE_LAYERS_JS = """
() => {
  const track = document.querySelector('[data-testid="view-mode-toggle"]');
  if (!track) return { error: "view-mode-toggle not found" };
  const pill = track.parentElement;
  const active = track.querySelector('button[aria-pressed="true"]');
  if (!active) return { error: "no active segment (aria-pressed=true)" };

  const describe = (name, el, pseudo = null) => {
    const cs = getComputedStyle(el, pseudo);
    const hostRect = el.getBoundingClientRect();
    const inset = pseudo ? {
      top: parseFloat(cs.top),
      right: parseFloat(cs.right),
      bottom: parseFloat(cs.bottom),
      left: parseFloat(cs.left),
    } : null;
    const rect = pseudo ? {
      width: hostRect.width - inset.left - inset.right,
      height: hostRect.height - inset.top - inset.bottom,
    } : hostRect;
    const radii = [
      "borderTopLeftRadius",
      "borderTopRightRadius",
      "borderBottomRightRadius",
      "borderBottomLeftRadius",
    ].map((key) => {
      const value = cs[key];
      if (value.endsWith("%")) {
        return (parseFloat(value) / 100) * Math.min(rect.width, rect.height);
      }
      // px values, including the enormous calc(infinity*1px) rounded-full
      // resolves to (serialized in scientific notation, which parseFloat
      // handles).
      return parseFloat(value);
    });
    const declared = Math.min(...radii);
    const capsuleRadius = Math.min(rect.width, rect.height) / 2;
    const effective = Math.min(declared, capsuleRadius);
    const bg = cs.backgroundColor;
    const alpha = (() => {
      if (bg === "transparent") return 0;
      const slash = bg.match(/\\/\\s*([\\d.]+%?)\\s*\\)$/); // oklch(... / 0.6)
      if (slash) {
        return slash[1].endsWith("%")
          ? parseFloat(slash[1]) / 100
          : parseFloat(slash[1]);
      }
      const rgba = bg.match(/^rgba\\([^)]*,\\s*([\\d.]+)\\s*\\)$/);
      if (rgba) return parseFloat(rgba[1]);
      return 1; // rgb() / named color: fully opaque
    })();
    return {
      name,
      width: rect.width,
      height: rect.height,
      declaredRadius: declared,
      effectiveRadius: effective,
      capsuleRadius,
      background: bg,
      alpha,
      inset,
      zIndex: cs.zIndex,
      // The track's grey is subtle by design — bg-muted/60 resolves to
      // ~3.5%-alpha black — yet clearly visible over the glass pill (it is
      // the "squared grey highlight" in the report), so "painted" must
      // catch even a low-alpha wash.
      painted: alpha > 0.01,
      capsule: effective >= capsuleRadius - 1.5,
    };
  };

  return {
    layers: {
      pill: describe("glass pill (header cluster)", pill),
      track: describe("switcher track element", track),
      trackPaint: describe("inset grey track paint", track, "::before"),
      active: describe("active segment", active),
    },
  };
}
"""

_MEASURE_EDGE_RIMS_JS = """
() => {
  const track = document.querySelector('[data-testid="view-mode-toggle"]');
  const menu = document.querySelector('[data-testid="header-conversation-actions"]');
  const pill = track?.parentElement;
  if (!track || !menu || !pill) return { error: "switcher pill controls not found" };
  const pillRect = pill.getBoundingClientRect();
  const trackRect = track.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  return {
    leading: trackRect.left - pillRect.left,
    trailing: pillRect.right - menuRect.right,
  };
}
"""


def _mark_terminal_first(base_url: str, session_id: str) -> None:
    """Stamp the session terminal-first (``omnigent.ui = terminal``).

    The Chat/Terminal switcher only exists for terminal-first sessions, so the
    label is the journey's precondition — the same one ``omnigent claude`` /
    ``omnigent codex`` sessions carry.

    :param base_url: Spawned server base URL.
    :param session_id: Session to label.
    """
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()


def _route_agent_terminal(page: Page, session_id: str) -> None:
    """Serve a deterministic agent terminal pane for the session.

    The switcher's shape does not need a live PTY, only the resource shape the
    runner publishes — mirrors ``test_ios_switcher_in_header.py``.

    :param page: Page whose network to intercept.
    :param session_id: Session whose terminals list to stub.
    """

    def _serve(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "terminal_tui_main",
                            "type": "terminal",
                            "session_id": session_id,
                            "name": "tui:main",
                            "metadata": {
                                "terminal_name": "tui",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": "terminal_tui_main",
                    "last_id": "terminal_tui_main",
                    "has_more": False,
                }
            ),
        )

    terminal_list = re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*")
    page.route(terminal_list, _serve)


def test_mobile_header_switcher_layers_fit_inside_the_glass_pill(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The switcher's distinct shapes must nest cleanly inside the glass pill.

    Journey (the report's): open a terminal-first session on an iPhone-sized
    iOS shell, look at the header's Chat/Terminal switcher, tap Terminal and
    then Chat. The glass pill and the circular selected bubble render as
    capsules; the grey track remains an inset 8px rounded rectangle rather
    than painting flush beneath either circular end.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    _mark_terminal_first(base_url, session_id)
    _route_agent_terminal(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.locator('textarea[aria-label="Message the agent"]')
    expect(composer).to_be_visible(timeout=60_000)

    # The bridge stub took: the SPA is running its iOS-shell chrome.
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=8_000)

    # Drive the switcher the way a user does — Terminal, then back to Chat —
    # so the selected bubble visibly moves inside the track (and the shapes
    # under test are the ones a user actually watches).
    page.get_by_test_id("view-mode-terminal").click()
    expect(page.get_by_test_id("view-mode-terminal")).to_have_attribute("aria-pressed", "true")
    page.wait_for_timeout(600)
    page.get_by_test_id("view-mode-chat").click()
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
    # Let the selected-bubble transition finish before measuring, and hold the
    # end state briefly so the rendered shapes are steady on screen.
    page.wait_for_timeout(1_200)

    measured = page.evaluate(_MEASURE_LAYERS_JS)
    assert "error" not in measured, f"could not measure switcher layers: {measured}"
    layers = measured["layers"]
    for layer in layers.values():
        print(
            f"[switcher-layers] {layer['name']}: "
            f"{layer['width']:.0f}x{layer['height']:.0f}px, "
            f"declared radius {layer['declaredRadius']:.1f}px, effective "
            f"{layer['effectiveRadius']:.1f}px of capsule {layer['capsuleRadius']:.1f}px, "
            f"bg {layer['background']} (alpha {layer['alpha']:.2f}) -> "
            f"painted={layer['painted']} capsule={layer['capsule']}"
        )

    pill = layers["pill"]
    track = layers["track"]
    track_paint = layers["trackPaint"]
    active = layers["active"]

    assert pill["painted"] and pill["capsule"], f"glass pill lost its capsule: {pill}"
    assert active["painted"] and active["capsule"], (
        f"selected switcher segment lost its circle: {active}"
    )
    assert not track["painted"], f"mobile track element must be transparent: {track}"
    assert track_paint["painted"], f"inset grey track paint is missing: {track_paint}"
    assert not track_paint["capsule"], (
        f"grey track should remain an inset rounded rectangle: {track_paint}"
    )
    assert abs(track_paint["height"] - active["height"]) <= _GEOMETRY_TOLERANCE_PX, (
        f"grey track and selected segment must remain equally tall: {layers}"
    )
    assert abs(track_paint["declaredRadius"] - _TRACK_RADIUS_PX) <= _GEOMETRY_TOLERANCE_PX, (
        f"grey track lost the {_TRACK_RADIUS_PX}px design-token radius: {track_paint}"
    )
    expected_insets = {
        "top": _TRACK_VERTICAL_INSET_PX,
        "right": _TRACK_HORIZONTAL_INSET_PX,
        "bottom": _TRACK_VERTICAL_INSET_PX,
        "left": _TRACK_HORIZONTAL_INSET_PX,
    }
    for edge, expected in expected_insets.items():
        inset = track_paint["inset"][edge]
        assert abs(inset - expected) <= _GEOMETRY_TOLERANCE_PX, (
            f"grey track {edge} inset is not {expected}px: {track_paint}"
        )

    # The expanded kebab paints its full 44px background, so it needs the same
    # visible glass rim as the switcher track at the opposite end.
    menu = page.get_by_test_id("header-conversation-actions")
    expect(menu).to_be_visible()
    menu.click()
    expect(menu).to_have_attribute("aria-expanded", "true")
    page.wait_for_timeout(300)

    rims = page.evaluate(_MEASURE_EDGE_RIMS_JS)
    assert "error" not in rims, f"could not measure switcher pill rims: {rims}"
    print(f"[pill-rims] leading={rims['leading']:.1f}px, trailing={rims['trailing']:.1f}px")
    assert rims["leading"] >= _MIN_GLASS_RIM_PX, f"switcher track lacks a glass rim: {rims}"
    assert rims["trailing"] >= _MIN_GLASS_RIM_PX, f"expanded kebab lacks a glass rim: {rims}"
    assert abs(rims["leading"] - rims["trailing"]) <= _RIM_SYMMETRY_TOLERANCE_PX, (
        f"switcher pill edge rims are asymmetric: {rims}"
    )
