import { describe, expect, it } from "vitest";

import {
  type Chord,
  chordAriaShortcut,
  chordKeys,
  chordMatches,
  type KeyChordInput,
} from "./chord";

function press(init: Partial<KeyChordInput> & { key: string }): KeyChordInput {
  return { metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...init };
}

describe("chordMatches", () => {
  const rename: Chord = { mod: true, alt: true, code: "KeyR" };

  it("takes ⌘ as the command modifier on macOS and Control elsewhere", () => {
    expect(
      chordMatches(rename, press({ key: "®", code: "KeyR", metaKey: true, altKey: true }), true),
    ).toBe(true);
    expect(
      chordMatches(rename, press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true }), true),
    ).toBe(false);
    expect(
      chordMatches(rename, press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true }), false),
    ).toBe(true);
    expect(
      chordMatches(rename, press({ key: "r", code: "KeyR", metaKey: true, altKey: true }), false),
    ).toBe(false);
  });

  it("accepts ⌘, Control, or both for an either-key chord", () => {
    const remove: Chord = { mod: true, eitherCommandKey: true, key: "Backspace" };
    for (const mac of [true, false]) {
      expect(chordMatches(remove, press({ key: "Backspace", metaKey: true }), mac)).toBe(true);
      expect(chordMatches(remove, press({ key: "Backspace", ctrlKey: true }), mac)).toBe(true);
      expect(
        chordMatches(remove, press({ key: "Backspace", metaKey: true, ctrlKey: true }), mac),
      ).toBe(true);
      expect(chordMatches(remove, press({ key: "Backspace" }), mac)).toBe(false);
      expect(
        chordMatches(remove, press({ key: "Backspace", metaKey: true, shiftKey: true }), mac),
      ).toBe(false);
    }
  });

  it("rejects both command keys at once", () => {
    const both = press({ key: "r", code: "KeyR", metaKey: true, ctrlKey: true, altKey: true });
    expect(chordMatches(rename, both, true)).toBe(false);
    expect(chordMatches(rename, both, false)).toBe(false);
  });

  it("requires exactly the chord's modifiers", () => {
    expect(
      chordMatches(
        rename,
        press({ key: "R", code: "KeyR", metaKey: true, altKey: true, shiftKey: true }),
        true,
      ),
    ).toBe(false);
    expect(chordMatches(rename, press({ key: "r", code: "KeyR", metaKey: true }), true)).toBe(
      false,
    );
  });

  it("keeps literal Control on macOS", () => {
    const cycleModel: Chord = { ctrl: true, alt: true, code: "KeyM" };
    expect(
      chordMatches(
        cycleModel,
        press({ key: "µ", code: "KeyM", ctrlKey: true, altKey: true }),
        true,
      ),
    ).toBe(true);
    expect(
      chordMatches(
        cycleModel,
        press({ key: "µ", code: "KeyM", metaKey: true, altKey: true }),
        true,
      ),
    ).toBe(false);
  });

  it("matches a layout key by character, ignoring case, and a physical key by code", () => {
    const palette: Chord = { mod: true, key: "k" };
    expect(chordMatches(palette, press({ key: "K", code: "KeyK", metaKey: true }), true)).toBe(
      true,
    );
    // A layout that puts K elsewhere still opens the palette.
    expect(chordMatches(palette, press({ key: "k", code: "KeyX", metaKey: true }), true)).toBe(
      true,
    );
    expect(
      chordMatches(rename, press({ key: "r", code: "KeyT", metaKey: true, altKey: true }), true),
    ).toBe(false);
  });

  it("never matches AltGr, which reports as Control+Alt", () => {
    const altGraph = press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true, altGraph: true });
    expect(chordMatches(rename, altGraph, false)).toBe(false);
  });

  it("compares the key exactly only for a chord that opts in", () => {
    const kelvin = press({ key: "\u212A", metaKey: true });
    expect(chordMatches({ mod: true, key: "k" }, kelvin, true)).toBe(true);
    expect(chordMatches({ mod: true, key: "k", exactKey: true }, kelvin, true)).toBe(false);
    expect(chordMatches({ key: "Tab", exactKey: true }, press({ key: "tab" }), true)).toBe(false);
    expect(chordMatches({ key: "Tab", exactKey: true }, press({ key: "Tab" }), true)).toBe(true);
  });

  it("matches under AltGr only for a chord that opts in", () => {
    const slash = press({ key: "/", ctrlKey: true, altGraph: true });
    expect(chordMatches({ mod: true, key: "/" }, slash, false)).toBe(false);
    expect(chordMatches({ mod: true, key: "/", matchesAltGraph: true }, slash, false)).toBe(true);
  });

  it("reads AltGr from a DOM keydown", () => {
    const event = new KeyboardEvent("keydown", {
      key: "r",
      code: "KeyR",
      ctrlKey: true,
      altKey: true,
    });
    expect(chordMatches(rename, event, false)).toBe(true);
    Object.defineProperty(event, "getModifierState", {
      value: (key: string) => key === "AltGraph",
    });
    expect(chordMatches(rename, event, false)).toBe(false);
  });
});

