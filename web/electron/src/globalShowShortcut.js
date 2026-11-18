"use strict";

// The shell's system-wide shortcuts, such as the optional one that brings the app to the
// front from any app. A chord stays in settings.json while its shortcut is off.

/**
 * Every system-wide shortcut, by id. Callers name a shortcut by id, never by its settings.json
 * key, so two shortcuts can't overwrite each other's `{ enabled, accelerator }`.
 */
const GLOBAL_SHORTCUTS = Object.freeze({
  show: Object.freeze({
    settingKey: "global_shortcut",
    defaultAccelerator: "Alt+Space",
    defaultEnabled: false,
  }),
  floatingComposer: Object.freeze({
    settingKey: "floating_composer_shortcut",
    defaultAccelerator: "CommandOrControl+Shift+Space",
    defaultEnabled: true,
  }),
});
const UNAVAILABLE_ERROR =
  "Couldn't use that shortcut. It may be invalid or already in use by another app.";
const CLAIMED_ERROR = "Another Omnigent shortcut already uses that chord. Choose a different one.";
const NOT_WORKING_ERROR =
  "This shortcut isn't working. Another app may be using it, so choose a different one.";
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
 *   id?: keyof typeof GLOBAL_SHORTCUTS,
 *   globalShortcut: Electron.GlobalShortcut,
 *   loadSettings: () => Record<string, unknown>,
 *   saveSettings: (settings: Record<string, unknown>) => void,
 *   show: () => void,
 *   isMac: boolean,
 *   claimedElsewhere?: (accelerator: string) => boolean,
 * }} deps `claimedElsewhere` says whether another of the shell's shortcuts holds a chord.
 */
function createGlobalShowShortcut({
  id = "show",
  globalShortcut,
  loadSettings,
  saveSettings,
  show,
  isMac,
  claimedElsewhere = () => false,
}) {
  const definition = GLOBAL_SHORTCUTS[id];
  if (!definition) throw new Error(`Unknown global shortcut: ${id}`);
  /** @type {string | null} */
  let registeredAccelerator = null;
  // Why the saved shortcut is on but its chord didn't register at launch, else null.
  /** @type {string | null} */
  let launchError = null;

  /** @returns {{ enabled: boolean, accelerator: string }} */
  function saved() {
    const stored = loadSettings()[definition.settingKey];
    return {
      enabled: typeof stored?.enabled === "boolean" ? stored.enabled : definition.defaultEnabled,
      accelerator:
        typeof stored?.accelerator === "string" && stored.accelerator !== ""
          ? stored.accelerator
          : definition.defaultAccelerator,
    };
  }

  /**
   * Whether this shortcut holds `accelerator`: registered now, or saved as on. An unsaved
   * default claims nothing, so a chord the user chose elsewhere wins over it.
   */
  function claims(accelerator) {
    if (registeredAccelerator === accelerator) return true;
    const stored = loadSettings()[definition.settingKey];
    return stored?.enabled === true && stored.accelerator === accelerator;
  }

  /**
   * The saved setting, with an error while it is on but not registered.
   *
   * @returns {{ enabled: boolean, accelerator: string, error?: string }}
   */
  function current() {
    const setting = saved();
    if (launchError === null || !setting.enabled) return setting;
    return { ...setting, error: launchError };
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
    if (!enabled) launchError = null;
    else if (claimedElsewhere(accelerator)) launchError = CLAIMED_ERROR;
    else launchError = register(accelerator) ? null : NOT_WORKING_ERROR;
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
    if (claimedElsewhere(accelerator)) return { ok: false, error: CLAIMED_ERROR };
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
    launchError = null;
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
    launchError = null;
    return { ok: true, setting };
  }

  /** Writes the setting to settings.json; false when that fails. */
  function store(setting) {
    try {
      const settings = loadSettings();
      settings[definition.settingKey] = setting;
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

  return { current, apply, set, record, cancelRecording, claims };
}

/**
 * The shell's shortcuts by id, each pressing its own action, built together so none can take
 * a chord another holds.
 *
 * @param {Omit<Parameters<typeof createGlobalShowShortcut>[0], "id" | "show" | "claimedElsewhere">
 *   & { presses: Partial<Record<keyof typeof GLOBAL_SHORTCUTS, () => void>> }} deps
 */
function createGlobalShortcuts({ presses, ...deps }) {
  /** @type {Record<string, ReturnType<typeof createGlobalShowShortcut>>} */
  const shortcuts = {};
  for (const [id, show] of Object.entries(presses)) {
    shortcuts[id] = createGlobalShowShortcut({
      ...deps,
      id,
      show,
      claimedElsewhere: (accelerator) =>
        Object.entries(shortcuts).some(
          ([other, shortcut]) => other !== id && shortcut.claims(accelerator),
        ),
    });
  }
  return shortcuts;
}

/** The chord as the user reads it: ⌃⌥⇧⌘ glyphs on macOS, `Ctrl+Alt+Space` elsewhere. */
function acceleratorLabel(accelerator, isMac) {
  const parts = accelerator.split("+");
  if (isMac) return parts.map((part) => MAC_MODIFIER_GLYPHS.get(part) ?? part).join("");
  return parts.map((part) => (part === "Control" ? "Ctrl" : part)).join("+");
}

module.exports = {
  GLOBAL_SHORTCUTS,
  acceleratorLabel,
  createGlobalShortcuts,
  createGlobalShowShortcut,
};
