import type { MenuItemConstructorOptions } from "electron";

// The Quit menu item (⌘Q / Ctrl+Q) asks before quitting, because quitting also
// disconnects the hosts this app runs and stops a local server it started.
// The window's page asks, in an in-app dialog. Only the menu item asks: Dock
// Quit, OS logout, and Restart to Update call app.quit() directly.

/** settings.json key; anything other than `false` keeps the prompt on. */
export const CONFIRM_BEFORE_QUIT_SETTING = "confirm_before_quit";

/** Settings as read from and written to settings.json. */
export type QuitSettings = Record<string, unknown>;

/** What quitting would stop. */
export interface RunningWork {
  hosts: number;
  localServer: boolean;
}

/** The part of a clicked checkbox menu item the warn toggle reads and resets. */
export interface CheckedMenuItem {
  checked: boolean;
}

export interface WarnBeforeQuitMenuItem extends MenuItemConstructorOptions {
  id: string;
  label: string;
  type: "checkbox";
  checked: boolean;
  click: (item: CheckedMenuItem) => void;
}

export interface QuitMenuItem extends MenuItemConstructorOptions {
  id: string;
  label: string;
  accelerator: string;
  click: () => void;
}

/** `ask` shows the prompt in a window's page; false when no page can. */
export interface QuitConfirmationDeps {
  isMac: boolean;
  appName: () => string;
  loadSettings: () => QuitSettings;
  saveSettings: (settings: QuitSettings) => void;
  runningWork: () => RunningWork;
  ask: (detail: string | null) => boolean;
  quit: () => void;
}

export interface QuitConfirmation {
  requestQuit: () => void;
  confirmQuit: (choice: { dontAskAgain: boolean }) => void;
  menuItems: () => [WarnBeforeQuitMenuItem, QuitMenuItem];
  quitStarted: () => void;
}

export function confirmBeforeQuitEnabled(settings: QuitSettings | null | undefined): boolean {
  return settings?.[CONFIRM_BEFORE_QUIT_SETTING] !== false;
}

/**
 * The prompt's detail line naming what quitting stops, or null when this app
 * runs nothing that would stop.
 */
export function quitPromptDetail({ hosts, localServer }: RunningWork): string | null {
  if (hosts > 0 && localServer) {
    return "Sessions running on this computer will go offline, and the local Omnigent server will stop.";
  }
  if (hosts > 0) return "Sessions running on this computer will go offline.";
  if (localServer) return "The local Omnigent server will stop.";
  return null;
}

export function createQuitConfirmation({
  isMac,
  appName,
  loadSettings,
  saveSettings,
  runningWork,
  ask,
  quit,
}: QuitConfirmationDeps): QuitConfirmation {
  // Once shutdown has started, a Cancel could not stop it, so Quit stops asking.
  let quitting = false;

  /** False when the setting could not be saved. */
  function setConfirmBeforeQuit(enabled: boolean): boolean {
    try {
      const settings = loadSettings();
      settings[CONFIRM_BEFORE_QUIT_SETTING] = enabled;
      saveSettings(settings);
      return true;
    } catch (err) {
      console.warn("[omnigent] could not save the warn-before-quitting setting:", err);
      return false;
    }
  }

  function startQuit() {
    quitting = true;
    quit();
  }

  /** Quit, asking first unless the user turned the prompt off or no page can ask. */
  function requestQuit() {
    if (
      quitting ||
      !confirmBeforeQuitEnabled(loadSettings()) ||
      !ask(quitPromptDetail(runningWork()))
    ) {
      startQuit();
    }
  }

  /**
   * The page's prompt was confirmed, optionally with "Don't ask again" checked.
   * The user said quit, so this quits even when that choice can't be saved.
   */
  function confirmQuit({ dontAskAgain }: { dontAskAgain: boolean }): void {
    if (dontAskAgain) setConfirmBeforeQuit(false);
    startQuit();
  }

  /**
   * The warn-before-quitting toggle and Quit itself, for the macOS app menu or
   * the File menu elsewhere.
   */
  function menuItems(): [WarnBeforeQuitMenuItem, QuitMenuItem] {
    return [
      {
        id: "confirm_before_quit",
        label: `Warn Before Quitting (${isMac ? "⌘Q" : "Ctrl+Q"})`,
        type: "checkbox",
        checked: confirmBeforeQuitEnabled(loadSettings()),
        // Electron flips `checked` before calling click; a failed save flips it back.
        click: (item) => {
          if (!setConfirmBeforeQuit(item.checked)) item.checked = !item.checked;
        },
      },
      {
        id: "quit",
        label: `Quit ${appName()}`,
        accelerator: "CmdOrCtrl+Q",
        click: () => requestQuit(),
      },
    ];
  }

  return {
    requestQuit,
    confirmQuit,
    menuItems,
    /** Another path (Dock Quit, Restart to Update) started shutting down. */
    quitStarted: () => {
      quitting = true;
    },
  };
}
