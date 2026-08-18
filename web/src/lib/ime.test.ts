import { describe, expect, it } from "vitest";
import { isImeCompositionKeyEvent, isImeCompositionNativeKeyEvent } from "./ime";

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

describe("isImeCompositionNativeKeyEvent", () => {
  it("reads the IME flags straight off a native KeyboardEvent", () => {
    // The React-shaped sibling cannot be used from a DOM listener: there is
    // no `nativeEvent` to unwrap.
    expect(
      isImeCompositionNativeKeyEvent(new KeyboardEvent("keydown", { isComposing: true })),
    ).toBe(true);
    expect(isImeCompositionNativeKeyEvent(new KeyboardEvent("keydown", { key: "Enter" }))).toBe(
      false,
    );
  });

  it("keeps the keyCode 229 fallback for browsers that leave isComposing unset", () => {
    expect(isImeCompositionNativeKeyEvent({ keyCode: 229 })).toBe(true);
  });

  it("honours a caller-tracked composition flag", () => {
    // For callers that follow compositionstart/end themselves and see a key
    // event whose own flags say nothing.
    expect(isImeCompositionNativeKeyEvent({}, true)).toBe(true);
  });

  it("agrees with the React-shaped check on the same flags", () => {
    // One predicate, two shapes: the two must never drift apart.
    for (const flags of [
      {},
      { isComposing: true },
      { keyCode: 229 },
      { isComposing: false, keyCode: 13 },
    ]) {
      expect(isImeCompositionNativeKeyEvent(flags)).toBe(
        isImeCompositionKeyEvent({ nativeEvent: flags }),
      );
    }
  });
});
