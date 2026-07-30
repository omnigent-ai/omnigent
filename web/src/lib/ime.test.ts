import { describe, expect, it } from "vitest";
import { isImeCompositionKeyEvent } from "./ime";

function keyEvent(nativeEvent: { isComposing?: boolean; keyCode?: number }) {
  return { nativeEvent };
}

describe("isImeCompositionKeyEvent", () => {
  it("returns true while the local composition flag is active", () => {
    expect(isImeCompositionKeyEvent(keyEvent({}), true)).toBe(true);
  });

  it("returns true when the native event is composing", () => {
    expect(isImeCompositionKeyEvent(keyEvent({ isComposing: true }))).toBe(true);
  });

  it("returns true for the keyCode 229 IME fallback", () => {
    expect(isImeCompositionKeyEvent(keyEvent({ keyCode: 229 }))).toBe(true);
  });

  it("returns false for ordinary key events", () => {
    expect(isImeCompositionKeyEvent(keyEvent({ isComposing: false, keyCode: 13 }))).toBe(false);
  });
});
