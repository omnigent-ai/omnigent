"use strict";

const SETTINGS_PATH = "/settings";
const SETTINGS_ACCELERATOR = "CmdOrCtrl+,";

/**
 * Return the focused window only while its connected app is visible.
 *
 * @param {Electron.BrowserWindow | null | undefined} focused
 * @param {Map<Electron.BrowserWindow, {origin: string | null, serverUrl?: string | null}>} windows
 * @returns {Electron.BrowserWindow | null}
 */
function focusedConnectedWindow(focused, windows) {
  if (!focused || !windows.has(focused) || focused.isDestroyed()) return null;
  const state = windows.get(focused);
  if (!state?.origin || !state.serverUrl) return null;
  try {
    if (new URL(focused.webContents.getURL()).origin !== state.origin) return null;
  } catch {
    return null;
  }
  return focused;
}

/**
 * Build the shared native Settings menu item.
 *
 * @param {() => void} openSettings
 * @returns {Electron.MenuItemConstructorOptions}
 */
function settingsMenuItem(openSettings) {
  return {
    id: "open_settings",
    label: "Settings…",
    accelerator: SETTINGS_ACCELERATOR,
    click: openSettings,
  };
}

/**
 * Add Settings to the conventional macOS application menu.
 *
 * @param {string} appName
 * @param {Electron.MenuItemConstructorOptions} item
 * @returns {Electron.MenuItemConstructorOptions}
 */
function macApplicationMenu(appName, item) {
  return {
    label: appName,
    submenu: [
      { role: "about" },
      { type: "separator" },
      item,
      { type: "separator" },
      { role: "services" },
      { type: "separator" },
      { role: "hide" },
      { role: "hideOthers" },
      { role: "unhide" },
      { type: "separator" },
      { role: "quit" },
    ],
  };
}

module.exports = {
  SETTINGS_ACCELERATOR,
  SETTINGS_PATH,
  focusedConnectedWindow,
  macApplicationMenu,
  settingsMenuItem,
};
