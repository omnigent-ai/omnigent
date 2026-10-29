"use strict";

// Native menu commands the SPA performs (see web/src/lib/desktopCommands.ts).
// The menu owns their accelerators so they fire even while an embedded browser
// view has focus, where the SPA's own key listeners never see the keystroke.
// The SPA reports which commands it can act on right now; until it does (an
// older web bundle, the setup page, a sign-in page) the items stay disabled and
// Close falls back to closing the window.

/** Commands the SPA can report and receive. */
const DESKTOP_COMMANDS = Object.freeze([
  "close",
  "new-terminal",
  "new-browser-tab",
  "archive-session",
  "toggle-pin-session",
  "go-back",
  "go-forward",
  "reload",
  "focus-address-bar",
  "reopen-closed-tab",
  "previous-tab",
  "next-tab",
]);

/**
 * @typedef {object} DesktopCommandState
 * @property {Set<string>} commands Commands the SPA can act on right now.
 * @property {boolean} sessionPinned Whether the open session is pinned.
 * @property {boolean} sessionArchived Whether the open session is archived.
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
  };
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
  registerDesktopCommandStateReset,
  sanitizeDesktopCommandState,
};
