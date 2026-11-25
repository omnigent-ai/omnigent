// The workspace sidebar's own keyboard shortcuts: its tabs and history, the code editor's
// commands, find, searches, and opening a row in a new tab. They extend the app's registry without joining it,
// so the Keyboard Shortcuts page and ⌘K list them only once the sidebar is routed.

import { isMacPlatform } from "@/lib/hotkeys";
import {
  type Chord,
  chordKeyLabel,
  chordKeys,
  chordMatches,
  type KeyChordInput,
} from "@/lib/shortcuts/chord";
import {
  activeShortcutChords as registryShortcutChords,
  currentShortcutEnvironment,
  shortcutBinding as registryShortcutBinding,
  type ShortcutEnvironment,
  type ShortcutId as RegistryShortcutId,
} from "@/lib/shortcuts/registry";

interface SidebarShortcutDefinition {
  label: string;
  /** The chords the page matches here; empty for a command reached only from ⌘K. */
  chords: (environment: ShortcutEnvironment) => Chord[];
}

const mod = (key: Chord): Chord => ({ mod: true, ...key });
const modAlt = (key: Chord): Chord => ({ mod: true, alt: true, ...key });

const SIDEBAR_BINDINGS = {
  // A browser keeps ⌘T and ⌘W for its own tabs, so the sidebar's tabs add Option.
  "workspace.newTab": {
    label: "New workspace tab",
    chords: () => [modAlt({ shift: true, code: "KeyN" })],
  },
  "workspace.closeTab": {
    label: "Close workspace tab",
    chords: () => [modAlt({ code: "KeyW" })],
  },
  "workspace.reopenTab": {
    label: "Reopen closed workspace tab",
    chords: () => [modAlt({ shift: true, code: "KeyT" })],
  },
  // ⌘← / ⌘→ in the macOS app, as in the browsers it follows; text keeps them for line start / end.
  "workspace.back": {
    label: "Go back in the workspace",
    chords: (environment) => [
      environment.mac && environment.native
        ? mod({ key: "ArrowLeft" })
        : modAlt({ shift: true, key: "ArrowLeft" }),
    ],
  },
  "workspace.forward": {
    label: "Go forward in the workspace",
    chords: (environment) => [
      environment.mac && environment.native
        ? mod({ key: "ArrowRight" })
        : modAlt({ shift: true, key: "ArrowRight" }),
    ],
  },
  // A browser keeps ⇧⌘[ and ⇧⌘] for its own tabs.
  "workspace.nextTab": {
    label: "Next workspace tab",
    chords: (environment) =>
      environment.native ? [mod({ shift: true, code: "BracketRight" })] : [],
  },
  "workspace.previousTab": {
    label: "Previous workspace tab",
    chords: (environment) =>
      environment.native ? [mod({ shift: true, code: "BracketLeft" })] : [],
  },
  "workspace.moveTabLeft": {
    label: "Move workspace tab left",
    chords: () => [{ alt: true, shift: true, key: "ArrowLeft" }],
  },
  "workspace.moveTabRight": {
    label: "Move workspace tab right",
    chords: () => [{ alt: true, shift: true, key: "ArrowRight" }],
  },
  "workspace.toggleFullScreen": {
    label: "Toggle workspace full screen",
    chords: () => [mod({ shift: true, code: "KeyF" })],
  },
  "browser.new": {
    label: "Open a new browser tab",
    chords: () => [modAlt({ code: "KeyB" })],
  },
  // Physical keys, since Option rewrites letters; ⌃⌘F is the system's full screen on macOS.
  "palette.text": {
    label: "Search text in the open session's files",
    chords: (environment) =>
      environment.mac ? [{ ctrl: true, mod: true, code: "KeyO" }] : [modAlt({ code: "KeyF" })],
  },
  "palette.files": {
    label: "Go to a file in the open session",
    chords: (environment) =>
      environment.mac ? [{ ctrl: true, mod: true, code: "KeyP" }] : [modAlt({ code: "KeyP" })],
  },
  // The Explorer chord editors use.
  "files.toggleTree": {
    label: "Show or hide the Files tree",
    chords: () => [mod({ shift: true, code: "KeyE" })],
  },
  "find.open": {
    label: "Find in file",
    chords: () => [mod({ key: "f" })],
  },
  "file.findAndReplace": {
    label: "Find and replace",
    // Windows and Linux editors also replace with Control+H.
    chords: (environment) => [
      modAlt({ shift: true, code: "KeyF" }),
      ...(environment.mac ? [] : [{ ctrl: true, code: "KeyH" }]),
    ],
  },
  "file.goToLine": {
    label: "Go to line",
    // Control on every platform, as in other editors.
    chords: () => [{ ctrl: true, code: "KeyG" }],
  },
  "file.toggleLineComment": {
    label: "Toggle line comment",
    chords: () => [mod({ key: "/" })],
  },
  "file.toggleBlockComment": {
    label: "Toggle block comment",
    chords: () => [{ alt: true, shift: true, code: "KeyA" }],
  },
  "file.indentLines": { label: "Indent lines", chords: () => [mod({ code: "BracketRight" })] },
  "file.outdentLines": { label: "Outdent lines", chords: () => [mod({ code: "BracketLeft" })] },
  "file.moveLinesUp": { label: "Move lines up", chords: () => [{ alt: true, key: "ArrowUp" }] },
  "file.moveLinesDown": {
    label: "Move lines down",
    chords: () => [{ alt: true, key: "ArrowDown" }],
  },
  "file.copyLinesUp": {
    label: "Copy lines up",
    chords: () => [{ alt: true, shift: true, key: "ArrowUp" }],
  },
  "file.copyLinesDown": {
    label: "Copy lines down",
    chords: () => [{ alt: true, shift: true, key: "ArrowDown" }],
  },
  "file.deleteLines": { label: "Delete lines", chords: () => [mod({ shift: true, code: "KeyK" })] },
  "file.insertLineBelow": { label: "Insert line below", chords: () => [mod({ key: "Enter" })] },
  "file.insertLineAbove": {
    label: "Insert line above",
    chords: () => [mod({ shift: true, key: "Enter" })],
  },
  "file.trimTrailingWhitespace": { label: "Trim trailing whitespace", chords: () => [] },
  "file.undoCursor": { label: "Undo cursor change", chords: () => [mod({ code: "KeyU" })] },
  "file.addCursorAbove": { label: "Add cursor above", chords: () => [modAlt({ key: "ArrowUp" })] },
  "file.addCursorBelow": {
    label: "Add cursor below",
    chords: () => [modAlt({ key: "ArrowDown" })],
  },
  "file.selectNextOccurrence": {
    label: "Select next occurrence",
    chords: () => [mod({ key: "d" })],
  },
  "file.selectAllOccurrences": {
    label: "Select all occurrences",
    chords: () => [mod({ shift: true, code: "KeyL" })],
  },
  "file.expandLineSelection": {
    label: "Expand selection to line",
    chords: () => [mod({ code: "KeyL" })],
  },
  "file.goToBracket": {
    label: "Go to matching bracket",
    chords: () => [mod({ shift: true, code: "Backslash" })],
  },
  // ⌘⌥[ and ⌘⌥] toggle the sidebars, so folding takes Control+Shift and a bracket.
  "file.fold": { label: "Fold", chords: () => [{ ctrl: true, shift: true, code: "BracketLeft" }] },
  "file.unfold": {
    label: "Unfold",
    chords: () => [{ ctrl: true, shift: true, code: "BracketRight" }],
  },
  "file.foldAll": { label: "Fold all", chords: () => [] },
  "file.unfoldAll": { label: "Unfold all", chords: () => [] },
  // A row that opens a place (a changed file, a commit or a pull request) opens it in a new tab.
  "rows.openInNewTab": {
    label: "Open the focused row in a new tab (⇧ to switch to it)",
    chords: () => [mod({ key: "Enter" }), mod({ shift: true, key: "Enter" })],
  },
} satisfies Record<string, SidebarShortcutDefinition>;

