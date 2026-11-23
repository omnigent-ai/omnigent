import { describe, expect, it } from "vitest";

import type { ShortcutEnvironment } from "@/lib/shortcuts/registry";
import { type ShortcutPageContext, shortcutPageGroups } from "@/lib/shortcuts/shortcutPage";

import { filterShortcutPage } from "./shortcutsFilter";

const MAC: ShortcutEnvironment = { mac: true, native: false, desktopMenu: false };
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
  quickPick: false,
};
const page = shortcutPageGroups(CONTEXT, true);
const labels = (query: string) =>
  filterShortcutPage(page, query, MAC).flatMap((group) => group.items.map((row) => row.label));

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

  it("finds the row a typed chord presses, written as keys or by name", () => {
    expect(labels("⌘K")).toEqual(["Open command palette"]);
    expect(labels("cmd+k")).toEqual(["Open command palette"]);
  });

  it("finds a row the registry doesn't bind by the chord it shows, on a Mac and elsewhere", () => {
    expect(labels("cmd+shift+o")).toEqual(["Start a new session"]);
    expect(labels("⌘⇧O")).toEqual(["Start a new session"]);
    const other: ShortcutEnvironment = { mac: false, native: false, desktopMenu: false };
    const otherPage = shortcutPageGroups(CONTEXT, false);
    const otherLabels = (query: string) =>
      filterShortcutPage(otherPage, query, other).flatMap((group) =>
        group.items.map((row) => row.label),
      );
    expect(otherLabels("ctrl+shift+o")).toEqual(["Start a new session"]);
    expect(otherLabels("ctrl+k")).toEqual(["Open command palette"]);
  });

  it("finds an unbound row whose chord uses Option", () => {
    expect(labels("cmd+option+a")).toEqual(["Add nested agent"]);
    expect(labels("⌘⌥A")).toEqual(["Add nested agent"]);
  });

  it("finds a row that lists a range of digits by any chord in the range", () => {
    expect(labels("ctrl+1")).toEqual(["Select a workspace tab"]);
    expect(labels("⌃4")).toEqual(["Select a workspace tab"]);
    expect(labels("cmd+option+3")).toEqual(["Jump to a sidebar session (1–10)"]);
    const other: ShortcutEnvironment = { mac: false, native: false, desktopMenu: false };
    const otherPage = shortcutPageGroups(CONTEXT, false);
    const otherLabels = (query: string) =>
      filterShortcutPage(otherPage, query, other).flatMap((group) =>
        group.items.map((row) => row.label),
      );
    expect(otherLabels("alt+1")).toEqual(["Select a workspace tab"]);
  });

  it("finds nothing for a query no row matches", () => {
    expect(filterShortcutPage(page, "no such shortcut", MAC)).toEqual([]);
  });
});
