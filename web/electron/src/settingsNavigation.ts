import type { MenuItemConstructorOptions } from "electron";

export const SETTINGS_PATH = "/settings";
export const SETTINGS_ACCELERATOR = "CmdOrCtrl+,";

/** The parts of a BrowserWindow that focusedConnectedWindow reads. */
export interface ConnectedWindowCandidate {
  isDestroyed: () => boolean;
  webContents: { getURL: () => string };
}

/** A shell window's connection, as tracked by main.js. */
export interface ConnectedWindowState {
  origin: string | null;
  serverUrl?: string | null;
}

/** A menu item whose click handler takes no arguments. */
export interface ClickMenuItem extends MenuItemConstructorOptions {
  id: string;
  label: string;
  click: () => void;
}

/** Menu items for the macOS application menu. */
export interface MacApplicationMenuItems {
  about: MenuItemConstructorOptions;
  updates: MenuItemConstructorOptions[];
  settings: MenuItemConstructorOptions;
  quit: MenuItemConstructorOptions[];
}

/**
 * Return the focused window only while its connected app is visible.
 */
export function focusedConnectedWindow<Window extends ConnectedWindowCandidate>(
  focused: Window | null | undefined,
  windows: ReadonlyMap<Window, ConnectedWindowState>,
): Window | null {
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
 */
export function settingsMenuItem(
  openSettings: () => void,
): ClickMenuItem & { accelerator: string } {
  return {
    id: "open_settings",
    label: "Settings…",
    accelerator: SETTINGS_ACCELERATOR,
    click: openSettings,
  };
}

/**
 * Build the custom About item that opens the shell-owned About window.
 */
export function aboutMenuItem(productName: string, openAbout: () => void): ClickMenuItem {
  return {
    id: "open_about",
    label: `About ${productName}`,
    click: openAbout,
  };
}

/**
 * The conventional macOS application menu: About and update checks, Settings,
 * the standard app roles, then the shell's Quit items.
 */
export function macApplicationMenu(
  appName: string,
  { about, updates, settings, quit }: MacApplicationMenuItems,
): MenuItemConstructorOptions & { label: string; submenu: MenuItemConstructorOptions[] } {
  return {
    label: appName,
    submenu: [
      about,
      ...updates,
      { type: "separator" },
      settings,
      { type: "separator" },
      { role: "services" },
      { type: "separator" },
      { role: "hide" },
      { role: "hideOthers" },
      { role: "unhide" },
      { type: "separator" },
      ...quit,
    ],
  };
}
