"use strict";

// Native menu commands the SPA performs (see web/src/lib/desktopCommands.ts).
// The menu owns their accelerators so they fire even while an embedded browser
// view has focus, where the SPA's own key listeners never see the keystroke.
// The SPA reports which commands it can act on right now; until it does (an
// older web bundle, the setup page, a sign-in page) the items stay disabled,
// Close falls back to closing the window, and Quit quits without asking.

/** Commands the SPA can report and receive. */
const DESKTOP_COMMANDS = Object.freeze([
  "close",
  "new-terminal",
  "new-browser-tab",
  "archive-session",
  "toggle-pin-session",
  "fork-session",
  "go-back",
  "go-forward",
  "reload",
  "focus-address-bar",
  "reopen-closed-tab",
  "previous-tab",
  "next-tab",
  "quit",
]);

/**
 * @typedef {object} DesktopCommandState
 * @property {Set<string>} commands Commands the SPA can act on right now.
 * @property {boolean} sessionPinned Whether the open session is pinned.
 * @property {boolean} sessionArchived Whether the open session is archived.
 * @property {boolean} pickListFocused Whether an open pick list has the keyboard, so it steps
 *   on Ctrl-N / Ctrl-P.
 */

/**
 * Validate a renderer-reported command state. Unknown commands are dropped;
 * anything that isn't an object reports nothing.
 *
 * @param {unknown} raw
 * @returns {DesktopCommandState | null}
 */
function sanitizeDesktopCommandState(raw) {
  if (!raw || typeof raw !== "object") return null;
  const reported = Array.isArray(raw.commands) ? raw.commands : [];
  return {
    commands: new Set(reported.filter((command) => DESKTOP_COMMANDS.includes(command))),
    sessionPinned: raw.sessionPinned === true,
    sessionArchived: raw.sessionArchived === true,
    pickListFocused: raw.pickListFocused === true,
  };
}

/**
 * Whether two reported states differ in what the menu shows: which items are enabled and the
 * Session menu's Pin and Archive labels. Pick-list focus alone doesn't change the menu.
 *
 * @param {DesktopCommandState | null | undefined} previous
 * @param {DesktopCommandState | null} next
 */
function menuStateChanged(previous, next) {
  if (!previous || !next) return previous !== next;
  return (
    previous.sessionPinned !== next.sessionPinned ||
    previous.sessionArchived !== next.sessionArchived ||
    previous.commands.size !== next.commands.size ||
    [...next.commands].some((command) => !previous.commands.has(command))
  );
}

/**
 * Whether a key press is Ctrl-N or Ctrl-P alone, the keys an open pick list steps on. A
 * non-Latin layout falls back to the physical key, as the page's own check does.
 *
 * @param {Pick<Electron.Input, "key" | "code" | "control" | "meta" | "alt" | "shift">} input
 */
function isPickListStep(input) {
  if (!input.control || input.meta || input.alt || input.shift) return false;
  const key = String(input.key).toLowerCase();
  if (/^[a-z]$/.test(key)) return key === "n" || key === "p";
  return input.code === "KeyN" || input.code === "KeyP";
}

/**
 * Leave Ctrl-N / Ctrl-P to the page while one of its pick lists has the keyboard, so the
 * menu's New Session (Ctrl+N off macOS) doesn't take the key the list steps on.
 *
 * @param {Pick<Electron.WebContents, "on" | "setIgnoreMenuShortcuts">} webContents
 * @param {() => boolean} isPickListFocused The page's latest report.
 */
function registerPickListMenuYield(webContents, isPickListFocused) {
  webContents.on("before-input-event", (_event, input) => {
    if (input.type !== "keyDown") return;
    webContents.setIgnoreMenuShortcuts(isPickListStep(input) && isPickListFocused());
  });
}

/**
 * Forget a window's reported command state whenever its page is replaced (a
 * reload, a sign-in redirect, another server) or its renderer dies, so the menu
 * never routes a command to a page that isn't listening. In-app routing stays
 * on the same document and keeps the state.
 *
 * @param {Pick<Electron.WebContents, "on">} webContents
 * @param {() => void} onReset
 */
function registerDesktopCommandStateReset(webContents, onReset) {
  webContents.on("did-navigate", onReset);
  webContents.on("render-process-gone", onReset);
}

module.exports = {
  DESKTOP_COMMANDS,
  menuStateChanged,
  registerDesktopCommandStateReset,
  registerPickListMenuYield,
  sanitizeDesktopCommandState,
};
