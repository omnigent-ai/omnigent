import { afterEach, describe, expect, it, vi } from "vitest";

import { clearPointerFocus, isKeyboardFocused, markPointerFocus } from "./stripTabFocus";

/** jsdom never matches `:focus-visible`, so the test decides when the browser shows focus. */
function trigger(focusVisible: boolean): HTMLButtonElement {
  const button = document.createElement("button");
  vi.spyOn(button, "matches").mockImplementation(
    (selector) => selector === ":focus-visible" && focusVisible,
  );
  return button;
}

afterEach(() => vi.restoreAllMocks());

describe("isKeyboardFocused", () => {
  it("is true for focus the browser shows that no pointer press put there", () => {
    expect(isKeyboardFocused(trigger(true))).toBe(true);
  });

  it("is false for focus from a pointer press, though the browser shows it", () => {
    const pressed = trigger(true);
    markPointerFocus(pressed);
    expect(isKeyboardFocused(pressed)).toBe(false);
  });

  it("becomes true once a key is pressed on a trigger the pointer focused", () => {
    const pressed = trigger(true);
    markPointerFocus(pressed);
    clearPointerFocus(pressed);
    expect(isKeyboardFocused(pressed)).toBe(true);
  });

  it("is false when the browser shows no focus, as during and after blur", () => {
    expect(isKeyboardFocused(trigger(false))).toBe(false);
  });
});
