// The app chords a focused embedded page hands to the renderer: only the
// renderer's well-formed list, matched with exact modifiers.
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  normalizeForwardedChords,
  matchesForwardedChord,
  forwardedKeydown,
  MAX_FORWARDED_CHORDS,
} = require("../src/browserForwardedChords");

const COMMAND_K = { key: "k", metaKey: true, ctrlKey: false, altKey: false, shiftKey: false };
const BACK = { key: "ArrowLeft", metaKey: true, ctrlKey: false, altKey: true, shiftKey: true };
const BRACKET = {
  code: "BracketLeft",
  metaKey: true,
  ctrlKey: true,
  altKey: false,
  shiftKey: false,
};

describe("normalizeForwardedChords", () => {
  it("keeps well-formed chords and drops the rest", () => {
    assert.deepEqual(
      normalizeForwardedChords([
        COMMAND_K,
        { ...BRACKET, key: "[" },
        { key: "a", metaKey: false, ctrlKey: false, altKey: false, shiftKey: true },
        { key: "e", metaKey: false, ctrlKey: false, altKey: true, shiftKey: true },
        { key: "", metaKey: true, ctrlKey: false, altKey: false, shiftKey: false },
        { key: "k", metaKey: "yes", ctrlKey: false, altKey: false, shiftKey: false },
        { metaKey: true, ctrlKey: false, altKey: false, shiftKey: false },
        null,
        "k",
      ]),
      [COMMAND_K, BRACKET],
    );
  });

  it("treats anything but a list as none, and caps the list", () => {
    assert.deepEqual(normalizeForwardedChords(undefined), []);
    assert.deepEqual(normalizeForwardedChords({ 0: COMMAND_K }), []);
    const many = Array.from({ length: MAX_FORWARDED_CHORDS + 5 }, () => COMMAND_K);
    assert.equal(normalizeForwardedChords(many).length, MAX_FORWARDED_CHORDS);
  });
});

describe("matchesForwardedChord", () => {
  const chords = normalizeForwardedChords([COMMAND_K, BACK, BRACKET]);
  const keyDown = (fields) => ({ type: "keyDown", ...fields });

  it("matches a key case-insensitively and a code exactly, with exactly the chord's modifiers", () => {
    assert.equal(matchesForwardedChord(chords, keyDown({ key: "K", meta: true })), true);
    // Kelvin sign, which a full case fold would turn into "k".
    assert.equal(matchesForwardedChord(chords, keyDown({ key: "\u212a", meta: true })), false);
    assert.equal(
      matchesForwardedChord(
        chords,
        keyDown({ key: "ArrowLeft", meta: true, alt: true, shift: true }),
      ),
      true,
    );
    assert.equal(
      matchesForwardedChord(
        chords,
        keyDown({ key: "“", code: "BracketLeft", meta: true, control: true }),
      ),
      true,
    );
  });

  it("leaves other keys, extra modifiers and key releases to the page", () => {
    assert.equal(matchesForwardedChord(chords, keyDown({ key: "k" })), false);
    assert.equal(
      matchesForwardedChord(chords, keyDown({ key: "k", meta: true, shift: true })),
      false,
    );
    assert.equal(
      matchesForwardedChord(chords, keyDown({ key: "ArrowLeft", meta: true, alt: true })),
      false,
    );
    assert.equal(matchesForwardedChord(chords, { type: "keyUp", key: "k", meta: true }), false);
    assert.equal(matchesForwardedChord([], keyDown({ key: "k", meta: true })), false);
  });
});

describe("keys that write text stay with the page", () => {
  const NEW_TAB = { code: "KeyN", metaKey: false, ctrlKey: true, altKey: true, shiftKey: true };
  const CTRL_K = { key: "k", metaKey: false, ctrlKey: true, altKey: false, shiftKey: false };
  const CTRL_KEY_K = {
    code: "KeyK",
    metaKey: false,
    ctrlKey: true,
    altKey: false,
    shiftKey: false,
  };
  const MAC_NEW_TAB = { code: "KeyN", metaKey: true, ctrlKey: false, altKey: true, shiftKey: true };
  const chords = normalizeForwardedChords([COMMAND_K, CTRL_K, CTRL_KEY_K, NEW_TAB, MAC_NEW_TAB]);
  const keyDown = (fields) => ({ type: "keyDown", ...fields });

  it("never forwards a composing or dead key, even a listed chord", () => {
    for (const platform of ["darwin", "win32", "linux"]) {
      for (const fields of [
        { key: "k", meta: true, isComposing: true },
        { key: "Process", code: "KeyK", control: true },
        { key: "Dead", code: "KeyN", meta: true, alt: true, shift: true },
      ]) {
        assert.equal(matchesForwardedChord(chords, keyDown(fields), platform), false);
      }
    }
  });

  for (const platform of ["win32", "linux"]) {
    it(`leaves Ctrl+Alt, which is AltGr, to the page on ${platform}`, () => {
      const altGrShiftN = keyDown({
        key: "N",
        code: "KeyN",
        control: true,
        alt: true,
        shift: true,
      });
      assert.equal(matchesForwardedChord(chords, altGrShiftN, platform), false);
      const reportedAltGr = keyDown({
        key: "k",
        control: true,
        modifiers: ["control", "altgraph"],
      });
      assert.equal(matchesForwardedChord(chords, reportedAltGr, platform), false);
      assert.equal(
        matchesForwardedChord(chords, keyDown({ key: "k", control: true }), platform),
        true,
      );
    });
  }

  it("forwards ⌥⌘ chords on the Mac, but never ⌥ without ⌘ or Ctrl", () => {
    const optionCommandShiftN = keyDown({
      key: "N",
      code: "KeyN",
      meta: true,
      alt: true,
      shift: true,
    });
    assert.equal(matchesForwardedChord(chords, optionCommandShiftN, "darwin"), true);
    const optionShiftE = { key: "e", metaKey: false, ctrlKey: false, altKey: true, shiftKey: true };
    assert.deepEqual(normalizeForwardedChords([optionShiftE]), []);
  });
});

describe("forwardedKeydown", () => {
  it("carries the keydown fields the renderer replays", () => {
    assert.deepEqual(forwardedKeydown({ key: "k", code: "KeyK", meta: true, isAutoRepeat: true }), {
      type: "keydown",
      key: "k",
      code: "KeyK",
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      metaKey: true,
      repeat: true,
    });
  });
});
