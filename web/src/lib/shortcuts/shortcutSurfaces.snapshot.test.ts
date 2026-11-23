// Every shortcut as each surface reads it, in all eight environments: label, chords, key caps
// for tooltips and rows, aria-keyshortcuts, editor keymaps, and what Settings search finds.

import { describe, expect, it } from "vitest";

import type { Chord, KeyChordInput } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  codeMirrorKeys,
  matchesShortcut,
  QUICK_PICK_SHORTCUT_BINDINGS,
  SHORTCUT_BINDINGS,
  type ShortcutEnvironment,
  type ShortcutId,
  shortcutAriaKeys,
  shortcutBinding,
  shortcutKeys,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";
import { rowsForChord, searchableChords } from "@/pages/settings/search/chordQuery";

const ENVIRONMENTS: ShortcutEnvironment[] = [true, false].flatMap((mac) =>
  [true, false].flatMap((native) =>
    [true, false].map((desktopMenu) => ({ mac, native, desktopMenu, submitWithModEnter: false })),
  ),
);

const environmentName = ({ mac, native, desktopMenu }: ShortcutEnvironment) =>
  [mac ? "mac" : "other", native ? "native" : "browser", desktopMenu ? "menu" : "no-menu"].join(
    " ",
  );

const IDS: ShortcutId[] = [
  ...SHORTCUT_BINDINGS,
  ...QUICK_PICK_SHORTCUT_BINDINGS,
  ...WORKSPACE_SIDEBAR_BINDINGS,
].map((binding) => binding.id);

/** The key press `chord` describes. */
function pressFor(chord: Chord, mac: boolean): KeyChordInput {
  const code = chord.code ?? chord.key ?? "";
  const letter = /^Key([A-Z])$/.exec(code)?.[1]?.toLowerCase();
  const digit = /^Digit(\d)$/.exec(code)?.[1];
  return {
    key: chord.key ?? letter ?? digit ?? code,
    code,
    metaKey: mac && !!chord.mod,
    ctrlKey: !!chord.ctrl || (!mac && !!chord.mod),
    altKey: !!chord.alt,
    shiftKey: !!chord.shift,
  };
}

const chordText = (chord: Chord) =>
  [
    chord.mod && "mod",
    chord.ctrl && "ctrl",
    chord.alt && "alt",
    chord.shift && "shift",
    chord.code !== undefined ? `code:${chord.code}` : `key:${chord.key ?? ""}`,
    chord.exactKey && "exact",
  ]
    .filter(Boolean)
    .join("+");

/** One line per binding: what each surface reads for it here. */
function surfaceLine(id: ShortcutId, environment: ShortcutEnvironment): string {
  const chords = activeShortcutChords(id, environment);
  return [
    id,
    shortcutBinding(id).label,
    `chords=${chords.map(chordText).join(" ") || "-"}`,
    `keys=${shortcutKeys(id, environment).join(" ") || "-"}`,
    `aria=${shortcutAriaKeys(id, environment) ?? "-"}`,
    `editor=${codeMirrorKeys(id, environment).join(" ") || "-"}`,
    `matches=${chords.map((chord) => matchesShortcut(pressFor(chord, environment.mac), id, environment)).join(" ") || "-"}`,
  ].join(" | ");
}

/** What a typed chord finds in Settings search, for each listed binding's searchable chords. */
function searchLine(id: ShortcutId, environment: ShortcutEnvironment, listed: Set<ShortcutId>) {
  const found = searchableChords(id, environment).map((chord) =>
    [...rowsForChord(pressFor(chord, environment.mac), environment, listed, { quickPick: true })]
      .toSorted()
      .join(","),
  );
  return `${id} | search=${found.join(" ") || "-"}`;
}

describe("shortcut surfaces", () => {
  it("lists each binding once", () => {
    expect(new Set(IDS).size).toBe(IDS.length);
  });

  it("read as recorded in every environment", async () => {
    const listed = new Set<ShortcutId>(SHORTCUT_BINDINGS.map((binding) => binding.id));
    const lines = ENVIRONMENTS.flatMap((environment) => [
      `# ${environmentName(environment)}`,
      ...IDS.map((id) => surfaceLine(id, environment)),
      ...SHORTCUT_BINDINGS.map(({ id }) => searchLine(id, environment, listed)),
    ]);
    await expect(`${lines.join("\n")}\n`).toMatchFileSnapshot("./shortcutSurfaces.golden.txt");
  });
});
