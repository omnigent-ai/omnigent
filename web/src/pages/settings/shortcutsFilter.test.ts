import { describe, expect, it } from "vitest";

import type { ShortcutEnvironment } from "@/lib/shortcuts/registry";
import { type ShortcutPageContext, shortcutPageGroups } from "@/lib/shortcuts/shortcutPage";

import { filterShortcutPage } from "./shortcutsFilter";

const MAC: ShortcutEnvironment = {
  mac: true,
  native: false,
  desktopMenu: false,
  goMenu: false,
  submitWithModEnter: false,
};
const CONTEXT: ShortcutPageContext = {
  native: false,
  electron: false,
  browser: true,
  submitWithModEnter: false,
  preventsKeyboardSubmit: false,
  contentSearch: true,
  grids: true,
  splitTerminal: true,
  siblingSessions: true,
  desktopCommands: false,
  alwaysSteer: false,
};
const OTHER: ShortcutEnvironment = { ...MAC, mac: false };
const page = shortcutPageGroups(CONTEXT, true);
const otherPage = shortcutPageGroups(CONTEXT, false);
const labelsIn = (groups: typeof page, environment: ShortcutEnvironment) => (query: string) =>
  filterShortcutPage(groups, query, environment).flatMap((group) =>
    group.items.map((row) => row.label),
  );
const labels = labelsIn(page, MAC);
const otherLabels = labelsIn(otherPage, OTHER);

describe("filterShortcutPage", () => {
  it("lists every shortcut for an empty query", () => {
    expect(filterShortcutPage(page, "  ", MAC)).toEqual(page);
  });

  it("finds rows by name, needing every word, in any case", () => {
    expect(labels("model")).toEqual(["Open model picker", "Cycle model"]);
    expect(labels("Cycle MODEL")).toEqual(["Cycle model"]);
  });

  it("finds a group's rows by its title, and leaves out groups with no match", () => {
    const groups = filterShortcutPage(page, "navigation", MAC);
    expect(groups.map((group) => group.title)).toEqual(["Navigation"]);
    expect(groups[0].items).toEqual(page.find((group) => group.title === "Navigation")?.items);
  });

  it("finds rows by the keys the page shows", () => {
    expect(labels("esc")).toEqual(
      expect.arrayContaining([
        "Decline approval prompt",
        "Cancel pending question",
        "Stop response",
      ]),
    );
    expect(labels("esc")).not.toContain("Send message");
  });

  it("finds the row a typed chord presses, written as keys or by name, on a Mac and elsewhere", () => {
    const cases: [query: string, mac: boolean, label: string][] = [
      ["⌘K", true, "Open command palette"],
      ["cmd+k", true, "Open command palette"],
      ["ctrl+k", false, "Open command palette"],
      ["cmd+shift+o", true, "Start a new session"],
      ["⌘⇧O", true, "Start a new session"],
      ["ctrl+shift+o", false, "Start a new session"],
      // Option, by name or symbol.
      ["cmd+option+a", true, "Add nested agent"],
      ["⌘⌥A", true, "Add nested agent"],
    ];
    for (const [query, mac, label] of cases) {
      expect((mac ? labels : otherLabels)(query), query).toEqual([label]);
    }
  });

  it("finds a row that lists a range of digits by any chord in the range", () => {
    expect(labels("ctrl+1")).toEqual(["Select a workspace tab"]);
    expect(labels("⌃4")).toEqual(["Select a workspace tab"]);
    expect(labels("cmd+option+3")).toEqual(["Jump to a sidebar session (1–9)"]);
    expect(otherLabels("alt+1")).toEqual(["Select a workspace tab"]);
  });

  it("finds nothing for a query no row matches", () => {
    expect(filterShortcutPage(page, "no such shortcut", MAC)).toEqual([]);
  });
});
