import { describe, expect, it } from "vitest";

import { PANE_COMMANDS, paneCommandLabel, paneMenuEntries } from "./paneCommandTable";

describe("paneCommandTable", () => {
  it("names each command once", () => {
    const ids = PANE_COMMANDS.map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(paneCommandLabel("terminal.movePane")).toBe("Move Pane…");
  });

  it("lists a menu's commands in order, with one separator between groups", () => {
    const entries = paneMenuEntries("pane");
    expect(entries.map((item) => (item.kind === "command" ? item.entry.id : "|"))).toEqual([
      "clear-scrollback",
      "|",
      "terminal.splitRight",
      "terminal.splitDown",
      "terminal.toggleZoom",
      "terminal.equalize",
      "|",
      "close",
    ]);
  });

  it("never leaves a leading, trailing or doubled separator when commands are left out", () => {
    const ids = (offered: Parameters<typeof paneMenuEntries>[1]) =>
      paneMenuEntries("context", offered).map((item) =>
        item.kind === "command" ? item.entry.id : "|",
      );
    // A whole middle group gone: the groups either side keep one separator between them.
    expect(ids((entry) => entry.group !== "buffer")).toEqual([
      "copy",
      "paste",
      "|",
      "terminal.splitRight",
      "terminal.splitDown",
      "terminal.toggleZoom",
      "|",
      "close",
    ]);
    // The first and last groups gone: no separator leads or trails.
    expect(ids((entry) => entry.group === "buffer" || entry.group === "panes")).toEqual([
      "find",
      "clear-scrollback",
      "|",
      "terminal.splitRight",
      "terminal.splitDown",
      "terminal.toggleZoom",
    ]);
  });
});
