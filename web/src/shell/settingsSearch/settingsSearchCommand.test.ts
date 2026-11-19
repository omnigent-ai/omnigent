import { describe, expect, it, vi } from "vitest";

import { clearSettingsSearchFocus, hasPendingSettingsSearchFocus } from "./settingsSearchFocus";
import { settingsSearchCommand, settingsSearchPath } from "./settingsSearchCommand";

describe("settingsSearchPath", () => {
  const inSettings = (pathname: string, search = "") => ({
    settingsLocation: { pathname, search },
    mobile: false,
  });

  it("stays on the current section, without a row", () => {
    expect(settingsSearchPath(inSettings("/settings/appearance"))).toBe("/settings/appearance");
  });

  it("keeps a nested Settings page and its query, so an open editor keeps its draft", () => {
    expect(settingsSearchPath(inSettings("/settings/custom-agents/new"))).toBe(
      "/settings/custom-agents/new",
    );
    expect(settingsSearchPath(inSettings("/settings/custom-agents/a1"))).toBe(
      "/settings/custom-agents/a1",
    );
    expect(settingsSearchPath(inSettings("/settings/harnesses/claude-native", "?tab=skills"))).toBe(
      "/settings/harnesses/claude-native?tab=skills",
    );
  });

  it("keeps the page on phones too", () => {
    expect(
      settingsSearchPath({
        settingsLocation: { pathname: "/settings/custom-agents/new", search: "" },
        mobile: true,
      }),
    ).toBe("/settings/custom-agents/new");
  });

  it("opens the default section on larger screens, and the menu on phones", () => {
    expect(settingsSearchPath({ settingsLocation: null, mobile: false })).toBe("/settings/general");
    expect(settingsSearchPath({ settingsLocation: null, mobile: true })).toBe("/settings");
  });
});

describe("settingsSearchCommand", () => {
  it("navigates and asks the field for focus when run", () => {
    const navigate = vi.fn();
    const command = settingsSearchCommand({ settingsLocation: null, mobile: false, navigate });
    command.run();
    expect(navigate).toHaveBeenCalledWith("/settings/general");
    expect(hasPendingSettingsSearchFocus()).toBe(true);
    clearSettingsSearchFocus();
  });

  it("drops only the row hash of a nested Settings page", () => {
    const navigate = vi.fn();
    settingsSearchCommand({
      settingsLocation: { pathname: "/settings/custom-agents/a1", search: "?view=config" },
      mobile: false,
      navigate,
    }).run();
    expect(navigate).toHaveBeenCalledWith("/settings/custom-agents/a1?view=config");
    clearSettingsSearchFocus();
  });
});
