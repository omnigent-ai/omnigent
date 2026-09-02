import { describe, expect, it } from "vitest";
import { isReservedEscapeSequence } from "./keybindingPolicy";
import { parseKeybinding } from "./keybindingParser";

describe("keybinding policy", () => {
  it("reserves plain and modified Escape sequences", () => {
    expect(isReservedEscapeSequence(parseKeybinding("escape"))).toBe(true);
    expect(isReservedEscapeSequence(parseKeybinding("shift+escape"))).toBe(true);
    expect(isReservedEscapeSequence(parseKeybinding("enter"))).toBe(false);
  });
});
