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
/** Sent to the panel's page each time the panel takes the keyboard, so its prompt does too. */
const FLOATING_COMPOSER_SHOWN_CHANNEL = "omnigent:floating-composer:shown";
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

/** The most a launch's attachments may total; the panel checks the same cap before reading them. */
const FLOATING_LAUNCH_MAX_BYTES = 25 * 1024 * 1024;

/**
 * The first message a panel launch hands to a main window, rebuilt from its checked fields, or
 * null. A skill launch carries no attachments, as the panel's own delivery sends none.
 *
 * @param {unknown} message
 * @returns {{ text: string, skill: { name: string, args: string } | null, files: { name: string, type: string, bytes: Uint8Array }[] } | null}
 */
function floatingComposerLaunchMessage(message) {
  if (!message || typeof message !== "object") return null;
  const { text } = message;
  const skill = message.skill ?? null;
  if (typeof text !== "string") return null;
  if (skill !== null) {
    if (
      typeof skill !== "object" ||
      typeof skill.name !== "string" ||
      typeof skill.args !== "string"
    ) {
      return null;
    }
    // The page's store drops a blank prompt, so a blank skill launch can't be sent.
    if (text.trim() === "") return null;
    return { text, skill: { name: skill.name, args: skill.args }, files: [] };
  }
  if (!Array.isArray(message.files)) return null;
  const files = [];
  let totalBytes = 0;
  for (const file of message.files) {
    if (
      !file ||
      typeof file.name !== "string" ||
      typeof file.type !== "string" ||
      !(file.bytes instanceof Uint8Array)
    ) {
      return null;
    }
    totalBytes += file.bytes.byteLength;
    if (totalBytes > FLOATING_LAUNCH_MAX_BYTES) return null;
    files.push({ name: file.name, type: file.type, bytes: file.bytes });
  }
  if (text.trim() === "" && files.length === 0) return null;
  return { text, skill: null, files };
}

