import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type ShortcutPageContext,
  type ShortcutPageGroup,
  shortcutPageGroups,
} from "./shortcutPage";
import {
  GOLDEN_GROUP_FLAGS,
  GOLDEN_GROUPS,
  GOLDEN_ROWS,
  type GoldenRow,
} from "./shortcutPage.golden";

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

describe("shortcutPageGroups against the shortcut catalog", () => {
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

/** The recorded groups for `context`, assembled as the page would list them. */
function goldenGroupsFor(context: ShortcutPageContext, mac: boolean): ShortcutPageGroup[] {
  const platform = mac ? 0 : 1;
  const caps = (keys: GoldenRow["keys"]) => keys[platform].split(" ");
  return Object.entries(GOLDEN_GROUP_FLAGS).flatMap(([title, flags]) => {
    const on = flags.filter((flag) => context[flag]);
    const key = `${title}: ${on.length > 0 ? on.join("+") : "-"}`;
    expect(key in GOLDEN_GROUPS, key).toBe(true);
    const group = GOLDEN_GROUPS[key];
    if (!group) return [];
    return [
      {
        title: group.title,
        ...("note" in group ? { note: group.note } : {}),
        items: group.rows.map((index) => {
          const row = GOLDEN_ROWS[index]!;
          return {
            label: row.label,
            keys: caps(row.keys),
            ...("alternateKeys" in row
              ? { alternateKeys: row.alternateKeys && caps(row.alternateKeys) }
              : {}),
            ...("desktopOnly" in row ? { desktopOnly: row.desktopOnly } : {}),
          };
        }),
      },
    ];
  });
}

describe("shortcutPageGroups against the recorded rows", () => {
  it.each([
    { platform: "macOS", mac: true },
    { platform: "other platforms", mac: false },
  ])("lists exactly the recorded rows in every context on $platform", ({ mac }) => {
    for (const context of everyContext()) {
      expect(shortcutPageGroups(context, mac), JSON.stringify(context)).toStrictEqual(
        goldenGroupsFor(context, mac),
      );
    }
  });
});

// A plain browser tab with no optional server features.
const BROWSER_TAB: ShortcutPageContext = {
  native: false,
  electron: false,
  browser: false,
  submitWithModEnter: false,
  preventsKeyboardSubmit: false,
  contentSearch: false,
  grids: false,
  splitTerminal: false,
  siblingSessions: false,
  desktopCommands: false,
};

function labelsIn(context: ShortcutPageContext, title: string): string[] {
  const group = shortcutPageGroups(context).find((candidate) => candidate.title === title);
  return group?.items.map((item) => item.label) ?? [];
}

describe("shortcutPageGroups", () => {
  it("drops groups left with no rows", () => {
    expect(shortcutPageGroups(BROWSER_TAB).map((group) => group.title)).toEqual([
      "General",
      "In chats",
      "Navigation",
      "View",
      "Slash commands",
    ]);
    expect(
      shortcutPageGroups({ ...BROWSER_TAB, grids: true }).map((group) => group.title),
    ).toContain("Panes");
  });

  it("adds desktop menu rows only when the shell hands commands to the page", () => {
    const desktop = { ...BROWSER_TAB, native: true, electron: true, desktopCommands: true };
    expect(labelsIn(BROWSER_TAB, "General")).not.toContain("Quit Omnigent");
    expect(labelsIn(desktop, "General")).toContain("Quit Omnigent");
    expect(labelsIn(desktop, "View")).toContain("Reopen the last closed tab");
    expect(labelsIn(desktop, "View")).not.toContain("Reload the browser tab");
    expect(labelsIn({ ...desktop, browser: true }, "View")).toEqual(
      expect.arrayContaining(["Open a new browser tab", "Reload the browser tab"]),
    );
  });

  it("hides send and desktop-only rows where Enter inserts a newline", () => {
    const touch = labelsIn({ ...BROWSER_TAB, preventsKeyboardSubmit: true }, "In chats");
    expect(touch).not.toContain("Send message");
    expect(touch).not.toContain("Pick a question option by number");
    expect(touch).toContain("Stop response");
    expect(labelsIn(BROWSER_TAB, "In chats").slice(0, 1)).toEqual(["Send message"]);
  });

  it("widens the workspace tab digits when content search adds a tab", () => {
    const tabRow = (context: ShortcutPageContext) =>
      shortcutPageGroups(context)
        .find((group) => group.title === "View")
        ?.items.find((item) => item.label === "Select a workspace tab");
    expect(tabRow(BROWSER_TAB)?.keys).toContain("1…4");
    expect(tabRow({ ...BROWSER_TAB, contentSearch: true })?.keys).toContain("1…5");
    expect(labelsIn({ ...BROWSER_TAB, contentSearch: true }, "View")).toContain(
      "Search workspace contents",
    );
  });
});
