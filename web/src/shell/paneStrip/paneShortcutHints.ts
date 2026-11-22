// The shortcut a pane's strip and menus show beside a command: the chord the shortcut registry
// has for it here, or none while the command has no chord.

import { ARIA_MOD_KEY } from "@/components/KeyboardShortcut";
import { chordKeyLabel } from "@/lib/shortcuts/chord";
import { activeShortcutChords, shortcutKeys, type ShortcutId } from "@/lib/shortcuts/registry";

// Pane commands, by their pane command table id, that have a registry chord.
const REGISTERED: Readonly<Record<string, ShortcutId>> = Object.freeze({
  "terminal.splitRight": "terminal.splitRight",
  "terminal.splitDown": "terminal.splitDown",
});

/** The key caps a hint shows for pane command `command`, or undefined without a chord. */
export function paneShortcutKeys(command: string | undefined): string[] | undefined {
  const id = command === undefined ? undefined : REGISTERED[command];
  if (id === undefined) return undefined;
  const keys = shortcutKeys(id);
  return keys.length > 0 ? keys : undefined;
}

/** The `aria-keyshortcuts` value for pane command `command`, or undefined without a chord. */
export function paneShortcutAria(command: string | undefined): string | undefined {
  const id = command === undefined ? undefined : REGISTERED[command];
  const chord = id === undefined ? undefined : activeShortcutChords(id)[0];
  if (!chord) return undefined;
  return [
    ...(chord.mod ? [ARIA_MOD_KEY] : []),
    ...(chord.ctrl && !(chord.mod && ARIA_MOD_KEY === "Control") ? ["Control"] : []),
    ...(chord.alt ? ["Alt"] : []),
    ...(chord.shift ? ["Shift"] : []),
    chordKeyLabel(chord),
  ].join("+");
}
