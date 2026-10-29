"use strict";

// The Quit menu item (⌘Q / Ctrl+Q) asks before quitting, because quitting also
// disconnects the hosts this app runs and stops a local server it started.
// Only the menu item asks: Dock Quit, OS logout, and Restart to Update call
// app.quit() directly and never block on a prompt.

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
 * The prompt's detail line naming what quitting stops, or undefined when this
 * app runs nothing that would stop.
 *
 * @param {{ hosts: number, localServer: boolean }} running
 * @returns {string | undefined}
 */
function quitPromptDetail({ hosts, localServer }) {
  if (hosts > 0 && localServer) {
    return "Sessions running on this computer will go offline, and the local Omnigent server will stop.";
  }
  if (hosts > 0) return "Sessions running on this computer will go offline.";
  if (localServer) return "The local Omnigent server will stop.";
  return undefined;
}

/**
 * @param {{
 *   appName: () => string,
 *   isMac: boolean,
 *   loadSettings: () => Record<string, unknown>,
 *   saveSettings: (settings: Record<string, unknown>) => void,
 *   runningWork: () => { hosts: number, localServer: boolean },
 *   parentWindow: () => Electron.BrowserWindow | null,
 *   showMessageBox: (
 *     parent: Electron.BrowserWindow | null,
 *     options: Electron.MessageBoxOptions,
 *   ) => Promise<Electron.MessageBoxReturnValue>,
 *   quit: () => void,
 * }} deps
 */
function createQuitConfirmation({
  appName,
  isMac,
  loadSettings,
  saveSettings,
  runningWork,
  parentWindow,
  showMessageBox,
  quit,
}) {
  let prompting = false;

  function setConfirmBeforeQuit(enabled) {
    const settings = loadSettings();
    settings[CONFIRM_BEFORE_QUIT_SETTING] = enabled;
    saveSettings(settings);
  }

  /** Quit, asking first unless the user turned the prompt off. */
  async function requestQuit() {
    if (!confirmBeforeQuitEnabled(loadSettings())) {
      quit();
      return;
    }
    // A second ⌘Q while the prompt is up leaves that prompt to answer.
    if (prompting) return;
    prompting = true;
    try {
      const detail = quitPromptDetail(runningWork());
      const { response, checkboxChecked } = await showMessageBox(parentWindow(), {
        type: "question",
        message: `Quit ${appName()}?`,
        ...(detail ? { detail } : {}),
        buttons: ["Quit", "Cancel"],
        defaultId: 0,
        cancelId: 1,
        checkboxLabel: "Don't ask again",
        checkboxChecked: false,
      });
      if (response !== 0) return;
      if (checkboxChecked) setConfirmBeforeQuit(false);
      quit();
    } finally {
      prompting = false;
    }
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
        click: () => void requestQuit(),
      },
    ];
  }

  return { requestQuit, menuItems };
}

module.exports = {
  CONFIRM_BEFORE_QUIT_SETTING,
  confirmBeforeQuitEnabled,
  createQuitConfirmation,
  quitPromptDetail,
};
