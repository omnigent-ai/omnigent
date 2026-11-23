// Finding a keyboard shortcut by typing its chord: "option b", "alt+b", "⌥B" and "cmd-[" read as
// the keys they name, matched against the chords the registry's bindings press, the way the
// hotkeys themselves match a keydown.

import { type Chord, chordMatches, type KeyChordInput } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  APP_SHORTCUT_BINDINGS,
  menuShortcutChords,
  type ShortcutEnvironment,
  type ShortcutId,
  shortcutDisplayRow,
} from "@/lib/shortcuts/registry";

import type { SettingsSearchMatcher } from "./settingsSearchIndex";

type Modifier = "metaKey" | "ctrlKey" | "altKey" | "shiftKey";

const MODIFIER_GLYPHS: Readonly<Record<string, Modifier>> = {
  "⌘": "metaKey",
  "⌥": "altKey",
  "⌃": "ctrlKey",
  "⇧": "shiftKey",
};

function modifierWord(word: string, mac: boolean): Modifier | null {
  switch (word) {
    case "cmd":
    case "command":
    case "meta":
    case "⌘":
      // Off macOS the command key isn't a modifier the app uses, so the query stays text.
      return mac ? "metaKey" : null;
    case "opt":
    case "option":
    case "alt":
    case "⌥":
      return "altKey";
    case "ctrl":
    case "control":
    case "ctl":
    case "⌃":
      return "ctrlKey";
    case "shift":
    case "⇧":
      return "shiftKey";
    default:
      return null;
  }
}

interface TypedKey {
  key: string;
  code: string;
  /** A named key, such as Escape, that can be searched for on its own. */
  named: boolean;
  /** The key is typed with Shift, such as "+". */
  shifted?: boolean;
}

const PUNCTUATION: Readonly<Record<string, Omit<TypedKey, "named">>> = {
  "-": { key: "-", code: "Minus" },
  minus: { key: "-", code: "Minus" },
  "=": { key: "=", code: "Equal" },
  equal: { key: "=", code: "Equal" },
  equals: { key: "=", code: "Equal" },
  "+": { key: "+", code: "Equal", shifted: true },
  plus: { key: "+", code: "Equal", shifted: true },
  ",": { key: ",", code: "Comma" },
  comma: { key: ",", code: "Comma" },
  ".": { key: ".", code: "Period" },
  period: { key: ".", code: "Period" },
  "/": { key: "/", code: "Slash" },
  slash: { key: "/", code: "Slash" },
  "\\": { key: "\\", code: "Backslash" },
  backslash: { key: "\\", code: "Backslash" },
  ";": { key: ";", code: "Semicolon" },
  semicolon: { key: ";", code: "Semicolon" },
  "'": { key: "'", code: "Quote" },
  quote: { key: "'", code: "Quote" },
  "[": { key: "[", code: "BracketLeft" },
  "]": { key: "]", code: "BracketRight" },
  "`": { key: "`", code: "Backquote" },
  backquote: { key: "`", code: "Backquote" },
  backtick: { key: "`", code: "Backquote" },
  space: { key: " ", code: "Space" },
};

function namedKey(name: string, mac: boolean): string | null {
  switch (name) {
    case "enter":
    case "return":
    case "↵":
    case "⏎":
      return "Enter";
    case "esc":
    case "escape":
    case "⎋":
      return "Escape";
    case "tab":
    case "⇥":
      return "Tab";
    case "up":
    case "↑":
      return "ArrowUp";
    case "down":
    case "↓":
      return "ArrowDown";
    case "left":
    case "←":
      return "ArrowLeft";
    case "right":
    case "→":
      return "ArrowRight";
    case "home":
      return "Home";
    case "end":
      return "End";
    case "pageup":
      return "PageUp";
    case "pagedown":
      return "PageDown";
    case "backspace":
    case "⌫":
      return "Backspace";
    // On macOS the key labelled delete is Backspace; forward delete is Delete.
    case "delete":
    case "del":
      return mac ? "Backspace" : "Delete";
    case "⌦":
    case "forwarddelete":
      return "Delete";
    default:
      return /^f([1-9]|1[0-2])$/.test(name) ? name.toUpperCase() : null;
  }
}

function typedKey(name: string, mac: boolean): TypedKey | null {
  if (/^[a-z]$/.test(name)) return { key: name, code: `Key${name.toUpperCase()}`, named: false };
  if (/^[0-9]$/.test(name)) return { key: name, code: `Digit${name}`, named: false };
  const punctuation = PUNCTUATION[name];
  if (punctuation) return { ...punctuation, named: false };
  const named = namedKey(name, mac);
  return named ? { key: named, code: named, named: true } : null;
}

/**
 * Splits a query into its parts: words, modifier glyphs ("⌘⌥[" is ⌘, ⌥, [) and the keys a
 * `+` or `-` stands for. Between two parts, with or without spaces, `+` and `-` separate them;
 * anywhere else, at the end or right after another separator, they are the key ("cmd++").
 */
