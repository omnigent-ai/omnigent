// The panes view's own keys: split, move between, resize, equalize and maximize panes. They
// extend the app's registry without joining it, so they bind only where a panes view is mounted.

import { type Chord, chordKeys, chordMatches } from "@/lib/shortcuts/chord";
import { currentShortcutEnvironment, type ShortcutEnvironment } from "@/lib/shortcuts/registry";
import { chordAriaShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

import { type PaneCommandId, paneCommandLabel } from "./paneCommandTable";

interface PaneShortcutDefinition {
  label: string;
  /** The chords the panes match here; empty where the command is only in ⌘K and menus. */
  chords: (environment: ShortcutEnvironment) => Chord[];
}

// Mac only, as in the terminals they follow: elsewhere Ctrl+D and Ctrl+arrows are the shell's.
const macOnly =
  (chord: Chord) =>
  (environment: ShortcutEnvironment): Chord[] =>
    environment.mac ? [chord] : [];
const labeled = (id: PaneCommandId, chords: PaneShortcutDefinition["chords"]) => ({
  label: paneCommandLabel(id),
  chords,
});

const PANE_BINDINGS = {
  "terminal.splitRight": labeled("terminal.splitRight", macOnly({ mod: true, code: "KeyD" })),
  "terminal.splitDown": labeled(
    "terminal.splitDown",
    macOnly({ mod: true, shift: true, code: "KeyD" }),
  ),
  "terminal.focusLeft": labeled(
    "terminal.focusLeft",
    macOnly({ mod: true, alt: true, key: "ArrowLeft" }),
  ),
  "terminal.focusRight": labeled(
    "terminal.focusRight",
    macOnly({ mod: true, alt: true, key: "ArrowRight" }),
  ),
  "terminal.focusUp": labeled(
    "terminal.focusUp",
    macOnly({ mod: true, alt: true, key: "ArrowUp" }),
  ),
  "terminal.focusDown": labeled(
    "terminal.focusDown",
    macOnly({ mod: true, alt: true, key: "ArrowDown" }),
  ),
  "terminal.resizeLeft": {
    label: "Move Divider Left",
    chords: macOnly({ mod: true, ctrl: true, key: "ArrowLeft" }),
  },
  "terminal.resizeRight": {
    label: "Move Divider Right",
    chords: macOnly({ mod: true, ctrl: true, key: "ArrowRight" }),
  },
  "terminal.resizeUp": {
    label: "Move Divider Up",
    chords: macOnly({ mod: true, ctrl: true, key: "ArrowUp" }),
  },
  "terminal.resizeDown": {
    label: "Move Divider Down",
    chords: macOnly({ mod: true, ctrl: true, key: "ArrowDown" }),
  },
  "terminal.equalize": labeled(
    "terminal.equalize",
    macOnly({ mod: true, ctrl: true, code: "Equal" }),
  ),
  "terminal.toggleZoom": labeled("terminal.toggleZoom", () => [
    { mod: true, shift: true, key: "Enter" },
  ]),
} as const satisfies Record<string, PaneShortcutDefinition>;

export type PaneShortcutId = keyof typeof PANE_BINDINGS;

export const PANE_SHORTCUT_IDS = Object.keys(PANE_BINDINGS) as PaneShortcutId[];

/** Whether `id` names a pane key, as a pane command's id may. */
export function isPaneShortcutId(id: string | undefined): id is PaneShortcutId {
  return id !== undefined && Object.hasOwn(PANE_BINDINGS, id);
}

export function paneShortcutLabel(id: PaneShortcutId): string {
  return PANE_BINDINGS[id].label;
}

/** `id`'s chords here; none where the command has no key. */
export function paneShortcutChords(
  id: PaneShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): Chord[] {
  return PANE_BINDINGS[id].chords(currentShortcutEnvironment(environment));
}

/** The pane key a keydown presses, or undefined when it presses none. */
export function matchedPaneShortcut(
  event: globalThis.KeyboardEvent,
  environment?: Partial<ShortcutEnvironment>,
): PaneShortcutId | undefined {
  const resolved = currentShortcutEnvironment(environment);
  return PANE_SHORTCUT_IDS.find((id) =>
    paneShortcutChords(id, resolved).some((chord) => chordMatches(chord, event, resolved.mac)),
  );
}

/** Key caps for `id`'s first chord here, or none. */
export function paneKeyCaps(
  id: PaneShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string[] {
  const resolved = currentShortcutEnvironment(environment);
  const chord = paneShortcutChords(id, resolved)[0];
  return chord ? chordKeys(chord, resolved.mac) : [];
}

/** `aria-keyshortcuts` for `id`'s first chord here, or undefined. */
export function paneKeyAria(
  id: PaneShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string | undefined {
  const resolved = currentShortcutEnvironment(environment);
  const chord = paneShortcutChords(id, resolved)[0];
  return chord ? chordAriaShortcut(chord, resolved.mac) : undefined;
}
