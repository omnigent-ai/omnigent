import { describe, expect, it } from "vitest";

import { paneKeyAria, paneKeyCaps } from "../paneShortcuts";
import { paneShortcutAria, paneShortcutKeys } from "./paneShortcutHints";

describe("paneShortcutHints", () => {
  it("shows the pane key a command has here, as caps and for assistive technology", () => {
    expect(paneShortcutKeys("terminal.toggleZoom")).toEqual(paneKeyCaps("terminal.toggleZoom"));
    expect(paneShortcutAria("terminal.toggleZoom")).toBe(paneKeyAria("terminal.toggleZoom"));
  });

  it("shows no hint for a command without a key here", () => {
    // jsdom isn't a Mac, where the split keys are the shell's.
    for (const command of ["terminal.splitRight", "close", "terminal.swapLeft", undefined]) {
      expect(paneShortcutKeys(command)).toBeUndefined();
      expect(paneShortcutAria(command)).toBeUndefined();
    }
  });
});
