import { afterEach, describe, expect, it, vi } from "vitest";

import { type ShortcutPageContext, shortcutPageGroups } from "./shortcutPage";

// Typed so that a new ShortcutPageContext field must be listed here.
const CONTEXT_FLAGS = Object.keys({
  native: true,
  electron: true,
  browser: true,
  submitWithModEnter: true,
  preventsKeyboardSubmit: true,
  contentSearch: true,
  grids: true,
  splitTerminal: true,
  siblingSessions: true,
  desktopCommands: true,
} satisfies Record<keyof ShortcutPageContext, true>) as (keyof ShortcutPageContext)[];

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
    // Fails if the stubbed navigator never reached the catalog's glyphs.
    const general = shortcutGroupsFor(contexts[0]!).find((group) => group.title === "General");
    const newSession = general?.items.find((item) => item.label === "Start a new session");
    expect(newSession?.keys[0]).toBe(mac ? "⌘" : "Ctrl");
    for (const context of contexts) {
      expect(shortcutPageGroups(context, mac), JSON.stringify(context)).toStrictEqual(
        shortcutGroupsFor(context),
      );
    }
  });
});
