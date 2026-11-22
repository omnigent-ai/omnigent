import { describe, expect, it } from "vitest";

import { ARIA_MOD_KEY } from "@/components/KeyboardShortcut";
import { chordKeyLabel } from "@/lib/shortcuts/chord";
import { activeShortcutChords, shortcutKeys } from "@/lib/shortcuts/registry";

import { paneShortcutAria, paneShortcutKeys } from "./paneShortcutHints";

describe("paneShortcutHints", () => {
  it("shows the registry's chord for the split commands", () => {
    expect(paneShortcutKeys("terminal.splitRight")).toEqual(shortcutKeys("terminal.splitRight"));
    expect(paneShortcutKeys("terminal.splitDown")).toEqual(shortcutKeys("terminal.splitDown"));
  });

  it("names the same chord for assistive technology", () => {
    const [chord] = activeShortcutChords("terminal.splitRight");
    expect(paneShortcutAria("terminal.splitRight")).toBe(
      `${ARIA_MOD_KEY}+Alt+Shift+${chordKeyLabel(chord!)}`,
    );
    expect(paneShortcutAria("terminal.splitDown")).toBe(
      `${ARIA_MOD_KEY}+Alt+${chordKeyLabel(activeShortcutChords("terminal.splitDown")[0]!)}`,
    );
  });

  it("shows no hint for a pane command without a chord", () => {
    for (const command of ["terminal.toggleZoom", "close", "terminal.swapLeft", undefined]) {
      expect(paneShortcutKeys(command)).toBeUndefined();
      expect(paneShortcutAria(command)).toBeUndefined();
    }
  });
});
