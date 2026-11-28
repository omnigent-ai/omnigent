// The desktop menu's accelerators take their keys before the page sees them, so no app
// shortcut may share one unless it runs the same command. Read from the Electron sources.

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { KeyChordInput } from "@/lib/shortcuts/chord";
import {
  APP_SHORTCUT_BINDINGS,
  matchesShortcut,
  type ShortcutId,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";

const ELECTRON_SOURCES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../electron/src",
);

// The global Show shortcut's setting has accelerator fields too, but no menu items.
const NOT_MENU_SOURCES = new Set(["globalShowShortcut.js", "preload.js"]);

const SOURCES = readdirSync(ELECTRON_SOURCES)
  .filter((file) => file.endsWith(".js") && !NOT_MENU_SOURCES.has(file))
  .map((file) => readFileSync(path.join(ELECTRON_SOURCES, file), "utf8"));

/**
 * The keys Electron gives each menu role the app uses, on the Mac and elsewhere, when the item
 * names no accelerator of its own. An unknown role fails the test until it's listed here.
 */
const ROLE_ACCELERATORS: Record<string, { mac?: string; other?: string }> = {
  undo: { mac: "CmdOrCtrl+Z", other: "CmdOrCtrl+Z" },
  redo: { mac: "Shift+CmdOrCtrl+Z", other: "Ctrl+Y" },
  cut: { mac: "CmdOrCtrl+X", other: "CmdOrCtrl+X" },
  copy: { mac: "CmdOrCtrl+C", other: "CmdOrCtrl+C" },
  paste: { mac: "CmdOrCtrl+V", other: "CmdOrCtrl+V" },
  pasteAndMatchStyle: { mac: "Shift+Alt+CmdOrCtrl+V" },
  selectAll: { mac: "CmdOrCtrl+A", other: "CmdOrCtrl+A" },
  forceReload: { mac: "Shift+CmdOrCtrl+R", other: "Shift+CmdOrCtrl+R" },
  resetZoom: { mac: "CmdOrCtrl+0", other: "CmdOrCtrl+0" },
  zoomIn: { mac: "CmdOrCtrl+Plus", other: "CmdOrCtrl+Plus" },
  zoomOut: { mac: "CmdOrCtrl+-", other: "CmdOrCtrl+-" },
  togglefullscreen: { mac: "Ctrl+CmdOrCtrl+F", other: "F11" },
  toggleDevTools: { mac: "Alt+CmdOrCtrl+I", other: "Ctrl+Shift+I" },
  minimize: { mac: "CmdOrCtrl+M", other: "CmdOrCtrl+M" },
  close: { mac: "CmdOrCtrl+W", other: "CmdOrCtrl+W" },
  hide: { mac: "CmdOrCtrl+H" },
  hideOthers: { mac: "Alt+CmdOrCtrl+H" },
  delete: {},
  services: {},
  unhide: {},
  zoom: {},
  // Context menus and submenus: no keys of their own.
  panel: {},
  editMenu: {},
  windowMenu: {},
};

/**
 * The accelerators `source` declares, as a string or a constant naming one. Any other
 * declaration fails, so a menu item's keys can't go unchecked.
 */
function declaredAccelerators(source: string): string[] {
  return source
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\*)/.test(line))
    .flatMap((line) => [...line.matchAll(/\baccelerator:(.*)$/g)])
    .map((match) => {
      const declaration = match[1]!.trim();
      const value = /^(?:"([^"]+)"|([A-Z][A-Z0-9_]+))\s*(?:[,}]|$)/.exec(declaration);
      const keys = value?.[2]
        ? new RegExp(`const ${value[2]} = "([^"]+)"`).exec(source)?.[1]
        : value?.[1];
      if (!keys) {
        throw new Error(`Declare the accelerator \`${declaration}\` as a string or a constant`);
      }
      return keys;
    });
}

/** Every menu accelerator on the Mac or elsewhere: literal, named by a constant, or a role's. */
function menuAccelerators(mac: boolean): string[] {
  const named = SOURCES.flatMap(declaredAccelerators);
  const roles = SOURCES.flatMap((source) =>
    [...source.matchAll(/role: "(\w+)"/g)].map((match) => {
      const keys = ROLE_ACCELERATORS[match[1]!];
      if (!keys) throw new Error(`Add the "${match[1]}" role's default keys to ROLE_ACCELERATORS`);
      return mac ? keys.mac : keys.other;
    }),
  );
  return [...new Set([...named, ...roles.filter((keys) => keys !== undefined)])];
}

const PUNCTUATION_CODES: Record<string, { code: string; key: string }> = {
  "[": { code: "BracketLeft", key: "[" },
  "]": { code: "BracketRight", key: "]" },
  "`": { code: "Backquote", key: "`" },
  ",": { code: "Comma", key: "," },
  "-": { code: "Minus", key: "-" },
  Plus: { code: "Equal", key: "=" },
};

