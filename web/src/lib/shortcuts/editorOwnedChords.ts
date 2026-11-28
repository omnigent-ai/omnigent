// The editor-owned chords rule: a focused surface that edits text keeps the keys text editing
// means there (⌘← for line start, ⌃W for delete word, ⌘B for bold in rich text), and app
// hotkeys, the desktop menu and an embedded page's forwarding leave those keys to it.

import { isMacPlatform, isTextEntry } from "@/lib/hotkeys";
import type { DesktopCommand } from "@/lib/nativeBridge";
import { TERMINAL_SURFACE } from "@/lib/paneSurfaces";
import { type Chord, chordMatches, type KeyChordInput, chordAriaShortcut } from "./chord";
import {
  activeShortcutChords,
  currentShortcutEnvironment,
  matchesShortcut,
  menuShortcutChords,
  SHORTCUT_BINDINGS,
  type ShortcutBinding,
  type ShortcutEnvironment,
  type ShortcutId,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "./registry";

/**
 * Marks an editor root with the keys it owns: `text` for caret and deletion keys, `rich` for
 * formatting chords too. A marked editor is never treated as a code editor.
 */
export const EDITOR_KEYS_ATTRIBUTE = "data-editor-keys";

/** The kinds of keyboard owner, from the most chords kept to the fewest. */
export type KeyOwner = "code" | "terminal" | "rich" | "text";

/** Focused surfaces that are code editors: Monaco and the CodeMirror file editor. */
export const CODE_EDITOR_FOCUS_SURFACES = `.monaco-editor, .cm-editor:not([${EDITOR_KEYS_ATTRIBUTE}])`;

const MARKED_EDITOR = `[${EDITOR_KEYS_ATTRIBUTE}]`;
const RICH_TEXT = "[contenteditable]:not([contenteditable='false'])";

/** The keyboard owner `element` sits in, or null for content that doesn't edit text. */
export function keyOwnerOf(element: Element | null): KeyOwner | null {
  if (!(element instanceof HTMLElement)) return null;
  const marked = element.closest(MARKED_EDITOR)?.getAttribute(EDITOR_KEYS_ATTRIBUTE);
  if (marked === "rich" || marked === "text") return marked;
  if (element.closest(CODE_EDITOR_FOCUS_SURFACES)) return "code";
  if (element.closest(TERMINAL_SURFACE)) return "terminal";
  if (!isTextEntry(element)) return null;
  // A rich-text editor is contenteditable; plain fields keep only the text keys.
  return element.closest(RICH_TEXT) ? "rich" : "text";
}

/** The keyboard owner that has focus now. */
export function focusedKeyOwner(): KeyOwner | null {
  if (typeof document === "undefined") return null;
  return keyOwnerOf(document.activeElement);
}

/** Whether keyboard focus is inside a code editor. */
export function isCodeEditorFocused(): boolean {
  return focusedKeyOwner() === "code";
}

/** The code editor's own commands, such as Outdent on ⌘[ and Fold on ⌃⇧[. */
const CODE_EDITOR_BINDINGS = WORKSPACE_SIDEBAR_BINDINGS.filter((binding) =>
  binding.id.startsWith("file."),
);

const withShift = (chords: Chord[]): Chord[] => chords.flatMap((c) => [c, { ...c, shift: true }]);

/** Caret movement and deletion, which every text owner keeps. */
function caretChords(mac: boolean): Chord[] {
  const lineAndWord: Chord[] = [
    { mod: true, key: "ArrowLeft" },
    { mod: true, key: "ArrowRight" },
    { mod: true, key: "ArrowUp" },
    { mod: true, key: "ArrowDown" },
    // Option moves by word on macOS; off it, Alt+← / → is Back and Forward.
    ...(mac
      ? [
          { alt: true, key: "ArrowLeft" },
          { alt: true, key: "ArrowRight" },
        ]
      : []),
  ];
  const deletion: Chord[] = [
    { mod: true, key: "Backspace" },
    { mod: true, key: "Delete" },
    ...(mac
      ? [
          { alt: true, key: "Backspace" },
          { alt: true, key: "Delete" },
        ]
      : []),
  ];
  return [...withShift(lineAndWord), ...deletion];
}

/**
 * The macOS text-system Control keys (⌃A, ⌃E, ⌃K, ⌃W, …), by physical key so they also work on
 * non-Latin layouts. Off macOS Control is the command key, so none are kept there.
 */
const READLINE_LETTERS = ["A", "E", "B", "F", "P", "N", "K", "U", "Y", "D", "H", "T", "W"];
function readlineChords(mac: boolean): Chord[] {
  return mac ? READLINE_LETTERS.map((letter) => ({ ctrl: true, code: `Key${letter}` })) : [];
}

/** Bold and italic in a rich-text editor, which its toolbar names too. */
export const BOLD_CHORD: Chord = { mod: true, code: "KeyB" };
export const ITALIC_CHORD: Chord = { mod: true, code: "KeyI" };

/** Formatting chords a rich-text editor keeps: bold, italic, code, strikethrough, lists, quote. */
const FORMATTING_CHORDS: Chord[] = [
  BOLD_CHORD,
  ITALIC_CHORD,
  { mod: true, code: "KeyE" },
  { mod: true, shift: true, code: "KeyX" },
  { mod: true, shift: true, code: "Digit7" },
  { mod: true, shift: true, code: "Digit8" },
  { mod: true, shift: true, code: "Period" },
];

/** The chords `owner` keeps on a platform, beyond the registry's `file.*` chords for code. */
export function ownedChords(owner: KeyOwner, mac: boolean = isMacPlatform()): Chord[] {
  const text = [...caretChords(mac), ...readlineChords(mac)];
  return owner === "rich" ? [...text, ...FORMATTING_CHORDS] : text;
}

/** Whether a focused `owner` keeps the keys `input` presses. */
export function ownerKeepsChord(
  owner: KeyOwner,
  input: KeyChordInput | globalThis.KeyboardEvent,
  mac: boolean = isMacPlatform(),
): boolean {
  return ownedChords(owner, mac).some((chord) => chordMatches(chord, input, mac));
}

/** Whether the focused surface keeps the keys `input` presses, so app hotkeys leave them alone. */
export function focusOwnsChord(
  input: KeyChordInput | globalThis.KeyboardEvent,
  mac: boolean = isMacPlatform(),
): boolean {
  const owner = focusedKeyOwner();
  return owner !== null && ownerKeepsChord(owner, input, mac);
}

/**
 * Whether a focused code editor keeps the keys `event` presses as one of its own `file.*`
 * commands, such as ⇧⌘\ for Go to matching bracket, so an app hotkey on the same keys yields.
 */
export function codeEditorKeepsChord(
  event: globalThis.KeyboardEvent,
  environment: Partial<ShortcutEnvironment>,
): boolean {
  if (!isCodeEditorFocused()) return false;
  return CODE_EDITOR_BINDINGS.some((binding) => matchesShortcut(event, binding.id, environment));
}

/**
 * Whether any text field keeps `chord`, so an embedded web page, whose focus the app can't see,
 * keeps it rather than handing it to the app.
 */
export function isTextEditingChord(chord: Chord, mac: boolean = isMacPlatform()): boolean {
  const aria = chordAriaShortcut(chord, mac);
  return ownedChords("text", mac).some((owned) => chordAriaShortcut(owned, mac) === aria);
}

const cache = new Map<string, ReadonlySet<DesktopCommand>>();

// The desktop menu items whose accelerators reach the page as commands, and Go Back and Go
// Forward, page hotkeys that the desktop Go menu also lists. Close Window never reaches the page.
const BINDING_DESKTOP_COMMANDS: Partial<Record<ShortcutId, DesktopCommand>> = {
  "tab.close": "close",
  "session.pin": "toggle-pin-session",
  "session.archive": "archive-session",
  "session.fork": "fork-session",
  "app.quit": "quit",
  "browser.newTab": "new-browser-tab",
  "tab.previous": "previous-tab",
  "tab.next": "next-tab",
  "tab.reopen": "reopen-closed-tab",
  "browser.reload": "reload",
  "browser.focusAddressBar": "focus-address-bar",
  "history.back": "go-back",
  "history.forward": "go-forward",
};

/** The desktop command a binding's chord runs through the menu, if any. */
function bindingDesktopCommand(binding: ShortcutBinding): DesktopCommand | null {
  return BINDING_DESKTOP_COMMANDS[binding.id] ?? null;
}

/**
 * Desktop menu commands whose chords the focused `owner` also keeps on this platform, such as
 * Go Back's ⌘[ (a code editor's outdent) or Focus Address Bar's ⌘L (expand selection). While it
 * has focus the page stops offering these to the shell, so the menu leaves those keys to it.
 */
export function editorOwnedDesktopCommands(
  environment?: Partial<ShortcutEnvironment>,
  owner: KeyOwner = "code",
): ReadonlySet<DesktopCommand> {
  // Menu chords exist only in a desktop app whose menu runs them.
  const resolved = currentShortcutEnvironment({
    ...environment,
    native: true,
    desktopMenu: true,
  });
  const key = `${owner}:${resolved.mac}`;
  const cached = cache.get(key);
  if (cached) return cached;
  const aria = (chord: Chord) => chordAriaShortcut(chord, resolved.mac);
  const kept = new Set(ownedChords(owner, resolved.mac).map(aria));
  if (owner === "code") {
    // The sidebar's code editor commands have no desktop menu accelerators.
    for (const binding of CODE_EDITOR_BINDINGS)
      activeShortcutChords(binding.id, resolved).forEach((chord) => kept.add(aria(chord)));
  }
  const owned = new Set<DesktopCommand>();
  for (const binding of SHORTCUT_BINDINGS) {
    const command = bindingDesktopCommand(binding);
    const chords = [
      ...activeShortcutChords(binding.id, resolved),
      ...menuShortcutChords(binding.id, resolved),
    ].map(aria);
    if (command && chords.some((chord) => kept.has(chord))) owned.add(command);
  }
  cache.set(key, owned);
  return owned;
}
