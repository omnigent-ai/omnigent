import { describe, expect, it } from "vitest";

import type { Chord, KeyChordInput } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  matchesShortcut,
  shortcutDisplayRow,
  SHORTCUT_BINDINGS,
  type ShortcutEnvironment,
  type ShortcutId,
} from "@/lib/shortcuts/registry";
import { type ShortcutPageContext, shortcutPageGroups } from "@/lib/shortcuts/shortcutPage";

import { CHORD_TIER, parseChordQuery, rowsForChord, searchableChords } from "./chordQuery";
import type { SettingsAvailability } from "./catalog/types";
import { searchSettings, settingsSearchTargets, textMatcher } from "./settingsSearchIndex";
import { settingsSearchMatchers } from "./settingsSearchMatchers";
import { shortcutSettingsEntries } from "./shortcutEntries";

const press = (
  key: string,
  code: string,
  modifiers: Partial<KeyChordInput> = {},
): KeyChordInput => ({
  key,
  code,
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...modifiers,
});

describe("parseChordQuery", () => {
  it("reads the issue's spellings of ⌥B and ⌘B", () => {
    const optionB = press("b", "KeyB", { altKey: true });
    for (const query of ["option b", "alt b", "opt+b", "⌥B", "⌥ b", "Alt-B"]) {
      expect(parseChordQuery(query, true)).toEqual(optionB);
    }
    const commandB = press("b", "KeyB", { metaKey: true });
    for (const query of ["cmd-b", "command b", "⌘B", "cmd+B", "meta b"]) {
      expect(parseChordQuery(query, true)).toEqual(commandB);
    }
  });

  it("names Control and Alt by their platform names off macOS, where ⌘ isn't a modifier", () => {
    expect(parseChordQuery("ctrl b", false)).toEqual(press("b", "KeyB", { ctrlKey: true }));
    expect(parseChordQuery("control+b", false)).toEqual(press("b", "KeyB", { ctrlKey: true }));
    expect(parseChordQuery("alt b", false)).toEqual(press("b", "KeyB", { altKey: true }));
    expect(parseChordQuery("cmd b", false)).toBeNull();
    expect(parseChordQuery("⌘B", false)).toBeNull();
  });

  it("ignores order and case, and splits glyph runs", () => {
    const shiftCommandT = press("t", "KeyT", { metaKey: true, shiftKey: true });
    expect(parseChordQuery("shift cmd t", true)).toEqual(shiftCommandT);
    expect(parseChordQuery("⌘⇧T", true)).toEqual(shiftCommandT);
    expect(parseChordQuery("T SHIFT CMD", true)).toEqual(shiftCommandT);
    expect(parseChordQuery("⌘⌥[", true)).toEqual(
      press("[", "BracketLeft", { metaKey: true, altKey: true }),
    );
    expect(parseChordQuery("⌃⇥", true)).toEqual(press("Tab", "Tab", { ctrlKey: true }));
    expect(parseChordQuery("alt-shift-a", true)).toEqual(
      press("a", "KeyA", { altKey: true, shiftKey: true }),
    );
  });

  it("reads separators between glyphs and words, with or without spaces", () => {
    const commandAltBracket = press("[", "BracketLeft", { metaKey: true, altKey: true });
    for (const query of ["⌘+⌥+[", "⌘-⌥-[", "cmd+⌥+[", "⌘+alt+[", "cmd + alt + [", "⌘ + ⌥ + ["]) {
      expect(parseChordQuery(query, true), query).toEqual(commandAltBracket);
    }
    expect(parseChordQuery("⌃+Tab", true)).toEqual(press("Tab", "Tab", { ctrlKey: true }));
    expect(parseChordQuery("ctrl - tab", false)).toEqual(press("Tab", "Tab", { ctrlKey: true }));
    expect(parseChordQuery("⌘+-", true)).toEqual(press("-", "Minus", { metaKey: true }));
    expect(parseChordQuery("cmd + +", true)).toEqual(
      press("+", "Equal", { metaKey: true, shiftKey: true }),
    );
    expect(parseChordQuery("⌘⌥+-", true)).toEqual(
      press("-", "Minus", { metaKey: true, altKey: true }),
    );
  });

  it("keeps + and - as keys where they can't be separators", () => {
    const plus = press("+", "Equal", { metaKey: true, shiftKey: true });
    expect(parseChordQuery("cmd++", true)).toEqual(plus);
    expect(parseChordQuery("cmd plus", true)).toEqual(plus);
    expect(parseChordQuery("ctrl+plus", false)).toEqual(
      press("+", "Equal", { ctrlKey: true, shiftKey: true }),
    );
    const minus = press("-", "Minus", { metaKey: true, altKey: true });
    expect(parseChordQuery("cmd alt -", true)).toEqual(minus);
    expect(parseChordQuery("cmd+alt+-", true)).toEqual(minus);
    expect(parseChordQuery("cmd alt minus", true)).toEqual(minus);
    expect(parseChordQuery("cmd-", true)).toEqual(press("-", "Minus", { metaKey: true }));
  });

  it("maps punctuation and named keys to the codes the registry matches", () => {
    const cases: [string, string, string][] = [
      ["cmd alt ,", ",", "Comma"],
      ["cmd alt ]", "]", "BracketRight"],
      ["cmd alt \\", "\\", "Backslash"],
      ["cmd `", "`", "Backquote"],
      ["cmd =", "=", "Equal"],
      ["cmd /", "/", "Slash"],
      ["cmd 1", "1", "Digit1"],
      ["cmd enter", "Enter", "Enter"],
      ["cmd up", "ArrowUp", "ArrowUp"],
      ["cmd ↓", "ArrowDown", "ArrowDown"],
    ];
    for (const [query, key, code] of cases) {
      expect(parseChordQuery(query, true)).toMatchObject({ key, code });
    }
  });

  it("tells Backspace from Delete on each platform", () => {
    expect(parseChordQuery("cmd delete", true)?.key).toBe("Backspace");
    expect(parseChordQuery("cmd ⌫", true)?.key).toBe("Backspace");
    expect(parseChordQuery("cmd fn delete", true)?.key).toBe("Delete");
    expect(parseChordQuery("cmd forward delete", true)?.key).toBe("Delete");
    expect(parseChordQuery("cmd ⌦", true)?.key).toBe("Delete");
    expect(parseChordQuery("ctrl delete", false)?.key).toBe("Delete");
    expect(parseChordQuery("ctrl backspace", false)?.key).toBe("Backspace");
  });

  it("reads a named key on its own, but not a character", () => {
    expect(parseChordQuery("esc", true)).toEqual(press("Escape", "Escape"));
    expect(parseChordQuery("F5", false)).toEqual(press("F5", "F5"));
    for (const query of [
      "",
      "b",
      "1",
      "sidebar",
      "cmd",
      "cmd b c",
      "⌘⌘b",
      "cmd sidebar",
      "space",
    ]) {
      expect(parseChordQuery(query, true)).toBeNull();
    }
  });
});

