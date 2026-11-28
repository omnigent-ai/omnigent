// Keyboard chords: how a shortcut is written down and matched against a keydown
// on each platform, so hotkeys stop re-deriving modifier rules by hand.

import { isMacPlatform } from "@/lib/hotkeys";

/**
 * A chord. `mod` is the platform command modifier (⌘ on macOS, Ctrl elsewhere);
 * `ctrl` is literal Control on every platform, for chords that must not become ⌘.
 */
export interface Chord {
  mod?: boolean;
  ctrl?: boolean;
  alt?: boolean;
  shift?: boolean;
  /** Matched against `KeyboardEvent.code`, for keys whose character Alt or Shift rewrites. */
  code?: string;
  /** Matched against `KeyboardEvent.key`, ignoring case, for keys that follow the layout. */
  key?: string;
  /** Compare `key` exactly, case included, for chords that always compared it exactly. */
  exactKey?: boolean;
  /** Also fires while AltGr is reported, as the chords that never checked for it always have. */
  matchesAltGraph?: boolean;
  /** With `mod`: ⌘, Control, or both satisfy it on every platform, as on a list's Delete chord. */
  eitherCommandKey?: boolean;
}

/** The fields a chord is matched against. */
export interface KeyChordInput {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  /** AltGr, which reports as Ctrl+Alt on some layouts; only a chord that opts in matches it. */
  altGraph?: boolean;
}

/** The matchable fields of a DOM keydown. */
export function keyChordInput(event: globalThis.KeyboardEvent): KeyChordInput {
  return {
    key: event.key,
    code: event.code,
    metaKey: event.metaKey,
    ctrlKey: event.ctrlKey,
    altKey: event.altKey,
    shiftKey: event.shiftKey,
    altGraph: typeof event.getModifierState === "function" && event.getModifierState("AltGraph"),
  };
}

/** The modifier keys a chord presses on a platform: `mod` is Meta on macOS, Control elsewhere. */
function physicalModifiers(chord: Chord, mac: boolean) {
  return {
    meta: mac && !!chord.mod,
    ctrl: !!chord.ctrl || (!mac && !!chord.mod),
    alt: !!chord.alt,
    shift: !!chord.shift,
  };
}

/** True when `input` presses exactly `chord`'s modifiers and key on this platform. */
export function chordMatches(
  chord: Chord,
  input: KeyChordInput | globalThis.KeyboardEvent,
  mac: boolean = isMacPlatform(),
): boolean {
  const keys = "getModifierState" in input ? keyChordInput(input) : input;
  if (keys.altGraph && !chord.matchesAltGraph) return false;
  if (!modifiersMatch(chord, keys, mac)) return false;
  if (chord.code !== undefined) return keys.code === chord.code;
  if (chord.key === undefined) return false;
  return chord.exactKey
    ? keys.key === chord.key
    : keys.key.toLowerCase() === chord.key.toLowerCase();
}

function modifiersMatch(chord: Chord, keys: KeyChordInput, mac: boolean): boolean {
  const wanted = physicalModifiers(chord, mac);
  if (keys.altKey !== wanted.alt || keys.shiftKey !== wanted.shift) return false;
  if (chord.mod && chord.eitherCommandKey) return keys.metaKey || keys.ctrlKey;
  return keys.metaKey === wanted.meta && keys.ctrlKey === wanted.ctrl;
}

/** Modifier key caps: symbols on macOS, words elsewhere; Shift is ⇧ everywhere. */
export function modifierGlyphs(mac: boolean = isMacPlatform()) {
  return {
    mod: mac ? "⌘" : "Ctrl",
    ctrl: mac ? "⌃" : "Ctrl",
    alt: mac ? "⌥" : "Alt",
    shift: "⇧",
  } as const;
}

const CODE_LABELS: Record<string, string> = {
  Backquote: "`",
  BracketLeft: "[",
  BracketRight: "]",
  Backslash: "\\",
  Comma: ",",
  Minus: "-",
  Slash: "/",
};

const KEY_LABELS: Record<string, string> = {
  enter: "↵",
  backspace: "⌫",
  escape: "Esc",
  arrowup: "↑",
  arrowdown: "↓",
  arrowleft: "←",
  arrowright: "→",
  tab: "Tab",
  " ": "Space",
};

/** The key cap for a chord's key, without modifiers. */
export function chordKeyLabel(chord: Chord): string {
  if (chord.code !== undefined) {
    if (Object.hasOwn(CODE_LABELS, chord.code)) return CODE_LABELS[chord.code]!;
    const match = /^(?:Key|Digit)(.+)$/.exec(chord.code);
    return match ? match[1]! : chord.code;
  }
  const key = chord.key ?? "";
  const lowerKey = key.toLowerCase();
  if (Object.hasOwn(KEY_LABELS, lowerKey)) return KEY_LABELS[lowerKey]!;
  return key.length === 1 ? key.toUpperCase() : key;
}

/** Key caps for a chord, left to right: command modifier, Control, Alt, Shift, then the key. */
export function chordKeys(chord: Chord, mac: boolean = isMacPlatform()): string[] {
  const glyphs = modifierGlyphs(mac);
  const keys: string[] = [];
  if (chord.mod) keys.push(glyphs.mod);
  // Off macOS the command modifier already is Control.
  if (chord.ctrl && !(chord.mod && !mac)) keys.push(glyphs.ctrl);
  if (chord.alt) keys.push(glyphs.alt);
  if (chord.shift) keys.push(glyphs.shift);
  keys.push(chordKeyLabel(chord));
  return keys;
}

const ARIA_KEY_NAMES: Record<string, string> = {
  "↵": "Enter",
  "⌫": "Backspace",
  Esc: "Escape",
  "↑": "ArrowUp",
  "↓": "ArrowDown",
  "←": "ArrowLeft",
  "→": "ArrowRight",
};

/** The chord as an `aria-keyshortcuts` value, such as `Meta+Alt+R`. */
export function chordAriaShortcut(chord: Chord, mac: boolean = isMacPlatform()): string {
  const wanted = physicalModifiers(chord, mac);
  const parts: string[] = [];
  if (wanted.meta) parts.push("Meta");
  if (wanted.ctrl) parts.push("Control");
  if (wanted.alt) parts.push("Alt");
  if (wanted.shift) parts.push("Shift");
  const label = chordKeyLabel(chord);
  parts.push(ARIA_KEY_NAMES[label] ?? label);
  return parts.join("+");
}
