// The keyboard shortcuts' chords, one entry per action. Hotkey hooks match
// through it, so a chord is written down once instead of in each hook.

import { isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell, supportsDesktopCommands } from "@/lib/nativeBridge";
import { type Chord, chordMatches, type KeyChordInput } from "@/lib/shortcuts/chord";

/** What a binding's chords depend on. */
export interface ShortcutEnvironment {
  mac: boolean;
  /** A native shell, where chords a browser keeps for itself (⌘N) reach the page. */
  native: boolean;
  /** The desktop shell's menus own some chords (⌘, for Settings) and hand the command to the page. */
  desktopMenu: boolean;
}

/** Environments built here, which read each field once, on first use. */
const resolvedEnvironments = new WeakSet<object>();

/**
 * The environment now, with `overrides` taking precedence. Each field is read
 * only when a chord needs it, so a keystroke doesn't probe the shell it doesn't use.
 */
export function currentShortcutEnvironment(
  overrides: Partial<ShortcutEnvironment> = {},
): ShortcutEnvironment {
  if (resolvedEnvironments.has(overrides)) return overrides as ShortcutEnvironment;
  const cache: Partial<ShortcutEnvironment> = { ...overrides };
  const read = <Key extends keyof ShortcutEnvironment>(
    key: Key,
    detect: () => ShortcutEnvironment[Key],
  ): ShortcutEnvironment[Key] => (cache[key] ??= detect()) as ShortcutEnvironment[Key];
  const environment: ShortcutEnvironment = {
    get mac() {
      return read("mac", isMacPlatform);
    },
    get native() {
      return read("native", isNativeShell);
    },
    get desktopMenu() {
      return read("desktopMenu", supportsDesktopCommands);
    },
  };
  resolvedEnvironments.add(environment);
  return environment;
}

interface ShortcutBindingDefinition {
  label: string;
  /** The chords the page matches here; empty where something else owns the action's chord. */
  chords: (environment: ShortcutEnvironment) => Chord[];
}

const BINDINGS = {
  "session.new": {
    label: "Start a new session",
    // A browser keeps ⌘N for a new window. Match the key that types N or O, on any layout.
    chords: (environment) =>
      environment.native ? [{ mod: true, key: "n" }] : [{ mod: true, shift: true, key: "o" }],
  },
  "session.rename": {
    label: "Rename current session",
    // The physical key, because Option changes the character it types.
    chords: () => [{ mod: true, alt: true, code: "KeyR" }],
  },
  "settings.open": {
    label: "Open Settings",
    // The desktop menu's Settings item owns ⌘, there; a browser keeps ⌘, for its own settings.
    chords: (environment) =>
      environment.desktopMenu ? [] : [{ mod: true, alt: true, code: "Comma" }],
  },
} satisfies Record<string, ShortcutBindingDefinition>;

export type ShortcutId = keyof typeof BINDINGS;

export interface ShortcutBinding extends ShortcutBindingDefinition {
  id: ShortcutId;
}

/** Every binding, in registry order. */
export const SHORTCUT_BINDINGS: readonly ShortcutBinding[] = Object.entries(BINDINGS).map(
  ([id, binding]) => ({ id: id as ShortcutId, ...(binding as ShortcutBindingDefinition) }),
);

const BINDINGS_BY_ID = new Map<ShortcutId, ShortcutBinding>(
  SHORTCUT_BINDINGS.map((binding) => [binding.id, binding]),
);

export function shortcutBinding(id: ShortcutId): ShortcutBinding {
  return BINDINGS_BY_ID.get(id)!;
}

/** The chords that run `id` now, for this environment. */
export function activeShortcutChords(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): Chord[] {
  return shortcutBinding(id).chords(currentShortcutEnvironment(environment));
}

/** True when `input` presses one of `id`'s active chords. */
export function matchesShortcut(
  input: KeyChordInput | globalThis.KeyboardEvent,
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): boolean {
  const resolved = currentShortcutEnvironment(environment);
  return activeShortcutChords(id, resolved).some((chord) =>
    chordMatches(chord, input, resolved.mac),
  );
}
