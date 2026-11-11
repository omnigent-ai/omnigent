import { afterEach, describe, expect, it, vi } from "vitest";

import { type ShortcutPageContext, shortcutPageGroups } from "./shortcutPage";

const CONTEXT_FLAGS = [
  "native",
  "electron",
  "browser",
  "submitWithModEnter",
  "preventsKeyboardSubmit",
  "contentSearch",
  "grids",
  "splitTerminal",
  "siblingSessions",
  "desktopCommands",
] as const satisfies readonly (keyof ShortcutPageContext)[];

/** Every combination of the context flags: 2^10 contexts. */
function everyContext(): ShortcutPageContext[] {
  return Array.from({ length: 1 << CONTEXT_FLAGS.length }, (_, bits) =>
    Object.fromEntries(CONTEXT_FLAGS.map((flag, index) => [flag, (bits & (1 << index)) !== 0])),
  ) as unknown as ShortcutPageContext[];
}

// The catalog's glyphs are fixed when it loads, so each platform loads it afresh.
async function loadCatalog(mac: boolean) {
  vi.resetModules();
  vi.stubGlobal("navigator", { platform: mac ? "MacIntel" : "Win32", userAgent: "" });
  return import("@/lib/shortcutCatalog");
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("shortcutPageGroups", () => {
  it.each([
    { platform: "macOS", mac: true },
    { platform: "other platforms", mac: false },
  ])("lists exactly the shortcut catalog's rows in every context on $platform", async ({ mac }) => {
    const { shortcutGroupsFor } = await loadCatalog(mac);
    const contexts = everyContext();
    expect(contexts).toHaveLength(1024);
    // The catalog loaded with this platform's glyphs.
    expect(shortcutGroupsFor(contexts[0]!)[0]!.items[0]!.keys[0]).toBe(mac ? "⌘" : "Ctrl");
    for (const context of contexts) {
      expect(shortcutPageGroups(context, mac), JSON.stringify(context)).toStrictEqual(
        shortcutGroupsFor(context),
      );
    }
  });
});