function queryParts(query: string): string[] {
  type Token = { kind: "part"; text: string } | { kind: "separator"; text: string };
  const tokens: Token[] = [];
  let word = "";
  const flush = () => {
    if (word !== "") tokens.push({ kind: "part", text: word });
    word = "";
  };
  for (const character of query) {
    if (/\s/.test(character)) {
      flush();
    } else if (character === "+" || character === "-") {
      flush();
      tokens.push({ kind: "separator", text: character });
    } else if (MODIFIER_GLYPHS[character] !== undefined) {
      flush();
      tokens.push({ kind: "part", text: character });
    } else {
      word += character;
    }
  }
  flush();
  const parts: string[] = [];
  let afterPart = false;
  tokens.forEach((token, index) => {
    const separates = token.kind === "separator" && afterPart && index < tokens.length - 1;
    if (separates) {
      afterPart = false;
      return;
    }
    parts.push(token.text);
    afterPart = true;
  });
  return parts;
}

/**
 * The keys a query names, as a keydown would report them: any modifiers plus exactly one key.
 * A key on its own counts only when it is a named key such as Esc; anything else is text.
 */
export function parseChordQuery(query: string, mac: boolean): KeyChordInput | null {
  const parts = queryParts(query.trim().toLowerCase())
    // "fn delete" and "forward delete" are one key.
    .reduce<string[]>((joined, part) => {
      const previous = joined.at(-1);
      if ((previous === "fn" || previous === "forward") && part === "delete") {
        joined[joined.length - 1] = "forwarddelete";
      } else {
        joined.push(part);
      }
      return joined;
    }, []);
  const input: KeyChordInput = {
    key: "",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
  };
  let key: TypedKey | null = null;
  let modifiers = 0;
  for (const part of parts) {
    const modifier = MODIFIER_GLYPHS[part] ?? modifierWord(part, mac);
    if (modifier && (part !== "⌘" || mac)) {
      if (input[modifier]) return null;
      input[modifier] = true;
      modifiers += 1;
      continue;
    }
    const typed = typedKey(part, mac);
    if (!typed || key) return null;
    key = typed;
  }
  if (!key || (modifiers === 0 && !key.named)) return null;
  return {
    ...input,
    key: key.key,
    code: key.code,
    shiftKey: input.shiftKey || key.shifted === true,
  };
}

const MODIFIER_FREE = (chord: Chord) => !chord.mod && !chord.ctrl && !chord.alt && !chord.shift;

/** One printable key and nothing else, which only means something where it's pressed. */
function isBarePrintable(chord: Chord): boolean {
  if (!MODIFIER_FREE(chord)) return false;
  if (chord.key !== undefined) return [...chord.key].length === 1;
  return (
    chord.code !== undefined &&
    /^(Key|Digit|Minus|Equal|Bracket|Backslash|Comma|Period|Slash|Semicolon|Quote|Backquote)/.test(
      chord.code,
    )
  );
}

/**
 * The chords a binding can be found by here: its active chords and the desktop menu's
 * accelerators for it, but not bare printable keys, such as the digit a workspace tab takes
 * after its chord, which only works in context.
 */
export function searchableChords(id: ShortcutId, environment: ShortcutEnvironment): Chord[] {
  return [...activeShortcutChords(id, environment), ...menuShortcutChords(id, environment)].filter(
    (chord) => !isBarePrintable(chord),
  );
}

/**
 * The listed page rows a typed chord finds here: each row showing a binding whose searchable
 * chords it presses, an alternative's or a part's chord finding the row it's drawn on.
 */
export function rowsForChord(
  input: KeyChordInput,
  environment: ShortcutEnvironment,
  listedRows: ReadonlySet<ShortcutId>,
): Set<ShortcutId> {
  const rows = new Set<ShortcutId>();
  for (const binding of APP_SHORTCUT_BINDINGS) {
    const row = shortcutDisplayRow(binding.id);
    if (!listedRows.has(row)) continue;
    if (
      searchableChords(binding.id, environment).some((chord) =>
        chordMatches(chord, input, environment.mac),
      )
    ) {
      rows.add(row);
    }
  }
  return rows;
}

/** Ranks above every text match. */
export const CHORD_TIER = 0;

/**
 * Finds the shortcut rows a typed chord presses, in `environment`, among the rows the page
 * lists. Other queries leave it out, so they rank by text alone.
 */
export function chordMatcher(
  environment: ShortcutEnvironment,
  listedRows: ReadonlySet<ShortcutId>,
): SettingsSearchMatcher {
  return (query) => {
    const input = parseChordQuery(query, environment.mac);
    if (!input) return null;
    const rows = rowsForChord(input, environment, listedRows);
    if (rows.size === 0) return null;
    return (target) =>
      target.part === undefined &&
      target.entry.shortcutId !== undefined &&
      rows.has(target.entry.shortcutId)
        ? { tier: CHORD_TIER }
        : null;
  };
}