describe("chordKeys", () => {
  it("draws modifiers as symbols on macOS and words elsewhere, in a fixed order", () => {
    const chord: Chord = { mod: true, alt: true, shift: true, code: "BracketLeft" };
    expect(chordKeys(chord, true)).toEqual(["⌘", "⌥", "⇧", "["]);
    expect(chordKeys(chord, false)).toEqual(["Ctrl", "Alt", "⇧", "["]);
    expect(chordKeys({ ctrl: true, shift: true, code: "KeyM" }, true)).toEqual(["⌃", "⇧", "M"]);
    expect(chordKeys({ ctrl: true, code: "Backquote" }, false)).toEqual(["Ctrl", "`"]);
    expect(chordKeys({ mod: true, ctrl: true, code: "KeyM" }, false)).toEqual(["Ctrl", "M"]);
    expect(chordKeys({ mod: true, ctrl: true, code: "KeyM" }, true)).toEqual(["⌘", "⌃", "M"]);
  });

  it("labels keys by their cap", () => {
    expect(chordKeys({ mod: true, key: "k" }, true)).toEqual(["⌘", "K"]);
    expect(chordKeys({ key: "ArrowLeft" }, true)).toEqual(["←"]);
    expect(chordKeys({ key: "ArrowRight" }, true)).toEqual(["→"]);
    expect(chordKeys({ mod: true, key: " " }, true)).toEqual(["⌘", "Space"]);
    expect(chordKeys({ code: "Period" }, true)).toEqual(["Period"]);
    expect(chordKeys({ key: "constructor" }, true)).toEqual(["constructor"]);
    expect(chordKeys({ mod: true, key: "Enter" }, true)).toEqual(["⌘", "↵"]);
    expect(chordKeys({ key: "Escape" }, true)).toEqual(["Esc"]);
    expect(chordKeys({ ctrl: true, key: "Tab" }, true)).toEqual(["⌃", "Tab"]);
    expect(chordKeys({ mod: true, alt: true, code: "Backslash" }, true)).toEqual(["⌘", "⌥", "\\"]);
    expect(chordKeys({ mod: true, alt: true, code: "Minus" }, true)).toEqual(["⌘", "⌥", "-"]);
    expect(chordKeys({ mod: true, alt: true, code: "Comma" }, true)).toEqual(["⌘", "⌥", ","]);
    expect(chordKeys({ mod: true, alt: true, code: "Digit1" }, true)).toEqual(["⌘", "⌥", "1"]);
  });

  it("shows Backspace as its key cap", () => {
    expect(chordKeys({ mod: true, key: "Backspace" }, true)).toEqual(["⌘", "⌫"]);
    expect(chordKeys({ mod: true, key: "Backspace" }, false)).toEqual(["Ctrl", "⌫"]);
  });
});

describe("chordAriaShortcut", () => {
  it("names the physical modifiers for aria-keyshortcuts", () => {
    expect(chordAriaShortcut({ mod: true, alt: true, code: "KeyR" }, true)).toBe("Meta+Alt+R");
    expect(chordAriaShortcut({ mod: true, alt: true, code: "KeyR" }, false)).toBe("Control+Alt+R");
    expect(chordAriaShortcut({ mod: true, shift: true, key: "o" }, true)).toBe("Meta+Shift+O");
    expect(chordAriaShortcut({ mod: true, alt: true, code: "Backslash" }, true)).toBe(
      "Meta+Alt+\\",
    );
    expect(chordAriaShortcut({ key: "1" }, true)).toBe("1");
  });

  it("spells keys by their aria names rather than their caps", () => {
    expect(chordAriaShortcut({ mod: true, key: "Backspace" }, true)).toBe("Meta+Backspace");
    expect(chordAriaShortcut({ mod: true, key: "Backspace" }, false)).toBe("Control+Backspace");
    expect(chordAriaShortcut({ key: "Escape" }, true)).toBe("Escape");
    expect(chordAriaShortcut({ mod: true, key: "Enter" }, true)).toBe("Meta+Enter");
  });
});
