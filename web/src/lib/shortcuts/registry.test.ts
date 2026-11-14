import { afterEach, describe, expect, it, vi } from "vitest";

import type { KeyChordInput } from "./chord";
import {
  activeShortcutChords,
  currentShortcutEnvironment,
  matchedShortcutChordIndex,
  matchesShortcut,
  SHORTCUT_BINDINGS,
  shortcutBinding,
} from "./registry";

function press(init: Partial<KeyChordInput> & { key: string }): KeyChordInput {
  return { metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...init };
}

const mac = { mac: true, native: false, desktopMenu: false };
const other = { mac: false, native: false, desktopMenu: false };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("shortcut registry", () => {
  it("looks up each binding by its id", () => {
    expect(SHORTCUT_BINDINGS.map((binding) => binding.id)).toEqual([
      "palette.commands",
      "palette.sessions",
      "shortcuts.show",
      "session.new",
      "session.rename",
      "settings.open",
      "history.back",
      "history.forward",
      "sessions.recent",
      "session.number",
      "view.mode",
      "sidebar.left",
      "sidebar.right",
      "workspace.tab",
      "terminal.new",
    ]);
    for (const binding of SHORTCUT_BINDINGS) expect(shortcutBinding(binding.id)).toBe(binding);
    expect(shortcutBinding("settings.open").label).toBe("Open Settings");
  });

  it("starts a new session with ⌘⇧O in a browser and ⌘N in a native shell", () => {
    expect(activeShortcutChords("session.new", { native: false })).toEqual([
      { mod: true, shift: true, key: "o" },
    ]);
    expect(activeShortcutChords("session.new", { native: true })).toEqual([
      { mod: true, key: "n" },
    ]);
    expect(
      matchesShortcut(press({ key: "O", metaKey: true, shiftKey: true }), "session.new", mac),
    ).toBe(true);
    expect(
      matchesShortcut(press({ key: "O", ctrlKey: true, shiftKey: true }), "session.new", other),
    ).toBe(true);
    expect(matchesShortcut(press({ key: "n", metaKey: true }), "session.new", mac)).toBe(false);
    expect(
      matchesShortcut(press({ key: "n", metaKey: true }), "session.new", { ...mac, native: true }),
    ).toBe(true);
    expect(
      matchesShortcut(press({ key: "n", ctrlKey: true }), "session.new", {
        ...other,
        native: true,
      }),
    ).toBe(true);
  });

  it("renames with the physical R key, whatever character Option types", () => {
    expect(
      matchesShortcut(
        press({ key: "®", code: "KeyR", metaKey: true, altKey: true }),
        "session.rename",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true }),
        "session.rename",
        other,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true }),
        "session.rename",
        mac,
      ),
    ).toBe(false);
  });

  it("leaves Settings' chord to the desktop menu when there is one", () => {
    const comma = press({ key: "≤", code: "Comma", metaKey: true, altKey: true });
    expect(matchesShortcut(comma, "settings.open", mac)).toBe(true);
    expect(activeShortcutChords("settings.open", { desktopMenu: true })).toEqual([]);
    expect(matchesShortcut(comma, "settings.open", { ...mac, desktopMenu: true })).toBe(false);
  });

  it("never matches AltGr, which reports as Control+Alt", () => {
    const altGraph = press({
      key: "r",
      code: "KeyR",
      ctrlKey: true,
      altKey: true,
      altGraph: true,
    });
    expect(matchesShortcut(altGraph, "session.rename", other)).toBe(false);
  });
});