export type WorkspaceSidebarShortcutId = keyof typeof SIDEBAR_BINDINGS;

/** A shortcut the sidebar matches: one of the app's, or one of its own. */
export type ShortcutId = RegistryShortcutId | WorkspaceSidebarShortcutId;

export interface ShortcutBinding extends SidebarShortcutDefinition {
  id: ShortcutId;
}

/** The sidebar's own bindings, in order, for the Keyboard Shortcuts page once it's routed. */
export const WORKSPACE_SIDEBAR_BINDINGS: readonly ShortcutBinding[] = Object.entries(
  SIDEBAR_BINDINGS,
).map(([id, binding]) => ({
  id: id as WorkspaceSidebarShortcutId,
  ...(binding as SidebarShortcutDefinition),
}));

function isSidebarShortcut(id: ShortcutId): id is WorkspaceSidebarShortcutId {
  return Object.hasOwn(SIDEBAR_BINDINGS, id);
}

export function shortcutBinding(id: ShortcutId): ShortcutBinding {
  if (!isSidebarShortcut(id)) return registryShortcutBinding(id);
  return { id, ...(SIDEBAR_BINDINGS[id] as SidebarShortcutDefinition) };
}

/** The chords that run `id` now, for this environment. */
export function activeShortcutChords(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): Chord[] {
  if (!isSidebarShortcut(id)) return registryShortcutChords(id, environment);
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

/** Key caps for the first of `id`'s chords here, or none when it has no chord. */
export function shortcutKeys(id: ShortcutId, environment?: Partial<ShortcutEnvironment>): string[] {
  const resolved = currentShortcutEnvironment(environment);
  const chord = activeShortcutChords(id, resolved)[0];
  return chord ? chordKeys(chord, resolved.mac) : [];
}

const CODE_MIRROR_KEY_NAMES: Record<string, string> = {
  Escape: "Escape",
  Enter: "Enter",
  Tab: "Tab",
};

/** The chord as a CodeMirror key name, such as `Mod-Alt-f`, for an editor keymap. */
export function codeMirrorKey(chord: Chord): string {
  const parts: string[] = [];
  if (chord.mod) parts.push("Mod");
  if (chord.ctrl) parts.push("Ctrl");
  if (chord.alt) parts.push("Alt");
  if (chord.shift) parts.push("Shift");
  const key = chord.code !== undefined ? chordKeyLabel(chord).toLowerCase() : (chord.key ?? "");
  parts.push(CODE_MIRROR_KEY_NAMES[key] ?? (key.length === 1 ? key.toLowerCase() : key));
  return parts.join("-");
}

/** `id`'s active chords as CodeMirror key names, for an editor keymap. */
export function codeMirrorKeys(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string[] {
  return activeShortcutChords(id, environment).map(codeMirrorKey);
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
  const parts: string[] = [];
  if (mac && chord.mod) parts.push("Meta");
  if (chord.ctrl || (!mac && chord.mod)) parts.push("Control");
  if (chord.alt) parts.push("Alt");
  if (chord.shift) parts.push("Shift");
  const label = chordKeyLabel(chord);
  parts.push(ARIA_KEY_NAMES[label] ?? label);
  return parts.join("+");
}

/** `aria-keyshortcuts` for the first of `id`'s chords here, or none when it has no chord. */
export function shortcutAriaKeys(
  id: ShortcutId,
  environment?: Partial<ShortcutEnvironment>,
): string | undefined {
  const resolved = currentShortcutEnvironment(environment);
  const chord = activeShortcutChords(id, resolved)[0];
  return chord ? chordAriaShortcut(chord, resolved.mac) : undefined;
}