/** The key press `accelerator` stands for on the Mac or elsewhere. */
function acceleratorPress(accelerator: string, mac: boolean): KeyChordInput {
  const parts = accelerator.split("+");
  const key = parts.pop()!;
  const modifiers = new Set(parts);
  const commandOrControl = modifiers.has("CmdOrCtrl");
  const shift = modifiers.has("Shift");
  const letter = /^[A-Z]$/.test(key);
  const digit = /^\d$/.test(key);
  const punctuation = PUNCTUATION_CODES[key];
  return {
    key: letter ? (shift ? key : key.toLowerCase()) : (punctuation?.key ?? key),
    code: letter ? `Key${key}` : digit ? `Digit${key}` : (punctuation?.code ?? key),
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

/** Keys the menu takes from an app shortcut today; listed so that any new overlap fails. */
const KNOWN_OVERLAPS: Record<string, readonly ShortcutId[]> = {
  // The code editor's own keys.
  "CmdOrCtrl+[": ["file.outdentLines"],
  "CmdOrCtrl+]": ["file.indentLines"],
  "CmdOrCtrl+L": ["file.expandLineSelection"],
  "CmdOrCtrl+Shift+[": ["file.fold"],
  "CmdOrCtrl+Shift+]": ["file.unfold"],
};

/** App shortcuts that share `accelerator`'s keys, leaving out the ones listed above. */
function clashingShortcuts(accelerator: string, mac: boolean): ShortcutId[] {
  const press = acceleratorPress(accelerator, mac);
  // Both submit preferences, since some shortcuts change keys with it.
  const environments = [true, false].map((submitWithModEnter) => ({
    mac,
    native: true,
    desktopMenu: true,
    submitWithModEnter,
  }));
  const allowed = [...(SAME_COMMAND[accelerator] ?? []), ...(KNOWN_OVERLAPS[accelerator] ?? [])];
  return [...APP_SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS]
    .filter(
      (binding) =>
        !allowed.includes(binding.id) &&
        environments.some((environment) => matchesShortcut(press, binding.id, environment)),
    )
    .map((binding) => binding.id);
}

describe("desktop menu accelerators", () => {
  it.each([
    ["the Mac", true, ["CmdOrCtrl+,", "CmdOrCtrl+0", "Ctrl+CmdOrCtrl+F", "CmdOrCtrl+H"]],
    ["Windows and Linux", false, ["CmdOrCtrl+,", "CmdOrCtrl+0", "F11", "Ctrl+Y"]],
  ])("are read from the Electron menus on %s", (_platform, mac, platformKeys) => {
    expect(menuAccelerators(mac)).toEqual(
      expect.arrayContaining([
        "CmdOrCtrl+N",
        "CmdOrCtrl+Alt+P",
        "CmdOrCtrl+Alt+F",
        "CmdOrCtrl+Q",
        ...platformKeys,
      ]),
    );
  });

  it.each([
    ["the Mac", true],
    ["Windows and Linux", false],
  ])("share no keys with an app shortcut on %s", (_platform, mac) => {
    const clashes = Object.fromEntries(
      menuAccelerators(mac)
        .map((accelerator) => [accelerator, clashingShortcuts(accelerator, mac)] as const)
        .filter(([, shortcuts]) => shortcuts.length > 0),
    );
    expect(clashes).toEqual({});
  });

  it("would catch an app shortcut on a menu item's keys", () => {
    expect(clashingShortcuts("CmdOrCtrl+K", false)).toContain("palette.commands");
  });

  it("would catch a shortcut that waits for quick_pick_palette on a menu item's keys", () => {
    expect(clashingShortcuts("Ctrl+Tab", true)).toContain("session.next");
    expect(clashingShortcuts("Ctrl+]", true)).toContain("session.recent");
  });

  it.each([
    ["a conditional", 'accelerator: isMac ? "Cmd+K" : "Ctrl+K",'],
    ["a call", "accelerator: keysFor(item),"],
    ["the next line", "accelerator:\n  SOME_KEYS,"],
    ["a constant without a string", "accelerator: MISSING_KEYS,"],
  ])("refuse to skip an accelerator declared as %s", (_form, source) => {
    expect(() => declaredAccelerators(`{ label: "Item", ${source} }`)).toThrow(
      /as a string or a constant/,
    );
  });

  it("read an accelerator declared beside other fields and through a constant", () => {
    const source =
      'const KEYS = "CmdOrCtrl+K";\n{ label: "A", accelerator: "CmdOrCtrl+J" },\n{ accelerator: KEYS }';
    expect(declaredAccelerators(source)).toEqual(["CmdOrCtrl+J", "CmdOrCtrl+K"]);
  });
});
