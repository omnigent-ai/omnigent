import { afterEach, describe, expect, it, vi } from "vitest";

import type { KeyChordInput } from "./chord";
import {
  activeShortcutChords,
  currentShortcutEnvironment,
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
      "session.new",
      "session.rename",
      "settings.open",
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
