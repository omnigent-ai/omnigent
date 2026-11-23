"""Emulation of the macOS desktop shell in Chromium for the desktop UI tests."""

from __future__ import annotations

# Run with ``page.add_init_script`` so the SPA boots as the macOS Electron shell.
MAC_SHELL_INIT_SCRIPT = """
    Object.defineProperty(navigator, "platform", { value: "MacIntel" });
    Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
    Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
    window.omnigentDesktop = {
        kind: "electron",
        setBadgeCount() {},
        notify() { return Promise.resolve(false); },
        onNotificationActivated() { return () => {}; },
        getServerPicker() { return Promise.resolve(null); },
        switchServer() { return Promise.resolve(); },
        openServerSetup() {},
    };
"""

# macOS paints the traffic lights outside the DOM; index.css reserves this strip for them.
WINDOW_CONTROLS_STRIP = {"x": 0, "y": 0, "width": 88, "height": 48}


def intersects_window_controls(box: dict[str, float] | None) -> bool:
    """Whether a rendered bounding box overlaps the traffic lights' strip."""
    assert box is not None and box["width"] > 0 and box["height"] > 0, f"no rendered box: {box}"
    strip = WINDOW_CONTROLS_STRIP
    return (
        box["x"] < strip["x"] + strip["width"]
        and strip["x"] < box["x"] + box["width"]
        and box["y"] < strip["y"] + strip["height"]
        and strip["y"] < box["y"] + box["height"]
    )
