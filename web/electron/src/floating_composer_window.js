"use strict";

/**
 * The floating New Agent panel: one per server, floating over every app and Space without
 * making Omnigent the active app (macOS).
 *
 * - It shows as the key window while another app stays frontmost.
 * - Switching to another app never hides it; focusing one of Omnigent's own windows does.
 *   Escape, its X and ⌘W hide it too.
 * - Hiding keeps the renderer, so the draft, caret and undo survive.
 *
 * Main owns the window lifecycle; everything with an Electron dependency is injected.
 */

const { FLOATING_COMPOSER_SIZE, placeFloatingComposer } = require("./floating_composer_frame");

const FLOATING_COMPOSER_PATH = "/compose/floating";
const FRAME_SETTINGS_KEY = "floating_composer_frames";
const SESSION_PATH = /^\/c\/[A-Za-z0-9_-]{1,128}$/;
/** App pages the panel's own links lead to; they open in a main window. */
const APP_PAGE_PATHS = new Set(["/settings/general", "/settings/import", "/settings/integrations"]);

/**
 * The route a launched session opens at, or null for anything else: the panel may only
 * point a main window at a session it created.
 *
 * @param {unknown} sessionPath
 * @returns {string | null}
 */
function floatingComposerSessionPath(sessionPath) {
  return typeof sessionPath === "string" && SESSION_PATH.test(sessionPath) ? sessionPath : null;
}

/**
 * An app page the panel may show in a main window, or null for anything else.
 *
 * @param {unknown} pagePath
 * @returns {string | null}
 */
function floatingComposerAppPagePath(pagePath) {
  return typeof pagePath === "string" && APP_PAGE_PATHS.has(pagePath) ? pagePath : null;
}

/** How long the panel takes to fade in when it opens; closing is instant. */
const FLOATING_COMPOSER_FADE_MS = 120;
/** Each panel's current fade, so reopening mid-fade stops the one before. */
const fades = new WeakMap();

/**
 * Show `panel` by calling `show`, fading it in over `durationMs` unless the user reduces
 * motion. The fade eases out, so the panel is nearly there at once.
 *
 * @param {Pick<import("electron").BrowserWindow, "setOpacity" | "isDestroyed">} panel
 * @param {() => void} show
 * @param {{
 *   reduceMotion: boolean,
 *   durationMs?: number,
 *   now?: () => number,
 *   setTimer?: (callback: () => void, ms: number) => unknown,
 * }} options
 */
function fadeInFloatingComposer(panel, show, options) {
  if (options.reduceMotion) {
    // A fade still running from an earlier show would undo this.
    fades.delete(panel);
    panel.setOpacity(1);
    show();
    return;
  }
  const durationMs = options.durationMs ?? FLOATING_COMPOSER_FADE_MS;
  const now = options.now ?? Date.now;
  const setTimer = options.setTimer ?? ((callback, ms) => setTimeout(callback, ms));
  const started = now();
  const fade = {};
  fades.set(panel, fade);
  panel.setOpacity(0);
  show();
  const step = () => {
    if (panel.isDestroyed() || fades.get(panel) !== fade) return;
    const progress = Math.min(1, (now() - started) / durationMs);
    panel.setOpacity(progress >= 1 ? 1 : 1 - (1 - progress) ** 3);
    if (progress < 1) setTimer(step, 8);
  };
  setTimer(step, 8);
}

/** BrowserWindow options for the panel; the shell's webPreferences always apply. */
function floatingComposerWindowOptions() {
  return {
    type: "panel",
    width: FLOATING_COMPOSER_SIZE.default.width,
    height: FLOATING_COMPOSER_SIZE.default.height,
    minWidth: FLOATING_COMPOSER_SIZE.min.width,
    minHeight: FLOATING_COMPOSER_SIZE.min.height,
    maxWidth: FLOATING_COMPOSER_SIZE.max.width,
    maxHeight: FLOATING_COMPOSER_SIZE.max.height,
    frame: false,
    resizable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    excludedFromShownWindowsMenu: true,
    show: false,
    title: "New Agent",
    // See-through, so the window behind stays readable; the page paints its own tint.
    transparent: true,
    backgroundColor: "#00000000",
  };
}

