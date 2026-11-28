import { describe, expect, it } from "vitest";

import { isCommandAvailable, isCommandListed } from "@/store/paletteCommands";

import {
  BULK_BUSY_REASON,
  BULK_MOVE_FAMILY_REASON,
  BULK_NONE_FLAGGABLE_REASON,
  BULK_NONE_OWNED_REASON,
  type BulkSessionCommandId,
  type BulkSessionFacts,
  bulkSessionCommands,
} from "./bulkModel";

const FACTS: BulkSessionFacts = {
  selected: 2,
  unread: 0,
  flaggable: 2,
  owned: 2,
  ownedArchived: 0,
  canMove: true,
  busy: false,
};

function commandsFor(overrides: Partial<BulkSessionFacts>) {
  const commands = bulkSessionCommands({ ...FACTS, ...overrides });
  const find = (id: BulkSessionCommandId) => commands.find((command) => command.id === id)!;
  return {
    listed: commands.filter((command) => isCommandListed(command.availability)).map(({ id }) => id),
    available: (id: BulkSessionCommandId) => isCommandAvailable(find(id).availability),
    reason: (id: BulkSessionCommandId) => {
      const { availability } = find(id);
      return availability.available ? undefined : availability.reason;
    },
    label: (id: BulkSessionCommandId) => find(id).label,
  };
}

describe("bulkSessionCommands", () => {
  it("offers Mark as unread, Archive, Move and Delete for owned, read sessions", () => {
    const commands = commandsFor({});
    expect(commands.listed).toEqual(["mark-unread", "archive", "move-to-project", "delete"]);
    expect(commands.label("delete")).toBe("Delete selected");
  });

  it("offers Mark as read in place of Mark as unread once a selected session is unread", () => {
    expect(commandsFor({ unread: 1 }).listed).toContain("mark-read");
    expect(commandsFor({ unread: 1 }).listed).not.toContain("mark-unread");
  });

  it("greys Mark as unread when every selected session is muted", () => {
    expect(commandsFor({ flaggable: 0 }).reason("mark-unread")).toBe(BULK_NONE_FLAGGABLE_REASON);
  });

  it("swaps Archive for Unarchive only when every owned session is archived", () => {
    expect(commandsFor({ ownedArchived: 1 }).listed).toContain("archive");
    expect(commandsFor({ ownedArchived: 1 }).listed).not.toContain("unarchive");
    const archived = commandsFor({ ownedArchived: 2 });
    expect(archived.listed).toContain("unarchive");
    expect(archived.listed).not.toContain("archive");
  });

  it("counts only owned sessions in Delete, and greys owner-only actions when none are owned", () => {
    expect(commandsFor({ selected: 3 }).label("delete")).toBe("Delete 2 selected");
    const shared = commandsFor({ owned: 0 });
    for (const id of ["archive", "move-to-project", "delete"] as const) {
      expect(shared.reason(id)).toBe(BULK_NONE_OWNED_REASON);
    }
    expect(shared.available("mark-unread")).toBe(true);
  });

  it("greys Move while a nested session's top session isn't selected", () => {
    expect(commandsFor({ canMove: false }).reason("move-to-project")).toBe(BULK_MOVE_FAMILY_REASON);
  });

  it("greys every action while a change is saving", () => {
    const busy = commandsFor({ busy: true });
    for (const id of busy.listed) expect(busy.reason(id)).toBe(BULK_BUSY_REASON);
  });
});
