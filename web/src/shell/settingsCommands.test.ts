import { describe, expect, it, vi } from "vitest";

import { isCommandListed } from "@/store/paletteCommands";
import { currentChoiceLabel, listedChoices, matchedChoice } from "@/store/paletteSettings";

import {
  appearanceModeSetting,
  paneCornersSetting,
  terminalThemeSetting,
} from "./settingsCommands";
import type { PaneCornersState } from "./useFloatingPanes";

const FLOATING_BY_POLICY: PaneCornersState = {
  available: true,
  choice: "automatic",
  policyFloating: true,
  floating: true,
};

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

  it("names what Automatic gives now, from the server's policy, whatever is chosen", () => {
    expect(currentChoiceLabel(paneCornersSetting(FLOATING_BY_POLICY))).toBe(
      "Automatic (Floating panes)",
    );
    const sharpChosen = paneCornersSetting({
      ...FLOATING_BY_POLICY,
      choice: "sharp",
      floating: false,
    });
    expect(currentChoiceLabel(sharpChosen)).toBe("Sharp corners");
    expect(listedChoices(sharpChosen).map((choice) => choice.label)).toEqual([
      "Automatic (Floating panes)",
      "Floating panes",
      "Sharp corners",
    ]);
    const policyOff = paneCornersSetting({
      ...FLOATING_BY_POLICY,
      policyFloating: false,
      floating: false,
    });
    expect(currentChoiceLabel(policyOff)).toBe("Automatic (Sharp corners)");
  });

  it("switches to sharp corners from a typed name and writes only known choices", () => {
    const write = vi.fn();
    const setting = paneCornersSetting(FLOATING_BY_POLICY, write);
    expect(matchedChoice(setting, "sharp corners")).toBe("sharp");
    setting.choose("sharp");
    setting.choose("square");
    expect(write).toHaveBeenCalledExactlyOnceWith("sharp");
  });

  it("hides the pane corners row where floating panes can't show", () => {
    const setting = paneCornersSetting({
      ...FLOATING_BY_POLICY,
      available: false,
      floating: false,
    });
    expect(isCommandListed(setting.availability)).toBe(false);
    expect(isCommandListed(paneCornersSetting(FLOATING_BY_POLICY).availability)).toBe(true);
  });
});
