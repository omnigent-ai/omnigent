"use strict";

// Optional system-wide shortcut that brings the app to the front from any app.
// Off by default. The chord stays in settings.json while the shortcut is off.

/** settings.json key: `{ enabled, accelerator }`. */
const GLOBAL_SHORTCUT_SETTING = "global_shortcut";
const DEFAULT_ACCELERATOR = "Alt+Space";
const UNAVAILABLE_ERROR =
  "Couldn't use that shortcut. It may be invalid or already in use by another app.";
const SAVE_ERROR = "Couldn't save the shortcut. Try again.";

const NAMED_KEYS = new Map([
  ["Space", "Space"],
  ["ArrowUp", "Up"],
  ["ArrowDown", "Down"],
  ["ArrowLeft", "Left"],
  ["ArrowRight", "Right"],
  ["Enter", "Enter"],
  ["Tab", "Tab"],
]);

const MAC_MODIFIER_GLYPHS = new Map([
  ["Control", "⌃"],
  ["Alt", "⌥"],
  ["Shift", "⇧"],
  ["Command", "⌘"],
]);

/** Electron's name for a `KeyboardEvent.code`, or undefined if a global shortcut can't use it. */
function acceleratorKeyName(code) {
  if (/^(Key[A-Z]|Digit[0-9])$/.test(code)) return code.slice(-1);
  if (/^F(1|[2-9]|1\d|2[0-4])$/.test(code)) return code;
  return NAMED_KEYS.get(code);
}

/**
 * The accelerator for a `before-input-event` key press, or undefined unless it has
 * Ctrl, Alt, or Cmd and a usable key.
 *
 * @param {Electron.Input} input
 * @param {boolean} isMac
 * @returns {string | undefined}
 */
function acceleratorFromInput(input, isMac) {
  const key = acceleratorKeyName(input.code);
  if (key === undefined || !(input.control || input.alt || input.meta)) return undefined;
  const modifiers = [];
  if (input.control) modifiers.push("Control");
  if (input.alt) modifiers.push("Alt");
  if (input.shift) modifiers.push("Shift");
  if (input.meta) modifiers.push(isMac ? "Command" : "Super");
  return [...modifiers, key].join("+");
}

/**
 * @param {{
 *   globalShortcut: Electron.GlobalShortcut,
 *   loadSettings: () => Record<string, unknown>,
 *   saveSettings: (settings: Record<string, unknown>) => void,
 *   show: () => void,
 *   isMac: boolean,
 * }} deps
 */
