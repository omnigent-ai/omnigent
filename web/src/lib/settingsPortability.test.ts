import { beforeEach, describe, expect, it } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";

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
