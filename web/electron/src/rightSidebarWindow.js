"use strict";

// Opt-in setting: opening the right sidebar widens the window by the sidebar's width so the
// chat keeps its width; closing it shrinks back only a window that grew.

/** settings.json key; only `true` turns the behavior on. */
const WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING = "widen_window_for_right_sidebar";

/** Sidebar widths outside this range are not a real sidebar report and are ignored. */
const MAX_SIDEBAR_WIDTH = 10000;

/** @typedef {{ x: number, y: number, width: number, height: number }} Rect */
/** @typedef {{ added: number, shiftX: number }} Growth Width added, and the x shift to stay on screen. */

/** @param {Record<string, unknown>} settings */
function widenWindowForRightSidebarEnabled(settings) {
  return settings?.[WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING] === true;
}

/** The window `extra` px wider inside `workArea` (moves left, then caps); null if it can't grow. */
function widenedBounds(bounds, workArea, extra) {
  const width = Math.min(bounds.width + extra, Math.max(bounds.width, workArea.width));
  const added = width - bounds.width;
  if (added <= 0) return null;
  const right = workArea.x + workArea.width;
  const x = bounds.x + width > right ? Math.max(workArea.x, right - width) : bounds.x;
  return { bounds: { ...bounds, x, width }, growth: { added, shiftX: x - bounds.x } };
}

/** The window minus its growth (keeping resizes made since), moved back if it moved left. */
function narrowedBounds(bounds, workArea, growth, minWidth) {
  const width = Math.max(minWidth, bounds.width - growth.added);
  if (growth.shiftX === 0) return { ...bounds, width };
  const right = workArea.x + workArea.width;
  const x = Math.max(workArea.x, Math.min(bounds.x - growth.shiftX, right - width));
  return { ...bounds, x, width };
}

/** Maximized and full-screen windows keep their size. */
function canResize(win) {
  return (
    !win.isDestroyed() &&
    win.isResizable() &&
    !win.isMaximized() &&
    !win.isFullScreen() &&
    win.isSimpleFullScreen?.() !== true
  );
}

/** `workAreaFor(bounds)` gives the work area of the window's display; `animate` is macOS-only. */
function createRightSidebarWindow({ loadSettings, saveSettings, workAreaFor, animate = false }) {
  /** What each window grew by for its sidebar, until the sidebar closes. */
  const growths = new WeakMap();
  /** Bounds to restore a window to whose sidebar closed while it was maximized or full screen. */
  const pendingCloses = new WeakMap();

  function enabled() {
    return widenWindowForRightSidebarEnabled(loadSettings());
  }

  /** @returns {boolean} false when the setting could not be saved */
  function setEnabled(next) {
    try {
      const others = Object.entries(loadSettings()).filter(
        ([key]) => key !== WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING,
      );
      if (next === true) others.push([WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING, true]);
      saveSettings(Object.fromEntries(others));
      return true;
    } catch (error) {
      console.warn("[omnigent] couldn't save the right sidebar window setting:", error);
      return false;
    }
  }

  /** The right sidebar opened, `sidebarWidth` CSS px wide. Returns whether the window grew. */
  function opened(win, sidebarWidth) {
    if (
      typeof sidebarWidth !== "number" ||
      !Number.isFinite(sidebarWidth) ||
      sidebarWidth <= 0 ||
      sidebarWidth > MAX_SIDEBAR_WIDTH
    ) {
      return false;
    }
    pendingCloses.delete(win);
    if (growths.has(win) || !canResize(win) || !enabled()) return false;
    const extra = Math.round(sidebarWidth * win.webContents.getZoomFactor());
    const bounds = win.getBounds();
    const plan = widenedBounds(bounds, workAreaFor(bounds), extra);
    if (!plan) return false;
    growths.set(win, plan.growth);
    win.setBounds(plan.bounds, animate);
    return true;
  }

  /** The right sidebar closed. A maximized or full-screen window shrinks once it's restored. */
  function closed(win) {
    const growth = growths.get(win);
    if (!growth) return false;
    const [minWidth = 0] = win.getMinimumSize();
    if (!canResize(win)) {
      // The normal bounds are reliable while the window is still maximized or full screen.
      const normal = win.getNormalBounds();
      pendingCloses.set(win, narrowedBounds(normal, workAreaFor(normal), growth, minWidth));
      return false;
    }
    forget(win);
    const bounds = win.getBounds();
    win.setBounds(narrowedBounds(bounds, workAreaFor(bounds), growth, minWidth), animate);
    return true;
  }

  /** The window may have left full screen or maximize; applies a close made meanwhile. */
  function restored(win) {
    const target = pendingCloses.get(win);
    if (!target || !canResize(win)) return false;
    forget(win);
    win.setBounds(target, animate);
    return true;
  }

  /** Drops a window's growth, e.g. when a new page that doesn't know about it loads. */
  function forget(win) {
    growths.delete(win);
    pendingCloses.delete(win);
  }

  return { enabled, setEnabled, opened, closed, restored, forget };
}

module.exports = {
  WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING,
  createRightSidebarWindow,
  narrowedBounds,
  widenWindowForRightSidebarEnabled,
  widenedBounds,
};
