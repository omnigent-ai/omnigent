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

/** True when `input` presses exactly `chord`'s modifiers and key on this platform. */
export function chordMatches(
  chord: Chord,
  input: KeyChordInput | globalThis.KeyboardEvent,
  mac: boolean = isMacPlatform(),
): boolean {
  const keys = "getModifierState" in input ? keyChordInput(input) : input;
  if (keys.altGraph && !chord.matchesAltGraph) return false;
  const wantMeta = mac && !!chord.mod;
  const wantCtrl = !!chord.ctrl || (!mac && !!chord.mod);
  if (keys.metaKey !== wantMeta || keys.ctrlKey !== wantCtrl) return false;
  if (keys.altKey !== !!chord.alt || keys.shiftKey !== !!chord.shift) return false;
  if (chord.code !== undefined) return keys.code === chord.code;
  if (chord.key === undefined) return false;
  return chord.exactKey
    ? keys.key === chord.key
    : keys.key.toLowerCase() === chord.key.toLowerCase();
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
  escape: "Esc",
  arrowup: "↑",
  arrowdown: "↓",
  tab: "Tab",
};

/** The key cap for a chord's key, without modifiers. */
export function chordKeyLabel(chord: Chord): string {
  if (chord.code !== undefined) {
    if (chord.code in CODE_LABELS) return CODE_LABELS[chord.code]!;
    const match = /^(?:Key|Digit)(.+)$/.exec(chord.code);
    return match ? match[1]! : chord.code;
  }
  const key = chord.key ?? "";
  return KEY_LABELS[key.toLowerCase()] ?? (key.length === 1 ? key.toUpperCase() : key);
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
