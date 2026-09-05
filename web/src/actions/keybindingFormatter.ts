import type { KeyModifier, KeySequence, KeyStroke } from "./types";

export interface KeybindingFormatOptions {
  isMac: boolean;
}

const MAC_MODIFIERS: Readonly<Record<KeyModifier, string>> = {
  mod: "⌘",
  primary: "⌘",
  ctrl: "⌃",
  meta: "⌘",
  alt: "⌥",
  shift: "⇧",
};

const OTHER_MODIFIERS: Readonly<Record<KeyModifier, string>> = {
  mod: "Ctrl",
  primary: "Ctrl",
  ctrl: "Ctrl",
  meta: "Meta",
  alt: "Alt",
  shift: "Shift",
};

const KEY_LABELS: Readonly<Record<string, string>> = {
  " ": "Space",
  Enter: "↵",
  Escape: "Esc",
  Backspace: "⌫",
  Delete: "⌦",
  Tab: "⇥",
  ArrowUp: "↑",
  ArrowDown: "↓",
  ArrowLeft: "←",
  ArrowRight: "→",
  PageUp: "Page Up",
  PageDown: "Page Down",
};

const CODE_LABELS: Readonly<Record<string, string>> = {
  BracketLeft: "[",
  BracketRight: "]",
  Equal: "=",
  Minus: "-",
  Slash: "/",
  Backslash: "\\",
  Semicolon: ";",
  Quote: "'",
  Comma: ",",
  Period: ".",
  Backquote: "`",
};

function keyLabel(stroke: KeyStroke): string {
  const value = stroke.key.value;
  if (stroke.key.kind === "code") {
    if (CODE_LABELS[value]) return CODE_LABELS[value];
    if (value.startsWith("Key") && value.length === 4) return value.slice(3).toUpperCase();
    if (value.startsWith("Digit") && value.length === 6) return value.slice(5);
    return value;
  }
  return KEY_LABELS[value] ?? ([...value].length === 1 ? value.toLocaleUpperCase() : value);
}

export function formatKeyStroke(stroke: KeyStroke, { isMac }: KeybindingFormatOptions): string {
  const labels = stroke.modifiers.map((modifier) =>
    isMac ? MAC_MODIFIERS[modifier] : OTHER_MODIFIERS[modifier],
  );
  labels.push(keyLabel(stroke));
  return isMac ? labels.join("") : labels.join("+");
}

export function formatKeybinding(sequence: KeySequence, options: KeybindingFormatOptions): string {
  return sequence.map((stroke) => formatKeyStroke(stroke, options)).join(" ");
}

export function keybindingParts(
  sequence: KeySequence,
  options: KeybindingFormatOptions,
): string[][] {
  return sequence.map((stroke) => {
    const modifierMap = options.isMac ? MAC_MODIFIERS : OTHER_MODIFIERS;
    return [...stroke.modifiers.map((modifier) => modifierMap[modifier]), keyLabel(stroke)];
  });
}