/**
 * @param {{
 *   createPanel: (serverUrl: string) => import("electron").BrowserWindow,
 *   screen: Pick<import("electron").Screen, "getAllDisplays" | "getCursorScreenPoint" | "getDisplayNearestPoint">,
 *   loadFrames: () => Record<string, unknown>,
 *   saveFrames: (frames: Record<string, unknown>) => void,
 *   isCurrent: (panel: import("electron").BrowserWindow, serverUrl: string) => boolean,
 *   quitting: () => boolean,
 *   present?: (panel: import("electron").BrowserWindow, show: () => void) => void,
 * }} deps `isCurrent` says whether a panel still shows its server's panel page; `present`
 *   puts a hidden panel on screen, such as with a fade, by calling `show`.
 */
function createFloatingComposerController(deps) {
  /** @type {Map<string, import("electron").BrowserWindow>} */
  const panels = new Map();
  /**
   * Panels whose document has its native file picker open; a main-window focus then must not
   * hide them. The state belongs to that document and ends with it.
   */
  const pickerOpen = new Set();
  /** Each panel's pending debounced frame save. */
  const saveTimers = new Map();

  function cancelSave(panel) {
    const timer = saveTimers.get(panel);
    if (timer !== undefined) clearTimeout(timer);
    saveTimers.delete(panel);
  }

  /** The remembered frames, or none when they can't be read. */
  function readFrames() {
    try {
      const frames = deps.loadFrames();
      return frames !== null && typeof frames === "object" ? frames : {};
    } catch {
      return {};
    }
  }

  /** A remembered frame is a convenience: failing to read or save one never keeps a panel up. */
  function persistFrames(nextFrames) {
    try {
      deps.saveFrames(nextFrames(readFrames()));
    } catch (err) {
      console.warn("[omnigent] could not save the floating panel's frame:", err);
    }
  }

  function rememberFrame(serverUrl, panel) {
    cancelSave(panel);
    if (panel.isDestroyed()) return;
    persistFrames((frames) => ({ ...frames, [serverUrl]: panel.getBounds() }));
  }

  function forgetFrame(serverUrl, panel) {
    cancelSave(panel);
    persistFrames((frames) =>
      Object.fromEntries(Object.entries(frames).filter(([key]) => key !== serverUrl)),
    );
  }

  function discard(serverUrl, panel) {
    cancelSave(panel);
    pickerOpen.delete(panel);
    if (panels.get(serverUrl) === panel) panels.delete(serverUrl);
    if (!panel.isDestroyed()) panel.destroy();
  }

  function hide(panel, { resetFrame = false } = {}) {
    for (const [serverUrl, candidate] of panels) {
      if (candidate !== panel || panel.isDestroyed()) continue;
      // A picker its page never reported closed must not keep it up on a later open.
      pickerOpen.delete(panel);
      panel.hide();
      if (resetFrame) forgetFrame(serverUrl, panel);
      else rememberFrame(serverUrl, panel);
      return true;
    }
    return false;
  }

  function isPanel(win) {
    for (const panel of panels.values()) if (panel === win) return true;
    return false;
  }

  function panelFor(serverUrl) {
    const existing = panels.get(serverUrl);
    if (existing && !existing.isDestroyed()) {
      // A panel that left its page (signed out, reconnected elsewhere) is rebuilt, not reused.
      if (deps.isCurrent(existing, serverUrl)) return existing;
      discard(serverUrl, existing);
    }
    const panel = deps.createPanel(serverUrl);
    panel.setAlwaysOnTop(true, "floating");
    panel.setVisibleOnAllWorkspaces(true, {
      visibleOnFullScreen: true,
      skipTransformProcessType: true,
    });
    const scheduleSave = () => {
      cancelSave(panel);
      const timer = setTimeout(() => {
        saveTimers.delete(panel);
        rememberFrame(serverUrl, panel);
      }, 250);
      timer.unref?.();
      saveTimers.set(panel, timer);
    };
    panel.on("moved", scheduleSave);
    panel.on("resized", scheduleSave);
    // A new document, or a renderer that went away, can't still have the picker open.
    panel.webContents.on("did-navigate", () => pickerOpen.delete(panel));
    panel.webContents.on("render-process-gone", () => pickerOpen.delete(panel));
    // ⌘W hides the panel like Escape, keeping its draft; only quitting closes it.
    panel.on("close", (event) => {
      if (deps.quitting()) return;
      event.preventDefault();
      hide(panel);
    });
    panel.on("closed", () => {
      cancelSave(panel);
      if (panels.get(serverUrl) === panel) panels.delete(serverUrl);
      pickerOpen.delete(panel);
    });
    panels.set(serverUrl, panel);
    return panel;
  }

  function targetFrame(serverUrl) {
    // Opened by the global shortcut, so it follows the pointer to the display the user is on.
    const pointerDisplay = deps.screen.getDisplayNearestPoint(deps.screen.getCursorScreenPoint());
    return placeFloatingComposer({
      savedFrame: readFrames()[serverUrl],
      displays: deps.screen.getAllDisplays(),
      pointerDisplay,
    });
  }

  return {
    /**
     * Show the panel for `serverUrl` as the key window without activating the app. If it is
     * already visible but not focused, focus it.
     *
     * @param {string} serverUrl
     * @returns {"shown" | "focused" | "unavailable"}
     */
    show(serverUrl) {
      if (typeof serverUrl !== "string" || serverUrl.length === 0) return "unavailable";
      const panel = panelFor(serverUrl);
      if (panel.isVisible()) {
        // A visible panel that lost focus needs an explicit focus; showInactive won't key it.
        panel.moveTop();
        panel.focus();
        panel.webContents.focus();
        return "focused";
      }
      panel.setBounds(targetFrame(serverUrl));
      if (deps.present) deps.present(panel, () => panel.showInactive());
      else panel.showInactive();
      panel.webContents.focus();
      return "shown";
    },

    /**
     * Hide `panel`, keeping its renderer. It remembers where it was, unless `resetFrame` asks
     * the next open to start at the default placement (after a launch).
     *
     * @param {import("electron").BrowserWindow} panel
     * @param {{ resetFrame?: boolean }} [options]
     * @returns {boolean} whether `panel` is one of this controller's
     */
    hide,

    /**
     * Close every panel whose server matches, such as on sign-out; the next open builds a
     * fresh one.
     *
     * @param {(serverUrl: string) => boolean} matches
     */
    closePanels(matches) {
      for (const [serverUrl, panel] of [...panels]) {
        if (matches(serverUrl)) discard(serverUrl, panel);
      }
    },

    /** Close `panel` if it is still one of this controller's, never a newer one for its server. */
    close(panel) {
      for (const [serverUrl, candidate] of [...panels]) {
        if (candidate === panel) discard(serverUrl, panel);
      }
    },

    /** One of Omnigent's own windows took focus: visible panels hide, like a click-away. */
    mainWindowFocused() {
      for (const [serverUrl, panel] of panels) {
        if (panel.isDestroyed() || !panel.isVisible() || pickerOpen.has(panel)) continue;
        panel.hide();
        rememberFrame(serverUrl, panel);
      }
    },

    filePickerOpened(panel) {
      if (isPanel(panel)) pickerOpen.add(panel);
    },

    filePickerClosed(panel) {
      pickerOpen.delete(panel);
    },

    isPanel,

    /** The server a panel is bound to, set by main when it built the panel, never by its page. */
    serverUrlFor(win) {
      for (const [serverUrl, panel] of panels) if (panel === win) return serverUrl;
      return null;
    },
  };
}

module.exports = {
  FLOATING_COMPOSER_FADE_MS,
  FLOATING_COMPOSER_PATH,
  FRAME_SETTINGS_KEY,
  createFloatingComposerController,
  fadeInFloatingComposer,
  floatingComposerAppPagePath,
  floatingComposerSessionPath,
  floatingComposerWindowOptions,
};