describe("General and Navigation bindings", () => {
  it("opens the palette with ⌘K and session search with ⌘⌥S, by the physical S", () => {
    expect(matchesShortcut(press({ key: "K", metaKey: true }), "palette.commands", mac)).toBe(true);
    expect(matchesShortcut(press({ key: "k", ctrlKey: true }), "palette.commands", mac)).toBe(
      false,
    );
    expect(matchesShortcut(press({ key: "k", ctrlKey: true }), "palette.commands", other)).toBe(
      true,
    );
    // Only k and K themselves: not the Kelvin sign, whose lowercase is k.
    expect(matchesShortcut(press({ key: "\u212A", metaKey: true }), "palette.commands", mac)).toBe(
      false,
    );
    expect(
      matchesShortcut(press({ key: "\u212A", ctrlKey: true }), "palette.commands", other),
    ).toBe(false);
    const sessionSearch = press({ key: "ß", code: "KeyS", metaKey: true, altKey: true });
    expect(matchesShortcut(sessionSearch, "palette.sessions", mac)).toBe(true);
    expect(matchesShortcut(sessionSearch, "palette.commands", mac)).toBe(false);
  });

  it("goes back and forward with ⌘[ and ⌘] by physical key", () => {
    const back = press({ key: "[", code: "BracketLeft", ctrlKey: true });
    expect(matchesShortcut(back, "history.back", other)).toBe(true);
    expect(matchesShortcut(back, "history.forward", other)).toBe(false);
    expect(
      matchesShortcut(
        press({ key: "]", code: "BracketRight", metaKey: true }),
        "history.forward",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "{", code: "BracketLeft", metaKey: true, shiftKey: true }),
        "history.back",
        mac,
      ),
    ).toBe(false);
  });

  it("switches recent sessions with Control+Tab on every platform, with or without Shift", () => {
    for (const environment of [mac, other]) {
      expect(
        matchesShortcut(press({ key: "Tab", ctrlKey: true }), "sessions.recent", environment),
      ).toBe(true);
      expect(
        matchesShortcut(
          press({ key: "Tab", ctrlKey: true, shiftKey: true }),
          "sessions.recent",
          environment,
        ),
      ).toBe(true);
    }
    expect(matchesShortcut(press({ key: "Tab", metaKey: true }), "sessions.recent", mac)).toBe(
      false,
    );
    for (const key of ["tab", "TAB"]) {
      expect(matchesShortcut(press({ key, ctrlKey: true }), "sessions.recent", other)).toBe(false);
    }
  });

  it("numbers sidebar sessions ⌘1–⌘9 then ⌘0, adding Alt in a browser", () => {
    const native = { ...mac, native: true };
    expect(
      matchedShortcutChordIndex(press({ key: "1", metaKey: true }), "session.number", native),
    ).toBe(0);
    expect(
      matchedShortcutChordIndex(press({ key: "0", metaKey: true }), "session.number", native),
    ).toBe(9);
    expect(
      matchedShortcutChordIndex(press({ key: "1", metaKey: true }), "session.number", mac),
    ).toBe(-1);
    expect(
      matchedShortcutChordIndex(
        press({ key: "¡", code: "Digit1", metaKey: true, altKey: true }),
        "session.number",
        mac,
      ),
    ).toBe(0);
    expect(
      matchedShortcutChordIndex(
        press({ key: "º", code: "Digit0", ctrlKey: true, altKey: true, altGraph: true }),
        "session.number",
        other,
      ),
    ).toBe(-1);
  });

  it("keeps firing under AltGr for the chords that never checked for it", () => {
    const altGraph = { altGraph: true };
    expect(
      matchesShortcut(press({ key: "/", ctrlKey: true, ...altGraph }), "shortcuts.show", other),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "[", code: "BracketLeft", ctrlKey: true, ...altGraph }),
        "history.back",
        other,
      ),
    ).toBe(true);
    expect(
      matchedShortcutChordIndex(press({ key: "2", ctrlKey: true, ...altGraph }), "session.number", {
        ...other,
        native: true,
      }),
    ).toBe(1);
    expect(
      matchesShortcut(press({ key: "k", ctrlKey: true, ...altGraph }), "palette.commands", other),
    ).toBe(false);
    expect(
      matchesShortcut(press({ key: "Tab", ctrlKey: true, ...altGraph }), "sessions.recent", other),
    ).toBe(false);
  });
});

