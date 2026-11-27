import { afterEach, describe, expect, it } from "vitest";

import {
  editorOwnedDesktopCommands,
  focusOwnsChord,
  isTextEditingChord,
  keyOwnerOf,
  type KeyOwner,
} from "./editorOwnedChords";
import {
  activeShortcutChords,
  menuShortcutChords,
  type ShortcutId as AppShortcutId,
  type WorkspaceSidebarShortcutId,
} from "./registry";
import { chordAriaShortcut } from "@/lib/shortcuts/chord";

const DESKTOP = { native: true, desktopMenu: true } as const;

/** The aria chord strings app binding `id` runs on, page or menu, in the desktop app. */
function chords(id: AppShortcutId, mac: boolean): string[] {
  const environment = { ...DESKTOP, mac };
  return [...activeShortcutChords(id, environment), ...menuShortcutChords(id, environment)].map(
    (chord) => chordAriaShortcut(chord, mac),
  );
}

/** The aria chord strings a code editor command runs on; it has no menu accelerators. */
function editorChords(id: WorkspaceSidebarShortcutId, mac: boolean): string[] {
  return activeShortcutChords(id, { ...DESKTOP, mac }).map((chord) =>
    chordAriaShortcut(chord, mac),
  );
}

describe("editorOwnedDesktopCommands", () => {
  it("leaves ⌘[ / ⌘] and ⌘L to the editor on the Mac, but not ⌘⇧[ / ⌘⇧]", () => {
    expect([...editorOwnedDesktopCommands({ mac: true })].sort()).toEqual([
      "focus-address-bar",
      "go-back",
      "go-forward",
    ]);
  });

  it("also leaves Ctrl+Shift+[ / ] to the editor off the Mac, where they fold and unfold", () => {
    expect([...editorOwnedDesktopCommands({ mac: false })].sort()).toEqual([
      "focus-address-bar",
      "go-back",
      "go-forward",
      "next-tab",
      "previous-tab",
    ]);
  });

  // Each pair is a real collision: the menu command's chord is also an editor chord.
  it.each([
    [true, "history.back", "file.outdentLines"],
    [true, "history.forward", "file.indentLines"],
    [true, "browser.focusAddressBar", "file.expandLineSelection"],
    [false, "history.back", "file.outdentLines"],
    [false, "history.forward", "file.indentLines"],
    [false, "browser.focusAddressBar", "file.expandLineSelection"],
    [false, "tab.previous", "file.fold"],
    [false, "tab.next", "file.unfold"],
  ] as const)("mac=%s: %s shares a chord with %s", (mac, menuId, editorId) => {
    const editor = new Set(editorChords(editorId, mac));
    expect(chords(menuId, mac).some((chord) => editor.has(chord))).toBe(true);
  });

  it("keeps tab stepping out of the editor's chords on the Mac", () => {
    const editor = new Set([
      ...editorChords("file.fold", true),
      ...editorChords("file.unfold", true),
    ]);
    expect(chords("tab.previous", true).some((chord) => editor.has(chord))).toBe(false);
  });
});

/** A focused element built from `html`, whose innermost `[data-focus]` element takes focus. */
function focusIn(html: string): HTMLElement {
  document.body.innerHTML = html;
  const target = document.querySelector<HTMLElement>("[data-focus]")!;
  target.tabIndex = 0;
  target.focus();
  return target;
}

afterEach(() => document.body.replaceChildren());

describe("keyOwnerOf", () => {
  it.each<[string, string, KeyOwner | null]>([
    ["page content", "<button data-focus>Open</button>", null],
    ["a checkbox", '<input type="checkbox" data-focus>', null],
    ["a text input", '<input type="text" data-focus>', "text"],
    ["a textarea", "<textarea data-focus></textarea>", "text"],
    ["a rich-text editor", '<div contenteditable="true"><p data-focus>x</p></div>', "rich"],
    ["a read-only rich view", '<div contenteditable="false"><p data-focus>x</p></div>', null],
    ["Monaco", '<div class="monaco-editor"><textarea data-focus></textarea></div>', "code"],
    ["the CodeMirror file editor", '<div class="cm-editor"><div data-focus></div></div>', "code"],
    [
      "a marked CodeMirror editor",
      '<div data-editor-keys="text"><div class="cm-editor"><div data-focus></div></div></div>',
      "text",
    ],
    [
      "a marked textarea for rich keys",
      '<div data-editor-keys="rich"><textarea data-focus></textarea></div>',
      "rich",
    ],
    ["a terminal", "<div data-terminal-surface><textarea data-focus></textarea></div>", "terminal"],
  ])("%s", (_name, html, owner) => {
    expect(keyOwnerOf(focusIn(html))).toBe(owner);
  });
});