function createGlobalShowShortcut({ globalShortcut, loadSettings, saveSettings, show, isMac }) {
  /** @type {string | null} */
  let registeredAccelerator = null;
  // The saved shortcut is on, but its chord didn't register at launch (another app may hold it).
  let launchRegistrationFailed = false;

  /** @returns {{ enabled: boolean, accelerator: string }} */
  function saved() {
    const stored = loadSettings()[GLOBAL_SHORTCUT_SETTING];
    return {
      enabled: stored?.enabled === true,
      accelerator:
        typeof stored?.accelerator === "string" && stored.accelerator !== ""
          ? stored.accelerator
          : DEFAULT_ACCELERATOR,
    };
  }

  /**
   * The saved setting, with an error while it is on but not registered.
   *
   * @returns {{ enabled: boolean, accelerator: string, error?: string }}
   */
  function current() {
    const setting = saved();
    if (!launchRegistrationFailed || !setting.enabled) return setting;
    return {
      ...setting,
      error: "This shortcut isn't working. Another app may be using it, so choose a different one.",
    };
  }

  function unregister() {
    if (registeredAccelerator === null) return;
    globalShortcut.unregister(registeredAccelerator);
    registeredAccelerator = null;
  }

  /** False when the chord is invalid or taken; Electron may refuse it or throw. */
  function tryRegister(accelerator) {
    try {
      return globalShortcut.register(accelerator, show) === true;
    } catch {
      return false;
    }
  }

  function register(accelerator) {
    if (!tryRegister(accelerator)) return false;
    registeredAccelerator = accelerator;
    return true;
  }

  /** Makes the registered chord match the saved setting. */
  function apply() {
    unregister();
    const { enabled, accelerator } = saved();
    launchRegistrationFailed = enabled && !register(accelerator);
  }

  /**
   * Saves `{ enabled, accelerator }`, or turns the shortcut off for `{ enabled: false }`.
   * A chord that isn't registered yet is approved by the user and tried first, even while
   * the shortcut is off, so an unusable one is never saved. The registered chord changes
   * only after the save, so a failure leaves it.
   *
   * @param {{ enabled: boolean, accelerator?: string }} change
   * @param {(accelerator: string) => Promise<boolean>} approve Asks the user before a chord
   *   registers, since the page asking can't grant itself system-wide keys.
   * @returns {Promise<
   *   | { ok: true, setting: { enabled: boolean, accelerator: string } }
   *   | { ok: false, error: string }
   * >}
   */
  async function set(change, approve) {
    if (typeof change?.accelerator !== "string") return turnOff();
    const on = change.enabled === true;
    const { accelerator } = change;
    const needsApproval = accelerator !== registeredAccelerator;
    if (needsApproval && !(await approve?.(accelerator))) {
      return { ok: false, error: "The shortcut wasn't changed." };
    }
    // Another window may have released the chord while the user decided.
    const candidate = accelerator !== registeredAccelerator;
    if (candidate && !needsApproval) return { ok: false, error: "The shortcut wasn't changed." };
    const previous = registeredAccelerator;
    // A new chord registers beside the previous one, which stays until the save succeeds.
    if (candidate && !tryRegister(accelerator)) return { ok: false, error: UNAVAILABLE_ERROR };
    const setting = { enabled: on, accelerator };
    if (!store(setting)) {
      if (candidate) globalShortcut.unregister(accelerator);
      return { ok: false, error: SAVE_ERROR };
    }
    if (previous !== null && (previous !== accelerator || !on)) unregister();
    if (on) registeredAccelerator = accelerator;
    else if (candidate) globalShortcut.unregister(accelerator);
    launchRegistrationFailed = false;
    return { ok: true, setting };
  }

  /**
   * Turns the shortcut off and keeps its saved chord, without asking or registering, so a
   * Settings window still showing an older chord can't replace it.
   */
  function turnOff() {
    const setting = { enabled: false, accelerator: saved().accelerator };
    if (!store(setting)) return { ok: false, error: SAVE_ERROR };
    unregister();
    launchRegistrationFailed = false;
    return { ok: true, setting };
  }

  /** Writes the setting to settings.json; false when that fails. */
  function store(setting) {
    try {
      const settings = loadSettings();
      settings[GLOBAL_SHORTCUT_SETTING] = setting;
      saveSettings(settings);
      return true;
    } catch (err) {
      console.warn("[omnigent] could not save the global shortcut:", err);
      return false;
    }
  }

  /** @type {(() => void) | null} */
  let stopRecording = null;

  /**
   * Takes the next chord pressed in `contents` before the page and the app menu see it,
   * so recording one can't also run an app shortcut such as Cmd+K. Resolves the
   * accelerator, or null when Escape, cancelRecording, or another recording ends it.
   *
   * @param {Electron.WebContents} contents
   * @returns {Promise<string | null>}
   */
  function record(contents) {
    stopRecording?.();
    return new Promise((resolve) => {
      const finish = (accelerator) => {
        contents.removeListener("before-input-event", onInput);
        contents.removeListener("destroyed", stop);
        contents.removeListener("did-start-navigation", onNavigate);
        contents.removeListener("render-process-gone", stop);
        if (stopRecording === stop) stopRecording = null;
        resolve(accelerator);
      };
      const stop = () => finish(null);
      const onInput = (event, input) => {
        if (input.type !== "keyDown") return;
        if (input.key === "Escape") {
          event.preventDefault();
          stop();
          return;
        }
        const accelerator = acceleratorFromInput(input, isMac);
        // Other keys reach the page, so Tab still moves focus, which ends the recording.
        if (accelerator === undefined) return;
        event.preventDefault();
        finish(accelerator);
      };
      // A reload or navigation replaces the page that asked, so its recording ends with it.
      const onNavigate = (details, _url, isInPlace, isMainFrame) => {
        const mainFrame = details?.isMainFrame ?? isMainFrame;
        const sameDocument = details?.isSameDocument ?? isInPlace;
        if (mainFrame && !sameDocument) stop();
      };
      contents.on("before-input-event", onInput);
      contents.once("destroyed", stop);
      contents.on("did-start-navigation", onNavigate);
      contents.on("render-process-gone", stop);
      stopRecording = stop;
    });
  }

  function cancelRecording() {
    stopRecording?.();
  }

  return { current, apply, set, record, cancelRecording };
}

/** The chord as the user reads it: ⌃⌥⇧⌘ glyphs on macOS, `Ctrl+Alt+Space` elsewhere. */
function acceleratorLabel(accelerator, isMac) {
  const parts = accelerator.split("+");
  if (isMac) return parts.map((part) => MAC_MODIFIER_GLYPHS.get(part) ?? part).join("");
  return parts.map((part) => (part === "Control" ? "Ctrl" : part)).join("+");
}

module.exports = { acceleratorLabel, createGlobalShowShortcut };
