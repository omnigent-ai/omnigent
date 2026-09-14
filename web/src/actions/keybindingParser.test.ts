import { describe, expect, it } from "vitest";
import { parseKeybinding, serializeKeybinding } from "./keybindingParser";

describe("keybinding parser", () => {
  it("normalizes modifier aliases and logical keys", () => {
    expect(parseKeybinding("CommandOrControl+Shift+K")).toEqual([
      {
        modifiers: ["mod", "shift"],
        key: { kind: "key", value: "k" },
      },
    ]);
    expect(serializeKeybinding(parseKeybinding("CTRL+Esc"))).toBe("ctrl+Escape");
    expect(serializeKeybinding(parseKeybinding("primary+K"))).toBe("primary+k");
  });

  it("preserves validated physical key codes", () => {
    const parsed = parseKeybinding("mod+alt+[BracketLeft]");
    expect(parsed[0]?.key).toEqual({ kind: "code", value: "BracketLeft" });
    expect(serializeKeybinding(parsed)).toBe("mod+alt+[BracketLeft]");
    expect(() => parseKeybinding("mod+[Foo]")).toThrow("Unknown physical key code");
  });

  it("rejects multi-stroke shortcuts", () => {
    expect(() => parseKeybinding("mod+k mod+s")).toThrow("one key combination");
  });

  it("normalizes named, plus, and function keys", () => {
    expect(serializeKeybinding(parseKeybinding("shift+return"))).toBe("shift+Enter");
    expect(serializeKeybinding(parseKeybinding("mod+f12"))).toBe("mod+F12");
    expect(serializeKeybinding(parseKeybinding("space"))).toBe("space");
    expect(serializeKeybinding(parseKeybinding("primary+plus"))).toBe("primary+plus");
  });

  it("rejects contradictory and duplicate modifiers", () => {
    expect(() => parseKeybinding("mod+ctrl+k")).toThrow();
    expect(() => parseKeybinding("primary+meta+k")).toThrow();
    expect(() =>
      serializeKeybinding([{ modifiers: ["ctrl", "ctrl"], key: { kind: "key", value: "k" } }]),
    ).toThrow("duplicate modifiers");
  });

  it.each([
    "",
    "mod+",
    "mod+mod+k",
    "k+v",
    "mod+[bad-code",
    "mod+DefinitelyNotAKey",
    "mod+k mod+s",
    "mod+k mod+s mod+p",
  ])("rejects invalid binding %j", (source) => {
    expect(() => parseKeybinding(source)).toThrow();
  });
});
