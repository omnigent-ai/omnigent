"use strict";

// Optional system-wide shortcut that brings the app to the front from any app.
// Off by default. The chord stays in settings.json while the shortcut is off.

/** settings.json key: `{ enabled, accelerator }`. */
const GLOBAL_SHORTCUT_SETTING = "global_shortcut";
const DEFAULT_ACCELERATOR = "Alt+Space";

/**
 * @param {{
 *   globalShortcut: Electron.GlobalShortcut,
 *   loadSettings: () => Record<string, unknown>,
 *   saveSettings: (settings: Record<string, unknown>) => void,
 *   show: () => void,
 * }} deps
 */
function createGlobalShowShortcut({ globalShortcut, loadSettings, saveSettings, show }) {
  /** @type {string | null} */
  let registeredAccelerator = null;

  /** @returns {{ enabled: boolean, accelerator: string }} */
  function current() {
    const saved = loadSettings()[GLOBAL_SHORTCUT_SETTING];
    return {
      enabled: saved?.enabled === true,
      accelerator:
        typeof saved?.accelerator === "string" && saved.accelerator !== ""
          ? saved.accelerator
          : DEFAULT_ACCELERATOR,
    };
  }

  function unregister() {
    if (registeredAccelerator === null) return;
    globalShortcut.unregister(registeredAccelerator);
    registeredAccelerator = null;
  }

  /** False when the chord is invalid or taken; Electron may refuse it or throw. */
  function register(accelerator) {
    try {
      if (!globalShortcut.register(accelerator, show)) return false;
    } catch {
      return false;
    }
    registeredAccelerator = accelerator;
    return true;
  }

  /** Makes the registered chord match the saved setting. */
  function apply() {
    unregister();
    const { enabled, accelerator } = current();
    if (enabled) register(accelerator);
  }

  /**
   * Saves the setting when its chord registers. Otherwise re-applies the saved
   * setting and returns the error.
   *
   * @param {{ enabled: boolean, accelerator: string }} setting
   * @returns {{ ok: true } | { ok: false, error: string }}
   */
  function set({ enabled, accelerator }) {
    unregister();
    if (enabled === true && !register(accelerator)) {
      apply();
      return {
        ok: false,
        error: "Couldn't use that shortcut. It may be invalid or already in use by another app.",
      };
    }
    const settings = loadSettings();
    settings[GLOBAL_SHORTCUT_SETTING] = { enabled: enabled === true, accelerator };
    saveSettings(settings);
    return { ok: true };
  }

  return { current, apply, set };
}

module.exports = { createGlobalShowShortcut };
