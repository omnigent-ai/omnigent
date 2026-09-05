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
 * Build the custom About item that opens the shell-owned About window.
 *
 * @param {string} productName
 * @param {() => void} openAbout
 * @returns {Electron.MenuItemConstructorOptions}
 */
function aboutMenuItem(productName, openAbout) {
  return {
    id: "open_about",
    label: `About ${productName}`,
    click: openAbout,
  };
}

/**
 * Add About and Settings to the conventional macOS application menu.
 *
 * @param {string} appName
 * @param {Electron.MenuItemConstructorOptions} aboutItem
 * @param {Electron.MenuItemConstructorOptions} settingsItem
 * @returns {Electron.MenuItemConstructorOptions}
 */
function macApplicationMenu(appName, aboutItem, settingsItem) {
  return {
    label: appName,
    submenu: [
      aboutItem,
      { type: "separator" },
      settingsItem,
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
  aboutMenuItem,
  macApplicationMenu,
  settingsMenuItem,
};