const CODE_NAMES: Record<string, string> = {
  Minus: "-",
  Equal: "=",
  Comma: ",",
  Period: ".",
  Slash: "/",
  Backslash: "\\",
  Semicolon: ";",
  Quote: "'",
  BracketLeft: "[",
  BracketRight: "]",
  Backquote: "`",
};
const KEY_NAMES: Record<string, string> = {
  Enter: "enter",
  Escape: "esc",
  Tab: "tab",
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  "+": "plus",
};

function keyName(chord: Chord): string | null {
  if (chord.code !== undefined) {
    if (chord.code.startsWith("Key")) return chord.code.slice(3).toLowerCase();
    if (chord.code.startsWith("Digit")) return chord.code.slice(5);
    return CODE_NAMES[chord.code] ?? null;
  }
  if (chord.key === undefined) return null;
  return KEY_NAMES[chord.key] ?? (chord.key.length === 1 ? chord.key.toLowerCase() : null);
}

function modifierWords(chord: Chord, mac: boolean): string[] {
  const words: string[] = [];
  if (chord.mod) words.push(mac ? "cmd" : "ctrl");
  if (chord.ctrl && !(chord.mod && !mac)) words.push("ctrl");
  if (chord.alt) words.push("alt");
  if (chord.shift) words.push("shift");
  return words;
}

const GLYPHS: Record<string, string> = { cmd: "⌘", ctrl: "⌃", alt: "⌥", shift: "⇧" };

/** Glyphs (on macOS), words and a +-joined form of the same chord. */
function spellings(chord: Chord, mac: boolean): string[] {
  const key = keyName(chord);
  if (key === null) throw new Error(`No name for ${JSON.stringify(chord)}`);
  const words = modifierWords(chord, mac);
  const forms = [[...words, key].join(" "), [...words, key].join("+"), [...words, key].join(" + ")];
  if (mac && words.length > 0) {
    const glyphs = words.map((word) => GLYPHS[word]);
    forms.push(`${glyphs.join("")}${key}`, [...glyphs, key].join("+"));
    // A glyph, then words, joined by separators.
    forms.push([glyphs[0], ...words.slice(1), key].join("-"));
  }
  return forms;
}

/** The test's own statement of the scope rules, read from the registry's metadata. */
function isBarePrintable(chord: Chord): boolean {
  const modifierFree = !chord.mod && !chord.ctrl && !chord.alt && !chord.shift;
  if (!modifierFree) return false;
  if (chord.key !== undefined) return chord.key.length === 1;
  return (
    chord.code !== undefined &&
    /^(Key.|Digit.|Minus|Equal|Comma|Period|Slash|Backslash|Semicolon|Quote|BracketLeft|BracketRight|Backquote)$/.test(
      chord.code,
    )
  );
}

/** Every environment the registry reads, as the page's own runtime can set them. */
const ENVIRONMENTS: ShortcutEnvironment[] = [true, false].flatMap((mac) =>
  [true, false].flatMap((native) =>
    [true, false].map((desktopMenu) => ({ mac, native, desktopMenu, submitWithModEnter: false })),
  ),
);
const MAC_DESKTOP: ShortcutEnvironment = {
  mac: true,
  native: true,
  desktopMenu: true,
  submitWithModEnter: false,
};

