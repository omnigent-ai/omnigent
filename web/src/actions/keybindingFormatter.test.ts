import { describe, expect, it } from "vitest";
import { formatKeybinding, keybindingParts } from "./keybindingFormatter";
import { parseKeybinding } from "./keybindingParser";

describe("keybinding formatter", () => {
  it("uses macOS modifier glyphs and readable key glyphs", () => {
    expect(formatKeybinding(parseKeybinding("primary+alt+arrowup"), { isMac: true })).toBe("⌘⌥↑");
    expect(formatKeybinding(parseKeybinding("shift+enter"), { isMac: true })).toBe("⇧↵");
    expect(formatKeybinding(parseKeybinding("backspace"), { isMac: true })).toBe("⌫");
    expect(formatKeybinding(parseKeybinding("delete"), { isMac: true })).toBe("⌦");
    expect(formatKeybinding(parseKeybinding("tab"), { isMac: true })).toBe("⇥");
    expect(formatKeybinding(parseKeybinding("escape"), { isMac: true })).toBe("Esc");
  });

  it("uses text modifiers on non-Mac platforms", () => {
    expect(formatKeybinding(parseKeybinding("mod+alt+[BracketRight]"), { isMac: false })).toBe(
      "Ctrl+Alt+]",
    );
  });

  it("formats physical letter and digit codes", () => {
    expect(formatKeybinding(parseKeybinding("primary+[KeyV]"), { isMac: false })).toBe("Ctrl+V");
    expect(formatKeybinding(parseKeybinding("primary+[Digit1]"), { isMac: false })).toBe("Ctrl+1");
  });

  it("formats chord strokes separately", () => {
    const sequence = parseKeybinding("mod+k mod+s");
    expect(formatKeybinding(sequence, { isMac: false })).toBe("Ctrl+K Ctrl+S");
    expect(keybindingParts(sequence, { isMac: true })).toEqual([
      ["⌘", "K"],
      ["⌘", "S"],
    ]);
  });
});
