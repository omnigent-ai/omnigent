import { beforeEach, describe, expect, it } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";
import { DEFAULT_CUSTOM_THEME, readCustomTheme, writeCustomTheme } from "./customTheme";

beforeEach(() => localStorage.clear());

describe("custom theme portability", () => {
  it("preserves flat backgrounds and custom colors through export and import", () => {
    const theme = {
      ...DEFAULT_CUSTOM_THEME,
      accent: "#2563eb",
      darkTint: "#24283b",
      flatBackground: true,
    };
    writeCustomTheme(theme);
    const exported = collectSettings()!;
    localStorage.clear();

    applyImportedSettings(JSON.parse(JSON.stringify(exported)));

    expect(readCustomTheme()).toEqual(theme);
  });
});

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
