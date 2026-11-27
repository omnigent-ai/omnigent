import { describe, expect, it } from "vitest";

import {
  matchedPaneShortcut,
  PANE_SHORTCUT_IDS,
  paneKeyAria,
  paneKeyCaps,
  paneShortcutChords,
  paneShortcutLabel,
} from "./paneShortcuts";

const MAC = { mac: true, native: false, desktopMenu: false };
const LINUX = { mac: false, native: false, desktopMenu: false };

const press = (init: KeyboardEventInit) => new KeyboardEvent("keydown", init);

describe("pane shortcuts", () => {
  it("binds the split, focus, divider and equalize keys on the Mac only", () => {
    for (const id of PANE_SHORTCUT_IDS) {
      expect(paneShortcutChords(id, MAC), id).toHaveLength(1);
      if (id !== "terminal.toggleZoom") expect(paneShortcutChords(id, LINUX), id).toEqual([]);
    }
    expect(paneShortcutChords("terminal.toggleZoom", LINUX)).toHaveLength(1);
  });

  it("finds the pane key a keydown presses, and none for other chords", () => {
    expect(matchedPaneShortcut(press({ metaKey: true, code: "KeyD", key: "d" }), MAC)).toBe(
      "terminal.splitRight",
    );
    expect(
      matchedPaneShortcut(press({ metaKey: true, shiftKey: true, code: "KeyD", key: "D" }), MAC),
    ).toBe("terminal.splitDown");
    expect(matchedPaneShortcut(press({ metaKey: true, altKey: true, key: "ArrowLeft" }), MAC)).toBe(
      "terminal.focusLeft",
    );
    expect(
      matchedPaneShortcut(press({ metaKey: true, ctrlKey: true, key: "ArrowDown" }), MAC),
    ).toBe("terminal.resizeDown");
    expect(matchedPaneShortcut(press({ ctrlKey: true, shiftKey: true, key: "Enter" }), LINUX)).toBe(
      "terminal.toggleZoom",
    );
    // Ctrl+D is the shell's end of input off the Mac.
    expect(matchedPaneShortcut(press({ ctrlKey: true, code: "KeyD", key: "d" }), LINUX)).toBe(
      undefined,
    );
    expect(matchedPaneShortcut(press({ metaKey: true, key: "k" }), MAC)).toBeUndefined();
  });

  it("names each key as the pane command table does, with caps and aria for its chord", () => {
    expect(paneShortcutLabel("terminal.splitRight")).toBe("Split Right");
    expect(paneShortcutLabel("terminal.resizeLeft")).toBe("Move Divider Left");
    expect(paneKeyCaps("terminal.splitDown", MAC)).toEqual(["⌘", "⇧", "D"]);
    expect(paneKeyAria("terminal.splitDown", MAC)).toBe("Meta+Shift+D");
    expect(paneKeyCaps("terminal.splitDown", LINUX)).toEqual([]);
    expect(paneKeyAria("terminal.splitDown", LINUX)).toBeUndefined();
  });
});
