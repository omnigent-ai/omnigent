"use strict";

// The Quit menu item (⌘Q / Ctrl+Q) asks before quitting, because quitting also
// disconnects the hosts this app runs and stops a local server it started.
// The window's page asks, in an in-app dialog. Only the menu item asks: Dock
// Quit, OS logout, and Restart to Update call app.quit() directly.

/** settings.json key; anything other than `false` keeps the prompt on. */
const CONFIRM_BEFORE_QUIT_SETTING = "confirm_before_quit";

/**
 * @param {Record<string, unknown>} settings
 * @returns {boolean}
 */
function confirmBeforeQuitEnabled(settings) {
  return settings?.[CONFIRM_BEFORE_QUIT_SETTING] !== false;
}

/**
 * The prompt's detail line naming what quitting stops, or null when this app
 * runs nothing that would stop.
 *
 * @param {{ hosts: number, localServer: boolean }} running
 * @returns {string | null}
 */
function quitPromptDetail({ hosts, localServer }) {
  if (hosts > 0 && localServer) {
    return "Sessions running on this computer will go offline, and the local Omnigent server will stop.";
  }
  if (hosts > 0) return "Sessions running on this computer will go offline.";
  if (localServer) return "The local Omnigent server will stop.";
  return null;
}

/**
 * @param {{
 *   isMac: boolean,
 *   appName: () => string,
 *   loadSettings: () => Record<string, unknown>,
 *   saveSettings: (settings: Record<string, unknown>) => void,
 *   runningWork: () => { hosts: number, localServer: boolean },
 *   ask: (detail: string | null) => boolean,
 *   quit: () => void,
 * }} deps `ask` shows the prompt in a window's page; false when no page can.
 */
function createQuitConfirmation({
  isMac,
  appName,
  loadSettings,
  saveSettings,
  runningWork,
  ask,
  quit,
}) {
  function setConfirmBeforeQuit(enabled) {
    const settings = loadSettings();
    settings[CONFIRM_BEFORE_QUIT_SETTING] = enabled;
    saveSettings(settings);
  }

  /** Quit, asking first unless the user turned the prompt off or no page can ask. */
  function requestQuit() {
    if (!confirmBeforeQuitEnabled(loadSettings()) || !ask(quitPromptDetail(runningWork()))) {
      quit();
    }
  }

  /** The page's prompt was confirmed, optionally with "Don't ask again" checked. */
  function confirmQuit({ dontAskAgain }) {
    if (dontAskAgain) setConfirmBeforeQuit(false);
    quit();
  }

  /**
   * The warn-before-quitting toggle and Quit itself, for the macOS app menu or
   * the File menu elsewhere.
   *
   * @returns {Electron.MenuItemConstructorOptions[]}
   */
  function menuItems() {
    return [
      {
        id: "confirm_before_quit",
        label: `Warn Before Quitting (${isMac ? "⌘Q" : "Ctrl+Q"})`,
        type: "checkbox",
        checked: confirmBeforeQuitEnabled(loadSettings()),
        // Electron flips `checked` before calling click.
        click: (item) => setConfirmBeforeQuit(item.checked),
      },
      {
        id: "quit",
        label: `Quit ${appName()}`,
        accelerator: "CmdOrCtrl+Q",
        click: () => requestQuit(),
      },
    ];
  }

  return { requestQuit, confirmQuit, menuItems };
}

module.exports = {
  CONFIRM_BEFORE_QUIT_SETTING,
  confirmBeforeQuitEnabled,
  createQuitConfirmation,
  quitPromptDetail,
};
