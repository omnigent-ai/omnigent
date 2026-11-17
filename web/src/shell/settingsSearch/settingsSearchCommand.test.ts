import { describe, expect, it, vi } from "vitest";

import { clearSettingsSearchFocus, hasPendingSettingsSearchFocus } from "./settingsSearchFocus";
import { settingsSearchCommand, settingsSearchPath } from "./settingsSearchCommand";

describe("settingsSearchPath", () => {
  it("stays on the current section, without a row", () => {
    expect(settingsSearchPath({ currentSection: "appearance", mobile: false })).toBe(
      "/settings/appearance",
    );
  });

  it("opens the default section on larger screens, and the menu on phones", () => {
    expect(settingsSearchPath({ currentSection: null, mobile: false })).toBe("/settings/general");
    expect(settingsSearchPath({ currentSection: null, mobile: true })).toBe("/settings");
  });
});

describe("settingsSearchCommand", () => {
  it("navigates and asks the field for focus when run", () => {
    const navigate = vi.fn();
    const command = settingsSearchCommand({ currentSection: null, mobile: false, navigate });
    command.run();
    expect(navigate).toHaveBeenCalledWith("/settings/general");
    expect(hasPendingSettingsSearchFocus()).toBe(true);
    clearSettingsSearchFocus();
  });
});
