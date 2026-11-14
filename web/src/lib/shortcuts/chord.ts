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
}

/** The fields a chord is matched against. */
export interface KeyChordInput {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  /** AltGr, which reports as Ctrl+Alt on some layouts and must never match a chord. */
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
  if (keys.altGraph) return false;
  const wantMeta = mac && !!chord.mod;
  const wantCtrl = !!chord.ctrl || (!mac && !!chord.mod);
  if (keys.metaKey !== wantMeta || keys.ctrlKey !== wantCtrl) return false;
  if (keys.altKey !== !!chord.alt || keys.shiftKey !== !!chord.shift) return false;
  if (chord.code !== undefined) return keys.code === chord.code;
  return chord.key !== undefined && keys.key.toLowerCase() === chord.key.toLowerCase();
}
