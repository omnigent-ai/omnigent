import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import type { Chord, KeyChordInput } from "@/lib/shortcuts/chord";
import { chordMatches } from "@/lib/shortcuts/chord";
import {
  OPEN_SESSION_CHORDS,
  QUICK_PICK_SHORTCUT_BINDINGS,
  SHORTCUT_BINDINGS,
  type ShortcutEnvironment,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";
import { openSessionCommands } from "@/shell/openSessionCommands";

import { HOSTED_SHORTCUT_COMMANDS, OPEN_SESSION_SHORTCUT_COMMANDS } from "./QuickPickShortcutHost";

/** Flagged bindings whose chord something else already handles, and what. */
const HANDLED_ELSEWHERE: Record<string, string> = {
  "message.previous": "The transcript's own ⌘⌥↑ listener; the binding gives its ⌘K row the keys.",
  "message.next": "The transcript's own ⌘⌥↓ listener; the binding gives its ⌘K row the keys.",
};

/** Flagged bindings with their own listener, whose chords nothing else may take. */
const OWN_LISTENER: Record<string, string> = {
  "session.recent": "useRecentSessionHotkey, which opens and steps the hold-to-switch switcher.",
  "session.recentReverse": "useRecentSessionHotkey, which steps the held switcher back.",
};

/** Flagged bindings that take over another's chord, whose own handler stands down with the flag on. */
const TAKES_OVER: Record<string, readonly string[]> = {
  // AppShellOverlays turns the recent switcher off with quick_pick_palette on.
  "session.next": ["sessions.recent"],
  "session.previous": ["sessions.recent"],
};

const ENVIRONMENTS: ShortcutEnvironment[] = [true, false].flatMap((mac) =>
  [true, false].flatMap((native) =>
    [true, false].flatMap((desktopMenu) =>
      // A shell from before the Go list has the command bridge, and leaves ⌘[ / ⌘] to the page.
      (desktopMenu ? [false, true] : [false]).map((goMenu) => ({
        mac,
        native,
        desktopMenu,
        goMenu,
        submitWithModEnter: false,
      })),
    ),
  ),
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

describe("shortcuts that wait for quick_pick_palette", () => {
  it("stay out of the bindings the Shortcuts page and Settings search read", () => {
    expect(SHORTCUT_BINDINGS.filter((binding) => binding.flag !== undefined)).toEqual([]);
    expect(QUICK_PICK_SHORTCUT_BINDINGS.length).toBeGreaterThan(0);
  });

  it("each run a ⌘K command through the host, or are handled elsewhere, exactly once", () => {
    const hosted = [
      ...Object.keys(HOSTED_SHORTCUT_COMMANDS),
      ...Object.keys(OPEN_SESSION_SHORTCUT_COMMANDS),
    ];
    const elsewhere = [...Object.keys(HANDLED_ELSEWHERE), ...Object.keys(OWN_LISTENER)];
    expect(hosted.filter((id) => elsewhere.includes(id))).toEqual([]);
    expect([...hosted, ...elsewhere].toSorted()).toEqual(
      QUICK_PICK_SHORTCUT_BINDINGS.map((binding) => binding.id).toSorted(),
    );
  });

  it("take no chord another binding already has, in any environment", () => {
    const others = [...SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS];
    for (const environment of ENVIRONMENTS) {
      for (const binding of QUICK_PICK_SHORTCUT_BINDINGS) {
        if (binding.id in HANDLED_ELSEWHERE) continue;
        for (const chord of binding.chords(environment)) {
          const press = pressFor(chord, environment.mac);
          const taken = others.filter((other) =>
            other
              .chords(environment)
              .some((otherChord) => chordMatches(otherChord, press, environment.mac)),
          );
          const replaced = TAKES_OVER[binding.id] ?? [];
          expect(
            taken.map((other) => other.id).filter((id) => !replaced.includes(id)),
            `${binding.id} ${JSON.stringify(environment)}`,
          ).toEqual([]);
        }
      }
    }
  });

  it("take the open session's own chords only for its own commands", () => {
    // Every command the header menu can show, with the desktop shell's chords bound.
    const nativeChords = openSessionCommands({
      canAddAgent: true,
      pinnable: true,
      pinned: false,
      pinSaving: false,
      canShare: true,
      canFork: true,
      canMarkUnread: true,
      canCreateAutomation: true,
      archived: false,
      desktopShortcuts: true,
    }).flatMap((command) => (command.nativeShortcut ? [command.nativeShortcut] : []));
    expect(nativeChords.length).toBeGreaterThan(0);
    for (const environment of ENVIRONMENTS) {
      for (const chord of nativeChords) {
        const press = pressFor(chord, environment.mac);
        const taken = QUICK_PICK_SHORTCUT_BINDINGS.filter(
          (binding) =>
            !(binding.id in OPEN_SESSION_SHORTCUT_COMMANDS) &&
            binding.chords(environment).some((own) => chordMatches(own, press, environment.mac)),
        );
        expect(
          taken.map((binding) => binding.id),
          JSON.stringify(chord),
        ).toEqual([]);
      }
    }
  });

  it("give the browser the desktop menu's Pin, Fork and Archive keys, leaving the menu its own", () => {
    const chordsIn = (id: string, environment: ShortcutEnvironment) =>
      QUICK_PICK_SHORTCUT_BINDINGS.find((binding) => binding.id === id)!.chords(environment);
    for (const environment of ENVIRONMENTS) {
      const browser = !environment.desktopMenu;
      // Off macOS, Ctrl+Alt+P and Ctrl+Alt+F are Go to a file and Search text in files.
      const pinAndFork = browser && environment.mac;
      expect(chordsIn("session.pin", environment)).toEqual(
        pinAndFork ? [OPEN_SESSION_CHORDS.pin] : [],
      );
      expect(chordsIn("session.fork", environment)).toEqual(
        pinAndFork ? [OPEN_SESSION_CHORDS.fork] : [],
      );
      expect(chordsIn("session.archive", environment)).toEqual(
        browser ? [OPEN_SESSION_CHORDS.archive] : [],
      );
    }
  });

  it("match the desktop menu's accelerators", () => {
    // The menu is built in the desktop shell, which can't import this module.
    const menu = readFileSync("electron/src/main.js", "utf8");
    const accelerator = (menuId: string) =>
      new RegExp(`id: "${menuId}",[\\s\\S]*?accelerator: "([^"]+)"`).exec(menu)?.[1];
    const asAccelerator = (chord: Chord) =>
      [
        chord.mod && "CmdOrCtrl",
        chord.alt && "Alt",
        chord.shift && "Shift",
        (chord.code?.replace(/^Key/, "") ?? chord.key ?? "").toUpperCase(),
      ]
        .filter(Boolean)
        .join("+");
    expect(accelerator("toggle_pin_session")).toBe(asAccelerator(OPEN_SESSION_CHORDS.pin));
    expect(accelerator("fork_session")).toBe(asAccelerator(OPEN_SESSION_CHORDS.fork));
    expect(accelerator("archive_session")).toBe(asAccelerator(OPEN_SESSION_CHORDS.archive));
  });

  it("leave ⌘0 as it is", () => {
    for (const environment of ENVIRONMENTS) {
      const zero = pressFor({ mod: true, code: "Digit0" }, environment.mac);
      for (const binding of QUICK_PICK_SHORTCUT_BINDINGS) {
        expect(
          binding.chords(environment).some((chord) => chordMatches(chord, zero, environment.mac)),
          binding.id,
        ).toBe(false);
      }
    }
  });
});
