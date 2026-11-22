// App chords a focused page hands to the shell renderer, which can't hear the page's keys.
// Text input (composing, dead keys, and Ctrl+Alt/AltGr off the Mac) always stays with the page.
"use strict";

/** More than any renderer needs; bounds what a renderer can make main match per key. */
const MAX_FORWARDED_CHORDS = 64;
const MAX_KEY_LENGTH = 32;

// Fold ASCII case only, so ⌘K's "k" never matches a character such as the Kelvin sign.
const asciiLower = (value) => value.replace(/[A-Z]/g, (letter) => letter.toLowerCase());

function isKeyName(value) {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_KEY_LENGTH;
}

/**
 * Keep the well-formed chords: physical modifiers plus a `code` or a `key`.
 * Anything else is dropped, so a malformed list forwards nothing extra.
 *
 * @param {unknown} raw
 * @returns {{ key?: string, code?: string, metaKey: boolean, ctrlKey: boolean, altKey: boolean, shiftKey: boolean }[]}
 */
function normalizeForwardedChords(raw) {
  if (!Array.isArray(raw)) return [];
  const chords = [];
  for (const candidate of raw.slice(0, MAX_FORWARDED_CHORDS)) {
    if (!candidate || typeof candidate !== "object") continue;
    const code = isKeyName(candidate.code) ? candidate.code : undefined;
    const key = isKeyName(candidate.key) ? candidate.key : undefined;
    if (!code && !key) continue;
    const modifiers = ["metaKey", "ctrlKey", "altKey", "shiftKey"];
    if (!modifiers.every((name) => typeof candidate[name] === "boolean")) continue;
    // A bare key, or ⌥ alone, would take ordinary typing away from the page.
    if (!candidate.metaKey && !candidate.ctrlKey) continue;
    chords.push({
      ...(code ? { code } : { key }),
      metaKey: candidate.metaKey,
      ctrlKey: candidate.ctrlKey,
      altKey: candidate.altKey,
      shiftKey: candidate.shiftKey,
    });
  }
  return chords;
}

/** Keys that write text, which a page keeps whatever the list says. */
function isTextInput(input, platform) {
  if (input.isComposing || input.key === "Process" || input.key === "Dead") return true;
  if (platform === "darwin") return false;
  const modifiers = Array.isArray(input.modifiers) ? input.modifiers : [];
  return (!!input.control && !!input.alt) || modifiers.includes("altgraph");
}

/**
 * Whether a page keydown is one of `chords`, with exactly its modifiers.
 *
 * @param {ReturnType<typeof normalizeForwardedChords>} chords
 * @param {Electron.Input} input
 * @param {NodeJS.Platform} [platform]
 */
function matchesForwardedChord(chords, input, platform = process.platform) {
  if (!input || input.type !== "keyDown" || isTextInput(input, platform)) return false;
  return chords.some(
    (chord) =>
      chord.metaKey === !!input.meta &&
      chord.ctrlKey === !!input.control &&
      chord.altKey === !!input.alt &&
      chord.shiftKey === !!input.shift &&
      (chord.code !== undefined
        ? input.code === chord.code
        : typeof input.key === "string" && asciiLower(input.key) === asciiLower(chord.key)),
  );
}

/** The forwarded keydown, in the shape the renderer replays as a window KeyboardEvent. */
function forwardedKeydown(input) {
  return {
    type: "keydown",
    key: input.key || "",
    code: input.code || input.key || "",
    ctrlKey: !!input.control,
    shiftKey: !!input.shift,
    altKey: !!input.alt,
    metaKey: !!input.meta,
    repeat: !!input.isAutoRepeat,
  };
}

module.exports = {
  normalizeForwardedChords,
  matchesForwardedChord,
  forwardedKeydown,
  MAX_FORWARDED_CHORDS,
};
