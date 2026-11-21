import { SquareTerminalIcon } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { HIDDEN, unavailableBecause } from "./paletteCommands";
import {
  booleanSetting,
  currentChoiceLabel,
  isChoiceAvailable,
  listedChoices,
  matchedChoice,
  type PaletteSetting,
  settingCommand,
} from "./paletteSettings";

const ENGINE: PaletteSetting = {
  id: "terminal-engine",
  owner: "app",
  label: "Terminal engine",
  icon: SquareTerminalIcon,
  keywords: ["renderer"],
  current: "automatic",
  choices: [
    { value: "automatic", label: "Automatic (xterm.js)" },
    { value: "xterm", label: "xterm.js" },
    { value: "ghostty", label: "Ghostty (experimental)" },
    { value: "webgl", label: "WebGL", availability: HIDDEN },
    { value: "canvas", label: "Canvas", availability: unavailableBecause("Needs a GPU") },
  ],
  choose: vi.fn(),
};

describe("settingCommand", () => {
  it("is one row named for the setting, bound to its owner, found by its listed values", () => {
    const command = settingCommand(ENGINE);
    expect(command).toMatchObject({ id: "setting:terminal-engine:app", label: "Terminal engine" });
    expect(command.setting).toBe(ENGINE);
    expect(command.keywords).toEqual(
      expect.arrayContaining(["renderer", "Ghostty (experimental)", "Canvas"]),
    );
    // A hidden value isn't listed, so it doesn't find the setting either.
    expect(command.keywords).not.toContain("WebGL");
    expect(settingCommand({ ...ENGINE, owner: "composer-2" }).id).toBe(
      "setting:terminal-engine:composer-2",
    );
  });

  it("runs nothing from the row itself", () => {
    settingCommand(ENGINE).run();
    expect(ENGINE.choose).not.toHaveBeenCalled();
  });
});

describe("the setting's values", () => {
  it("lists greyed values but not hidden ones, and only an available value can be chosen", () => {
    expect(listedChoices(ENGINE).map((choice) => choice.value)).toEqual([
      "automatic",
      "xterm",
      "ghostty",
      "canvas",
    ]);
    expect(isChoiceAvailable(ENGINE, "ghostty")).toBe(true);
    expect(isChoiceAvailable(ENGINE, "canvas")).toBe(false);
    expect(isChoiceAvailable(ENGINE, "webgl")).toBe(false);
    expect(isChoiceAvailable(ENGINE, "missing")).toBe(false);
  });

  it("shows the current value by its label, and nothing when it isn't one of the values", () => {
    expect(currentChoiceLabel(ENGINE)).toBe("Automatic (xterm.js)");
    expect(currentChoiceLabel({ ...ENGINE, current: "" })).toBeUndefined();
  });
});

describe("booleanSetting", () => {
  it("is On or Off, writing the chosen state", () => {
    const write = vi.fn();
    const setting = booleanSetting({
      id: "always-steer",
      owner: "app",
      label: "Always steer",
      icon: SquareTerminalIcon,
      keywords: [],
      enabled: true,
      write,
    });
    expect(setting.choices.map((choice) => choice.label)).toEqual(["On", "Off"]);
    expect(currentChoiceLabel(setting)).toBe("On");
    void setting.choose("off");
    expect(write).toHaveBeenCalledWith(false);
  });
});

describe("matchedChoice", () => {
  it.each([
    ["ghostty", "ghostty"],
    ["terminal ghostty", "ghostty"],
    ["xterm", "xterm"],
    ["auto", "automatic"],
    ["canvas", "canvas"],
  ])("names the value %s → %s", (query, value) => {
    expect(matchedChoice(ENGINE, query)).toBe(value);
  });

  it.each(["", "terminal", "terminal engine", "term", "webgl"])(
    "names no value for %j: the setting itself, a word inside a value, or a hidden one",
    (query) => {
      expect(matchedChoice(ENGINE, query)).toBeNull();
    },
  );
});
