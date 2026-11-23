"use strict";

// Settings → "Widen window when opening the right sidebar" (off by default): opening the right
// sidebar makes the window wider by the sidebar's width so the chat keeps its width, and closing
// it takes that width back off. Only a window that grew for the sidebar shrinks.

/** settings.json key; only `true` turns the behavior on. */
const WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING = "widen_window_for_right_sidebar";

/** Sidebar widths outside this range are not a real sidebar report and are ignored. */
const MAX_SIDEBAR_WIDTH = 10000;

/**
 * @typedef {{ x: number, y: number, width: number, height: number }} Rect
 * @typedef {{ added: number, shiftX: number }} Growth How much the window grew, and how far
 *   it moved right (negative: left) to stay on screen.
 */

/**
 * @param {Record<string, unknown>} settings
 * @returns {boolean}
 */
function widenWindowForRightSidebarEnabled(settings) {
  return settings?.[WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING] === true;
}

/**
 * The window `extra` px wider, kept inside `workArea`: it moves left when its right edge would
 * pass the screen's, and is capped at the work area's width. Null when it can't grow.
 *
 * @param {Rect} bounds
 * @param {Rect} workArea
 * @param {number} extra
 * @returns {{ bounds: Rect, growth: Growth } | null}
 */
function widenedBounds(bounds, workArea, extra) {
  const width = Math.min(bounds.width + extra, Math.max(bounds.width, workArea.width));
  const added = width - bounds.width;
  if (added <= 0) return null;
  const right = workArea.x + workArea.width;
  const x = bounds.x + width > right ? Math.max(workArea.x, right - width) : bounds.x;
  return { bounds: { ...bounds, x, width }, growth: { added, shiftX: x - bounds.x } };
}

/**
 * The window with the sidebar's width taken back off, kept above `minWidth`. Its current width
 * counts, so a resize while the sidebar was open is kept. A window moved left to make room
 * moves back, staying inside `workArea`.
 *
 * @param {Rect} bounds
 * @param {Rect} workArea
 * @param {Growth} growth
 * @param {number} minWidth
 * @returns {Rect}
 */
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

/**
 * @param {{
 *   loadSettings: () => Record<string, unknown>,
 *   saveSettings: (settings: Record<string, unknown>) => void,
 *   workAreaFor: (bounds: Rect) => Rect,
 *   animate?: boolean,
 * }} deps `workAreaFor` is the work area of the display a window is on; `animate` animates
 *   the resize where the platform can (macOS).
 */
function createRightSidebarWindow({ loadSettings, saveSettings, workAreaFor, animate = false }) {
  /** What each window grew by for its sidebar, until the sidebar closes. */
  const growths = new WeakMap();

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

  /**
   * The page opened its right sidebar, `sidebarWidth` CSS px wide. A window that already grew
   * for it (and hasn't shrunk back) doesn't grow again.
   *
   * @param {Electron.BrowserWindow} win
   * @param {number} sidebarWidth
   * @returns {boolean} whether the window grew
   */
  function opened(win, sidebarWidth) {
    if (
      typeof sidebarWidth !== "number" ||
      !Number.isFinite(sidebarWidth) ||
      sidebarWidth <= 0 ||
      sidebarWidth > MAX_SIDEBAR_WIDTH
    ) {
      return false;
    }
    if (growths.has(win) || !canResize(win) || !enabled()) return false;
    const extra = Math.round(sidebarWidth * win.webContents.getZoomFactor());
    const bounds = win.getBounds();
    const plan = widenedBounds(bounds, workAreaFor(bounds), extra);
    if (!plan) return false;
    growths.set(win, plan.growth);
    win.setBounds(plan.bounds, animate);
    return true;
  }

  /**
   * The page closed its right sidebar. A maximized or full-screen window keeps its size and
   * shrinks the next time the sidebar closes while it's a normal window.
   *
   * @param {Electron.BrowserWindow} win
   * @returns {boolean} whether the window shrank
   */
  function closed(win) {
    const growth = growths.get(win);
    if (!growth || !canResize(win)) return false;
    growths.delete(win);
    const bounds = win.getBounds();
    const [minWidth = 0] = win.getMinimumSize();
    win.setBounds(narrowedBounds(bounds, workAreaFor(bounds), growth, minWidth), animate);
    return true;
  }

  return { enabled, setEnabled, opened, closed };
}

module.exports = {
  WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING,
  createRightSidebarWindow,
  narrowedBounds,
  widenWindowForRightSidebarEnabled,
  widenedBounds,
};