describe("View bindings", () => {
  it("toggles Chat / Terminal with ⌘⌥\\ by physical key", () => {
    const toggle = press({ key: "«", code: "Backslash", metaKey: true, altKey: true });
    expect(matchesShortcut(toggle, "view.mode", mac)).toBe(true);
    expect(matchesShortcut({ ...toggle, shiftKey: true }, "view.mode", mac)).toBe(false);
  });

  it("toggles the sidebars with ⌘B / ⇧⌘B natively and ⌘⌥[ / ⌘⌥] in a browser", () => {
    const native = { ...mac, native: true };
    expect(matchesShortcut(press({ key: "b", metaKey: true }), "sidebar.left", native)).toBe(true);
    expect(
      matchesShortcut(press({ key: "B", metaKey: true, shiftKey: true }), "sidebar.right", native),
    ).toBe(true);
    expect(matchesShortcut(press({ key: "b", metaKey: true }), "sidebar.left", mac)).toBe(false);
    const left = press({ key: "“", code: "BracketLeft", metaKey: true, altKey: true });
    expect(matchesShortcut(left, "sidebar.left", mac)).toBe(true);
    expect(matchesShortcut(left, "sidebar.left", native)).toBe(false);
    expect(
      matchesShortcut(
        press({ key: "]", code: "BracketRight", ctrlKey: true, altKey: true }),
        "sidebar.right",
        other,
      ),
    ).toBe(true);
  });

  it("picks workspace tabs 1–9 with Control on macOS and Alt elsewhere", () => {
    const control = press({ key: "1", code: "Digit1", ctrlKey: true });
    const alt = press({ key: "¡", code: "Digit1", altKey: true });
    expect(matchedShortcutChordIndex(control, "workspace.tab", mac)).toBe(0);
    expect(matchedShortcutChordIndex(alt, "workspace.tab", mac)).toBe(-1);
    expect(matchedShortcutChordIndex(alt, "workspace.tab", other)).toBe(0);
    expect(matchedShortcutChordIndex(control, "workspace.tab", other)).toBe(-1);
    expect(
      matchedShortcutChordIndex(
        press({ key: "9", code: "Digit9", ctrlKey: true }),
        "workspace.tab",
        mac,
      ),
    ).toBe(8);
    expect(
      matchedShortcutChordIndex(
        press({ key: "0", code: "Digit0", ctrlKey: true }),
        "workspace.tab",
        mac,
      ),
    ).toBe(-1);
  });

  it("opens a new shell with literal Control+` on every platform, even under AltGr", () => {
    const shell = press({ key: "`", code: "Backquote", ctrlKey: true });
    expect(matchesShortcut(shell, "terminal.new", mac)).toBe(true);
    expect(matchesShortcut(shell, "terminal.new", other)).toBe(true);
    expect(matchesShortcut({ ...shell, altGraph: true }, "terminal.new", other)).toBe(true);
    expect(
      matchesShortcut(press({ key: "`", code: "Backquote", metaKey: true }), "terminal.new", mac),
    ).toBe(false);
  });
});

describe("currentShortcutEnvironment", () => {
  it("prefers overrides and reads the platform only when asked", () => {
    const platform = vi.fn(() => "MacIntel");
    vi.stubGlobal("navigator", {
      get platform() {
        return platform();
      },
    });
    const environment = currentShortcutEnvironment({ native: true });
    expect(platform).not.toHaveBeenCalled();
    expect(environment.native).toBe(true);
    expect(environment.mac).toBe(true);
    expect(environment.mac).toBe(true);
    expect(platform).toHaveBeenCalledTimes(1);
    // A resolved environment passes through unchanged.
    expect(currentShortcutEnvironment(environment)).toBe(environment);
  });
});
