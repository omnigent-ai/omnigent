import { describe, expect, it, vi } from "vitest";

import { currentChoiceLabel, listedChoices } from "@/store/paletteSettings";

import {
  ALWAYS_STEER_SETTING,
  appearanceModeSetting,
  settingSwitchSetting,
  terminalThemeSetting,
} from "./settingsCommands";

describe("settings rows", () => {
  it("names the appearance mode as Settings does, following a Databricks host", () => {
    const setMode = vi.fn();
    const setting = appearanceModeSetting("system", false, setMode);
    expect(currentChoiceLabel(setting)).toBe("System");
    expect(currentChoiceLabel(appearanceModeSetting("system", true, setMode))).toBe(
      "Use Databricks setting",
    );
    setting.choose("dark");
    expect(setMode).toHaveBeenCalledWith("dark");
  });

  it("offers the terminal theme's values and ignores one it doesn't know", () => {
    const write = vi.fn();
    const setting = terminalThemeSetting("auto", write);
    expect(listedChoices(setting).map((choice) => choice.label)).toEqual([
      "Match app",
      "Light",
      "Dark",
    ]);
    setting.choose("light");
    setting.choose("sepia");
    expect(write).toHaveBeenCalledExactlyOnceWith("light");
  });

  it("turns a switch on and off through its write", () => {
    const write = vi.fn();
    const setting = settingSwitchSetting(ALWAYS_STEER_SETTING, true, write);
    expect(currentChoiceLabel(setting)).toBe("On");
    setting.choose("off");
    expect(write).toHaveBeenCalledWith(false);
  });
});