describe("focusOwnsChord", () => {
  const keydown = (init: KeyboardEventInit) => new KeyboardEvent("keydown", init);
  const COMMAND_LEFT = keydown({ key: "ArrowLeft", metaKey: true });

  it("keeps ⌘← / ⌘→ for every text owner on macOS, and nowhere else", () => {
    focusIn("<button data-focus>Open</button>");
    expect(focusOwnsChord(COMMAND_LEFT, true)).toBe(false);
    for (const html of [
      "<textarea data-focus></textarea>",
      '<div contenteditable="true" data-focus></div>',
      '<div class="monaco-editor"><textarea data-focus></textarea></div>',
      "<div data-terminal-surface><textarea data-focus></textarea></div>",
    ]) {
      focusIn(html);
      expect(focusOwnsChord(COMMAND_LEFT, true)).toBe(true);
      expect(
        focusOwnsChord(keydown({ key: "ArrowRight", metaKey: true, shiftKey: true }), true),
      ).toBe(true);
    }
  });

  it("keeps the macOS Control text keys in text, matched by physical key", () => {
    focusIn("<textarea data-focus></textarea>");
    // ⌃W on a Cyrillic layout still reports KeyW.
    expect(focusOwnsChord(keydown({ key: "ц", code: "KeyW", ctrlKey: true }), true)).toBe(true);
    expect(focusOwnsChord(keydown({ key: "a", code: "KeyA", ctrlKey: true }), true)).toBe(true);
    // Option letters type characters, and ⌃⌥ belongs to VoiceOver.
    expect(focusOwnsChord(keydown({ key: "∫", code: "KeyB", altKey: true }), true)).toBe(false);
    expect(
      focusOwnsChord(keydown({ key: "w", code: "KeyW", ctrlKey: true, altKey: true }), true),
    ).toBe(false);
  });

  it("keeps no Control letter off macOS, where Ctrl+W closes and Ctrl+A selects all", () => {
    focusIn("<textarea data-focus></textarea>");
    expect(focusOwnsChord(keydown({ key: "w", code: "KeyW", ctrlKey: true }), false)).toBe(false);
    expect(focusOwnsChord(keydown({ key: "ArrowLeft", ctrlKey: true }), false)).toBe(true);
  });

  it("keeps ⌘B for bold only in rich text", () => {
    const bold = keydown({ key: "b", code: "KeyB", metaKey: true });
    focusIn("<textarea data-focus></textarea>");
    expect(focusOwnsChord(bold, true)).toBe(false);
    focusIn('<div data-editor-keys="rich"><textarea data-focus></textarea></div>');
    expect(focusOwnsChord(bold, true)).toBe(true);
    focusIn('<div contenteditable="true" data-focus></div>');
    expect(focusOwnsChord(bold, true)).toBe(true);
  });

  it("never keeps the palette's ⌘K", () => {
    focusIn('<div contenteditable="true" data-focus></div>');
    expect(focusOwnsChord(keydown({ key: "k", code: "KeyK", metaKey: true }), true)).toBe(false);
  });
});

describe("isTextEditingChord", () => {
  it("names the workspace history chords an embedded page's text field keeps", () => {
    expect(isTextEditingChord({ mod: true, key: "ArrowLeft" }, true)).toBe(true);
    expect(isTextEditingChord({ mod: true, alt: true, shift: true, key: "ArrowLeft" }, true)).toBe(
      false,
    );
  });

  it("names the Mac's Control text keys, such as ⌃P, but not Ctrl+P off macOS", () => {
    expect(isTextEditingChord({ ctrl: true, code: "KeyP" }, true)).toBe(true);
    expect(isTextEditingChord({ ctrl: true, code: "KeyP" }, false)).toBe(false);
  });
});

describe("editorOwnedDesktopCommands for text owners", () => {
  // No menu command uses a text key today; a new one that does is left to the focused field.
  it.each<KeyOwner>(["text", "rich", "terminal"])("%s keeps no menu command today", (owner) => {
    expect([...editorOwnedDesktopCommands({ mac: true }, owner)]).toEqual([]);
    expect([...editorOwnedDesktopCommands({ mac: false }, owner)]).toEqual([]);
  });
});
