import { beforeEach, describe, expect, it } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";
import {
  TERMINAL_RENDERER_STORAGE_KEY,
  readTerminalRendererMode,
  writeTerminalRendererMode,
} from "./terminalRendererPreferences";

beforeEach(() => localStorage.clear());

describe("composer shortcut portability", () => {
  it("exports, imports, and clears the device-local preference", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    expect(collectSettings()?.settings[COMPOSER_SEND_SHORTCUT_STORAGE_KEY]).toBe("true");

    applyImportedSettings({ version: 1, settings: {} });
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBeNull();

    applyImportedSettings({
      version: 1,
      settings: { [COMPOSER_SEND_SHORTCUT_STORAGE_KEY]: "true" },
    });
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBe("true");
  });
});

describe("terminal renderer portability", () => {
  it("carries the compatibility renderer across export and import", () => {
    // WHY: the renderer preference is the escape hatch for a corrupt WebGL
    // atlas — a device that exports its settings must not silently lose it.
    writeTerminalRendererMode("dom");
    expect(collectSettings()?.settings[TERMINAL_RENDERER_STORAGE_KEY]).toBe("dom");

    // Importing a default-renderer file overwrites rather than merges.
    applyImportedSettings({ version: 1, settings: {} });
    expect(readTerminalRendererMode()).toBe("auto");

    applyImportedSettings({
      version: 1,
      settings: { [TERMINAL_RENDERER_STORAGE_KEY]: "dom" },
    });
    expect(readTerminalRendererMode()).toBe("dom");
  });
});
