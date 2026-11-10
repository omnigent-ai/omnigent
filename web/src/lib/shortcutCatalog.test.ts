import { describe, expect, it } from "vitest";

import { shortcutGroupsFor, type ShortcutCatalogContext } from "@/lib/shortcutCatalog";

// A plain browser tab with no optional server features.
const BROWSER_TAB: ShortcutCatalogContext = {
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

function labelsIn(context: ShortcutCatalogContext, title: string): string[] {
  const group = shortcutGroupsFor(context).find((candidate) => candidate.title === title);
  return group?.items.map((item) => item.label) ?? [];
}

describe("shortcutGroupsFor", () => {
  it("drops groups left with no rows", () => {
    expect(shortcutGroupsFor(BROWSER_TAB).map((group) => group.title)).toEqual([
      "General",
      "In chats",
      "Navigation",
      "View",
      "Slash commands",
    ]);
    expect(
      shortcutGroupsFor({ ...BROWSER_TAB, grids: true }).map((group) => group.title),
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
    const tabRow = (context: ShortcutCatalogContext) =>
      shortcutGroupsFor(context)
        .find((group) => group.title === "View")
        ?.items.find((item) => item.label === "Select a workspace tab");
    expect(tabRow(BROWSER_TAB)?.keys).toContain("1…4");
    expect(tabRow({ ...BROWSER_TAB, contentSearch: true })?.keys).toContain("1…5");
    expect(labelsIn({ ...BROWSER_TAB, contentSearch: true }, "View")).toContain(
      "Search workspace contents",
    );
  });
});
