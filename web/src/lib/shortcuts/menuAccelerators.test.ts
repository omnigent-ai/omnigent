// The desktop menu's accelerators take their keys before the page sees them, so no app
// shortcut may share one unless it runs the same command. Read from the Electron sources.

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { KeyChordInput } from "@/lib/shortcuts/chord";
import {
  matchesShortcut,
  SHORTCUT_BINDINGS,
  type ShortcutId,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";

const ELECTRON_SOURCES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../electron/src",
);

/** Every literal menu accelerator in the Electron sources, such as "CmdOrCtrl+Alt+P". */
function menuAccelerators(): string[] {
  return readdirSync(ELECTRON_SOURCES)
    .filter((file) => file.endsWith(".js"))
    .flatMap((file) =>
      [
        ...readFileSync(path.join(ELECTRON_SOURCES, file), "utf8").matchAll(
          /accelerator: "([^"]+)"/g,
        ),
      ].map((match) => match[1]!),
    );
}

const PUNCTUATION_CODES: Record<string, string> = {
  "[": "BracketLeft",
  "]": "BracketRight",
  "`": "Backquote",
  ",": "Comma",
};

/** The key press `accelerator` stands for on the Mac or elsewhere. */
function acceleratorPress(accelerator: string, mac: boolean): KeyChordInput {
  const parts = accelerator.split("+");
  const key = parts.pop()!;
  const modifiers = new Set(parts);
  const commandOrControl = modifiers.has("CmdOrCtrl");
  const shift = modifiers.has("Shift");
  const letter = /^[A-Z]$/.test(key);
  return {
    key: letter ? (shift ? key : key.toLowerCase()) : key,
    code: letter ? `Key${key}` : (PUNCTUATION_CODES[key] ?? key),
    metaKey: mac && commandOrControl,
    ctrlKey: modifiers.has("Ctrl") || (!mac && commandOrControl),
    altKey: modifiers.has("Alt"),
    shiftKey: shift,
  };
}

/** Menu items whose app shortcut runs the same command, so sharing its keys is intended. */
const SAME_COMMAND: Record<string, readonly ShortcutId[]> = {
  "CmdOrCtrl+N": ["session.new"],
  "Ctrl+`": ["terminal.new"],
  "CmdOrCtrl+[": ["history.back"],
  "CmdOrCtrl+]": ["history.forward"],
  "CmdOrCtrl+F": ["find.open"],
  "CmdOrCtrl+Shift+[": ["workspace.previousTab"],
  "CmdOrCtrl+Shift+]": ["workspace.nextTab"],
};

/** App shortcuts that share `accelerator`'s keys, leaving out its same-command shortcut. */
function clashingShortcuts(accelerator: string, mac: boolean): ShortcutId[] {
  const press = acceleratorPress(accelerator, mac);
  const environment = { mac, native: true, desktopMenu: true, submitWithModEnter: true };
  return [...SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS]
    .filter(
      (binding) =>
        // The code editor's own keys act only while it has focus.
        binding.group !== "files" &&
        !SAME_COMMAND[accelerator]?.includes(binding.id) &&
        matchesShortcut(press, binding.id, environment),
    )
    .map((binding) => binding.id);
}

describe("desktop menu accelerators", () => {
  const accelerators = menuAccelerators();

  it("are read from the Electron menus", () => {
    expect(accelerators).toEqual(
      expect.arrayContaining(["CmdOrCtrl+N", "CmdOrCtrl+Alt+P", "CmdOrCtrl+Alt+F", "CmdOrCtrl+Q"]),
    );
  });

  it.each([
    ["the Mac", true],
    ["Windows and Linux", false],
  ])("share no keys with an app shortcut on %s", (_platform, mac) => {
    const clashes = Object.fromEntries(
      accelerators
        .map((accelerator) => [accelerator, clashingShortcuts(accelerator, mac)] as const)
        .filter(([, shortcuts]) => shortcuts.length > 0),
    );
    expect(clashes).toEqual({});
  });

  it("would catch an app shortcut on a menu item's keys", () => {
    expect(clashingShortcuts("CmdOrCtrl+K", false)).toContain("palette.commands");
  });
});
