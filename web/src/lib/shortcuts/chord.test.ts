import { describe, expect, it } from "vitest";

import { type Chord, chordMatches, type KeyChordInput } from "./chord";

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
