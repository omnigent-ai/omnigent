// Every shortcut as each surface reads it, in all sixteen environments: label, group, chords,
// key caps for tooltips and rows, aria-keyshortcuts, editor keymaps, and what Settings search
// finds. Recorded in shortcutSurfaces.golden.txt so moving bindings can't change a surface.

import { describe, expect, it } from "vitest";

import type { Chord, KeyChordInput } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  APP_SHORTCUT_BINDINGS,
  codeMirrorKeys,
  matchesShortcut,
  menuShortcutChords,
  type ShortcutEnvironment,
  type ShortcutPageContext,
  type ShortcutId,
  shortcutAriaKeys,
  shortcutBinding,
  shortcutKeys,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";
import { rowsForChord, searchableChords } from "@/pages/settings/search/chordQuery";

const ENVIRONMENTS: ShortcutEnvironment[] = [true, false].flatMap((mac) =>
  [true, false].flatMap((native) =>
    [true, false].flatMap((desktopMenu) =>
      // Without the Go menu; "a shell that routes Go" below covers what it moves.
      [true, false].map((submitWithModEnter) => ({
        mac,
        native,
        desktopMenu,
        goMenu: false,
        submitWithModEnter,
      })),
    ),
  ),
);

const EVERYTHING_SUPPORTED: ShortcutPageContext = {
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
  alwaysSteer: false,
};

const environmentName = ({ mac, native, desktopMenu, submitWithModEnter }: ShortcutEnvironment) =>
  [
    mac ? "mac" : "other",
    native ? "native" : "browser",
    desktopMenu ? "menu" : "no-menu",
    submitWithModEnter ? "mod-enter-sends" : "enter-sends",
  ].join(" ");

const IDS: ShortcutId[] = [...APP_SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS].map(
  (binding) => binding.id,
);

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
    chord.mod && (chord.eitherCommandKey ? "mod(either)" : "mod"),
    chord.ctrl && "ctrl",
    chord.alt && "alt",
    chord.shift && "shift",
    chord.code !== undefined ? `code:${chord.code}` : `key:${chord.key ?? ""}`,
    chord.exactKey && "exact",
    chord.matchesAltGraph && "altgr",
  ]
    .filter(Boolean)
    .join("+");

/** One line per binding: what each surface reads for it here. */
function surfaceLine(id: ShortcutId, environment: ShortcutEnvironment): string {
  const chords = activeShortcutChords(id, environment);
  const binding = shortcutBinding(id);
  return [
    id,
    binding.label,
    `group=${binding.group}`,
    `chords=${chords.map(chordText).join(" ") || "-"}`,
    `keys=${shortcutKeys(id, environment).join(" ") || "-"}`,
    `aria=${shortcutAriaKeys(id, environment) ?? "-"}`,
    `editor=${codeMirrorKeys(id, environment).join(" ") || "-"}`,
    `matches=${chords.map((chord) => matchesShortcut(pressFor(chord, environment.mac), id, environment)).join(" ") || "-"}`,
  ].join(" | ");
}

/** What a typed chord finds in Settings search, for each listed binding's searchable chords. */
function searchLine(id: ShortcutId, environment: ShortcutEnvironment, listed: Set<ShortcutId>) {
  const found = searchableChords(id, environment).map(
    (chord) =>
      [...rowsForChord(pressFor(chord, environment.mac), environment, listed)]
        .toSorted()
        .join(",") || "-",
  );
  return `${id} | search=${found.join(" ") || "-"}`;
}

describe("shortcut surfaces", () => {
  it("lists each binding once", () => {
    expect(new Set(IDS).size).toBe(IDS.length);
  });

  it("read as recorded in every environment", async () => {
    // Every row the page can list somewhere: all it reads but those it never lists.
    const listed = new Set<ShortcutId>(
      APP_SHORTCUT_BINDINGS.filter(
        (binding) => binding.shownWhen?.(EVERYTHING_SUPPORTED) ?? true,
      ).map((binding) => binding.id),
    );
    const lines = ENVIRONMENTS.flatMap((environment) => [
      `# ${environmentName(environment)}`,
      ...IDS.map((id) => surfaceLine(id, environment)),
      ...APP_SHORTCUT_BINDINGS.map(({ id }) => searchLine(id, environment, listed)),
    ]);
    await expect(`${lines.join("\n")}\n`).toMatchFileSnapshot("./shortcutSurfaces.golden.txt");
  });

  it("move only Back and Forward to a shell that routes Go, which then owns their chords", () => {
    const listed = new Set<ShortcutId>(APP_SHORTCUT_BINDINGS.map((binding) => binding.id));
    for (const environment of ENVIRONMENTS.filter(({ desktopMenu }) => desktopMenu)) {
      const goMenu = { ...environment, goMenu: true };
      const changed = IDS.filter(
        (id) =>
          surfaceLine(id, environment) !== surfaceLine(id, goMenu) ||
          (listed.has(id) &&
            searchLine(id, environment, listed) !== searchLine(id, goMenu, listed)),
      );
      expect(changed, environmentName(environment)).toEqual(["history.back", "history.forward"]);
      for (const id of changed) {
        // The menu's accelerator takes the chord, so the page neither matches nor shows it.
        expect(activeShortcutChords(id, goMenu)).toEqual([]);
        expect(menuShortcutChords(id, goMenu)).toHaveLength(1);
      }
    }
  });
});
