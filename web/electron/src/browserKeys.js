"use strict";

const MODIFIER_ALIASES = new Map([
  ["alt", "Alt"],
  ["option", "Alt"],
  ["control", "Control"],
  ["ctrl", "Control"],
  ["meta", "Meta"],
  ["cmd", "Meta"],
  ["command", "Meta"],
  ["shift", "Shift"],
]);
const KEY_ALIASES = new Map([
  ["esc", "Escape"],
  ["return", "Enter"],
  ["space", " "],
  ["del", "Delete"],
]);
const NAMED_KEYS = new Map(
  [
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "Insert",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    ...Array.from({ length: 12 }, (_, index) => `F${index + 1}`),
  ].map((key) => [key.toLowerCase(), key]),
);
const PUNCTUATION_KEYS = new Map([
  ["`", "Backquote"],
  ["-", "Minus"],
  ["=", "Equal"],
  ["[", "BracketLeft"],
  ["]", "BracketRight"],
  ["\\", "Backslash"],
  [";", "Semicolon"],
  ["'", "Quote"],
  [",", "Comma"],
  [".", "Period"],
  ["/", "Slash"],
]);

/**
 * Parse a shortcut for Puppeteer's keyboard API, which owns key layout and Shift
 * mapping. A single Unicode character can instead be sent with insertText.
 *
 * @param {unknown} combo
 * @param {string} [platform] `process.platform`, for `ControlOrMeta`.
 * @returns {{ modifiers: string[], key: string }}
 */
function parseKeyCombo(combo, platform = process.platform) {
  if (typeof combo !== "string" || !combo || combo.length > 64) {
    throw new Error("key must be a non-empty string of at most 64 characters");
  }
  // The final part is the key; a literal plus is spelled `+` or `Shift++`.
  const plusKey = combo === "+" || combo.endsWith("++");
  const split = plusKey ? combo.length - 2 : combo.lastIndexOf("+");
  const keyName = plusKey ? "+" : combo.slice(split + 1);
  const modifierText = split < 0 ? "" : combo.slice(0, split);
  if (!keyName) throw new Error(`key "${combo}" is missing the key after its modifiers`);
  const modifiers = [];
  for (const part of modifierText ? modifierText.split("+") : []) {
    const lower = part.trim().toLowerCase();
    const modifier =
      lower === "controlormeta"
        ? platform === "darwin"
          ? "Meta"
          : "Control"
        : MODIFIER_ALIASES.get(lower);
    if (!modifier) throw new Error(`unknown modifier "${part}" in key "${combo}"`);
    if (!modifiers.includes(modifier)) modifiers.push(modifier);
  }
  const lowerKey = keyName.toLowerCase();
  const key = NAMED_KEYS.get(lowerKey) ?? KEY_ALIASES.get(lowerKey) ?? keyName;
  if (!NAMED_KEYS.has(lowerKey) && !KEY_ALIASES.has(lowerKey) && [...keyName].length !== 1) {
    throw new Error(`unknown key "${keyName}"; use a key name like Enter or a single character`);
  }
  if (modifiers.length && [...key].length === 1 && !/^[\x20-\x7e]$/.test(key)) {
    throw new Error("Unicode characters cannot be used in shortcuts");
  }
  return { modifiers, key };
}

/** Use physical names for Shift so Puppeteer applies its own keyboard layout. */
function keyboardKey({ modifiers, key }) {
  if (!modifiers.includes("Shift")) return key;
  if (/^[a-z]$/i.test(key)) return `Key${key.toUpperCase()}`;
  if (/^[0-9]$/.test(key)) return `Digit${key}`;
  return PUNCTUATION_KEYS.get(key) ?? key;
}

module.exports = { parseKeyCombo, keyboardKey };