const ALL_LISTED: ReadonlySet<ShortcutId> = new Set(SHORTCUT_BINDINGS.map((binding) => binding.id));

const rows = (query: string, environment: ShortcutEnvironment) => {
  const input = parseChordQuery(query, environment.mac);
  return input ? [...rowsForChord(input, environment, ALL_LISTED)] : [];
};

describe("finding shortcuts by chord", () => {
  it("finds each listed binding's row by any of its searchable chords, in every spelling", () => {
    for (const environment of ENVIRONMENTS) {
      for (const binding of SHORTCUT_BINDINGS) {
        for (const chord of searchableChords(binding.id, environment)) {
          for (const spelling of spellings(chord, environment.mac)) {
            expect(
              rows(spelling, environment),
              `${spelling} in ${JSON.stringify(environment)}`,
            ).toContain(shortcutDisplayRow(binding.id));
          }
        }
      }
    }
  });

  it("finds real bindings by their keys", () => {
    expect(rows("cmd k", MAC_DESKTOP)).toContain("palette.commands");
    expect(rows("cmd /", MAC_DESKTOP)).toContain("shortcuts.show");
  });

  it("never finds a bare printable key, which only works in context", () => {
    for (const environment of ENVIRONMENTS) {
      for (const binding of SHORTCUT_BINDINGS) {
        for (const chord of activeShortcutChords(binding.id, environment)) {
          if (!isBarePrintable(chord)) continue;
          expect(searchableChords(binding.id, environment)).not.toContainEqual(chord);
        }
      }
    }
  });

  it("finds only the rows the page lists", () => {
    const input = parseChordQuery("cmd k", true)!;
    expect(rowsForChord(input, MAC_DESKTOP, new Set())).toEqual(new Set());
    expect(rowsForChord(input, MAC_DESKTOP, new Set(["palette.commands"]))).toEqual(
      new Set(["palette.commands"]),
    );
  });
});

const PAGE_CONTEXT: ShortcutPageContext = {
  native: true,
  electron: true,
  browser: true,
  submitWithModEnter: false,
  preventsKeyboardSubmit: false,
  contentSearch: true,
  grids: true,
  splitTerminal: true,
  siblingSessions: true,
  desktopCommands: true,
  quickPick: false,
};

const SHOWN: SettingsAvailability = {
  globalShortcut: true,
  floatingComposerShortcut: true,
  widenWindowForRightSidebar: true,
  inAppBrowser: true,
  desktopAppHandoff: true,
  adminView: true,
  accountsEnabled: true,
  archiveWorktreeCleanup: true,
  mcpServices: true,
  integrationProviders: { github: true, databricks: true },
  projectKanban: true,
  sessionTitleInstructions: true,
  proactiveAgent: true,
  quickPickPalette: true,
};

describe("chord results in Settings search", () => {
  const groups = shortcutPageGroups(PAGE_CONTEXT, true);
  const listed = new Set(groups.flatMap((group) => group.items.map((row) => row.shortcutId)));
  const targets = settingsSearchTargets(
    shortcutSettingsEntries(groups),
    [{ id: "shortcuts", label: "Keyboard shortcuts" }],
    SHOWN,
  );
  const matchers = settingsSearchMatchers(MAC_DESKTOP, listed);
  const keysFor = (query: string) =>
    searchSettings(query, targets, matchers).map((result) => result.target.entry.shortcutId);

  it("ranks the chord's row above text matches, each row once", () => {
    const results = keysFor("cmd k");
    expect(results[0]).toBe("palette.commands");
    expect(new Set(results).size).toBe(results.length);
    expect(CHORD_TIER).toBeLessThan(1);
  });

  it("finds a desktop menu row by the menu's accelerator, which no page hotkey matches", () => {
    expect(keysFor("cmd w")[0]).toBe("tab.close");
    expect(keysFor("cmd q")[0]).toBe("app.quit");
    expect(keysFor("cmd ,")[0]).toBe("settings.open");
    expect(keysFor("cmd opt p")[0]).toBe("session.pin");
    expect(keysFor("cmd shift a")[0]).toBe("session.archive");
    const closeTab = parseChordQuery("cmd w", true)!;
    expect(matchesShortcut(closeTab, "tab.close", MAC_DESKTOP)).toBe(false);
  });

  it("finds no menu row in a browser tab, where the menu isn't there", () => {
    const browser = { ...MAC_DESKTOP, native: false, desktopMenu: false };
    const input = parseChordQuery("cmd w", true)!;
    expect([...rowsForChord(input, browser, listed)]).not.toContain("tab.close");
  });

  it("finds the row an alternative or a part's chord is drawn on", () => {
    expect(keysFor("opt enter")[0]).toBe("composer.newLine");
    expect(keysFor("down")).toContain("slash.previous");
    expect(keysFor("down")).not.toContain("slash.next");
  });

  it("ranks other queries by text alone", () => {
    for (const query of ["sidebar", "terminal", "send"]) {
      expect(keysFor(query)).toEqual(
        searchSettings(query, targets, [textMatcher]).map(
          (result) => result.target.entry.shortcutId,
        ),
      );
    }
  });
});