/** How long the panel takes to fade in when it opens; closing is instant. */
const FLOATING_COMPOSER_FADE_MS = 120;
/** How long an open waits for a page still loading to say it's ready before showing anyway. */
const FLOATING_COMPOSER_READY_FALLBACK_MS = 1000;
/** How long the app stays quiet after a page can render the panel before it's built hidden. */
const FLOATING_COMPOSER_PRELOAD_DELAY_MS = 3000;
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
 *   readyFallbackMs?: number,
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
  /** Panels whose current document said its composer rendered; this ends with the document. */
  const ready = new Set();
  /** Panels built hidden ahead of an open, and never shown since. */
  const unshown = new Set();
  /** Opens waiting for a loading page: the panel → its fallback timer. */
  const pendingReveals = new Map();
  /**
   * Panels the user has open, on screen or waiting for their page, in the order opened. An open
   * ends three ways: putting the panel away, launching from it, or clicking an Omnigent window.
   */
  const open = new Set();

  function cancelReveal(panel) {
    const timer = pendingReveals.get(panel);
    if (timer !== undefined) clearTimeout(timer);
    pendingReveals.delete(panel);
  }

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
    cancelReveal(panel);
    pickerOpen.delete(panel);
    ready.delete(panel);
    unshown.delete(panel);
    open.delete(panel);
    if (panels.get(serverUrl) === panel) panels.delete(serverUrl);
    if (!panel.isDestroyed()) panel.destroy();
  }

  function hide(panel, { resetFrame = false } = {}) {
    for (const [serverUrl, candidate] of panels) {
      if (candidate !== panel || panel.isDestroyed()) continue;
      cancelReveal(panel);
      open.delete(panel);
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

  /**
   * Give the panel the keyboard. For a panel-type window this makes it key without activating
   * the app, so no other Omnigent window comes forward; putting it away hands the keyboard back
   * to whatever app had it.
   */
  function takeKeyboard(panel) {
    if (panel.isDestroyed()) return;
    panel.webContents.focus();
    // To its current document only; a page still loading focuses its prompt once it renders.
    if (!panel.webContents.isDestroyed()) panel.webContents.send(FLOATING_COMPOSER_SHOWN_CHANNEL);
  }

  function serverUrlFor(win) {
    for (const [serverUrl, panel] of panels) if (panel === win) return serverUrl;
    return null;
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
    // A new document, or a renderer that went away, can't still have the picker open or be ready.
    const endDocument = () => {
      pickerOpen.delete(panel);
      ready.delete(panel);
    };
    panel.webContents.on("did-navigate", endDocument);
    panel.webContents.on("render-process-gone", endDocument);
    // ⌘W puts the panel away like Escape, keeping its draft; only quitting closes it.
    panel.on("close", (event) => {
      if (deps.quitting()) return;
      event.preventDefault();
      hide(panel);
    });
    panel.on("closed", () => {
      cancelSave(panel);
      cancelReveal(panel);
      if (panels.get(serverUrl) === panel) panels.delete(serverUrl);
      pickerOpen.delete(panel);
      ready.delete(panel);
      unshown.delete(panel);
      open.delete(panel);
    });
    panels.set(serverUrl, panel);
    return panel;
  }

  function reveal(serverUrl, panel) {
    cancelReveal(panel);
    if (panel.isDestroyed()) return;
    // A document that has been on screen reopens at once, even one that never says it's ready.
    ready.add(panel);
    panel.setBounds(targetFrame(serverUrl));
    if (deps.present) deps.present(panel, () => panel.showInactive());
    else panel.showInactive();
    takeKeyboard(panel);
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
     * Open the panel for `serverUrl` as the key window without activating the app. A panel
     * already open (on screen or waiting for its page) takes the keyboard again.
     *
     * @param {string} serverUrl
     * @returns {"shown" | "focused" | "unavailable"}
     */
    show(serverUrl) {
      if (typeof serverUrl !== "string" || serverUrl.length === 0) return "unavailable";
      const panel = panelFor(serverUrl);
      // The user asked for this panel, so it's kept; other servers' builds ahead and waiting
      // opens are dropped.
      unshown.delete(panel);
      for (const [otherUrl, other] of [...panels]) {
        if (other === panel) continue;
        if (pendingReveals.has(other)) {
          cancelReveal(other);
          open.delete(other);
        }
        if (unshown.has(other)) discard(otherUrl, other);
      }
      // The latest open goes last, so a press while it's open comes back to it.
      open.delete(panel);
      open.add(panel);
      if (panel.isVisible()) {
        takeKeyboard(panel);
        return "focused";
      }
      // A page still loading shows once it's ready, or after a moment if it never says so.
      if (ready.has(panel)) reveal(serverUrl, panel);
      else if (!pendingReveals.has(panel)) {
        const timer = setTimeout(
          () => reveal(serverUrl, panel),
          deps.readyFallbackMs ?? FLOATING_COMPOSER_READY_FALLBACK_MS,
        );
        timer.unref?.();
        pendingReveals.set(panel, timer);
      }
      return "shown";
    },

    /**
     * Build the panel for `serverUrl` hidden, so the next open shows a page that's already
     * loaded. At most one panel waits unshown: another server's is let go first.
     *
     * @param {string} serverUrl
     */
    preload(serverUrl) {
      const existing = panels.get(serverUrl);
      if (existing && !existing.isDestroyed() && deps.isCurrent(existing, serverUrl)) return;
      for (const [otherUrl, panel] of [...panels]) {
        if (otherUrl !== serverUrl && unshown.has(panel)) discard(otherUrl, panel);
      }
      unshown.add(panelFor(serverUrl));
    },

    /** Let go of `serverUrl`'s panel if it was built ahead and never shown. */
    releaseUnshown(serverUrl) {
      const panel = panels.get(serverUrl);
      if (panel && unshown.has(panel)) discard(serverUrl, panel);
    },

    /** The panel's current document rendered its composer: an open waiting on it shows now. */
    markReady(panel) {
      if (!isPanel(panel) || panel.isDestroyed()) return;
      ready.add(panel);
      if (pendingReveals.has(panel)) reveal(serverUrlFor(panel), panel);
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

    /** The server of the panel the user has open now, or null; ⇧⌘Space then goes back to it. */
    openServerUrl() {
      const latest = [...open].at(-1);
      return latest === undefined ? null : serverUrlFor(latest);
    },

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
      // An open still waiting on its page is dropped too: the user moved on.
      for (const panel of [...pendingReveals.keys()]) {
        cancelReveal(panel);
        open.delete(panel);
      }
      for (const [serverUrl, panel] of panels) {
        if (panel.isDestroyed() || !panel.isVisible() || pickerOpen.has(panel)) continue;
        // The user chose that window; it keeps the focus.
        open.delete(panel);
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
    serverUrlFor,
  };
}

/**
 * Builds a panel hidden once the app has been quiet for a while, so ⇧⌘Space shows a page that
 * has already loaded. Each `schedule` restarts the wait; an open before it ends cancels it. A
 * panel lost later is built by the next open, or ahead after the next announcement.
 *
 * @param {{
 *   target: () => string | null,
 *   preload: (serverUrl: string) => void,
 *   delayMs?: number,
 * }} deps `target` is the server to build for when the wait ends, or null for none.
 */
function createFloatingComposerPreloader(deps) {
  let timer = null;
  const cancel = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  return {
    schedule() {
      cancel();
      timer = setTimeout(() => {
        timer = null;
        const serverUrl = deps.target();
        if (serverUrl !== null) deps.preload(serverUrl);
      }, deps.delayMs ?? FLOATING_COMPOSER_PRELOAD_DELAY_MS);
      timer.unref?.();
    },
    cancel,
  };
}

module.exports = {
  FLOATING_COMPOSER_FADE_MS,
  FLOATING_COMPOSER_PRELOAD_DELAY_MS,
  FLOATING_COMPOSER_READY_FALLBACK_MS,
  FLOATING_COMPOSER_PATH,
  FLOATING_COMPOSER_SHOWN_CHANNEL,
  FRAME_SETTINGS_KEY,
  createFloatingComposerController,
  createFloatingComposerPreloader,
  fadeInFloatingComposer,
  floatingComposerAppPagePath,
  floatingComposerLaunchMessage,
  floatingComposerSessionPath,
  floatingComposerWindowOptions,
};
