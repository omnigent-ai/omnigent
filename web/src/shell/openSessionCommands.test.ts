import { describe, expect, it } from "vitest";

import { AVAILABLE, unavailableBecause } from "@/store/paletteCommands";

import { type OpenSessionFacts, openSessionCommands } from "./openSessionCommands";

const OWNER_FACTS: OpenSessionFacts = {
  canAddAgent: true,
  pinnable: true,
  pinned: false,
  pinSaving: false,
  canShare: true,
  canFork: true,
  canMarkUnread: true,
  unread: false,
  canCreateAutomation: true,
  archived: false,
  desktopShortcuts: false,
};

const ids = (facts: OpenSessionFacts) => openSessionCommands(facts).map((command) => command.id);

describe("openSessionCommands", () => {
  it("lists the header menu's actions in its order", () => {
    expect(openSessionCommands(OWNER_FACTS).map(({ id, label }) => [id, label])).toEqual([
      ["add-nested-agent", "Add nested agent"],
      ["pin", "Pin session"],
      ["share", "Share session"],
      ["fork", "Fork session"],
      ["export", "Export session"],
      ["rename", "Rename session"],
      ["mark-unread", "Mark session as unread"],
      ["create-automation", "Create automation from session"],
      ["archive", "Archive session"],
      ["delete", "Delete session"],
    ]);
  });

  it("leaves out what the menu hides", () => {
    expect(
      ids({
        ...OWNER_FACTS,
        canAddAgent: false,
        pinnable: false,
        canShare: false,
        canFork: false,
        canMarkUnread: false,
        canCreateAutomation: false,
      }),
    ).toEqual(["export", "rename", "archive", "delete"]);
  });

  it("offers Mark as read in Mark as unread's place on a session marked unread", () => {
    // ⌘⌥U marks such a session read, so ⌘K offers the same, muted or not.
    for (const canMarkUnread of [true, false]) {
      const commands = openSessionCommands({ ...OWNER_FACTS, unread: true, canMarkUnread });
      const commandIds = commands.map((command) => command.id);
      expect(commandIds).toContain("mark-read");
      expect(commandIds).not.toContain("mark-unread");
      expect(commands.find((command) => command.id === "mark-read")).toMatchObject({
        label: "Mark session as read",
        availability: AVAILABLE,
      });
    }
  });

  it("offers the opposite toggle for a pinned or archived session", () => {
    const commands = openSessionCommands({ ...OWNER_FACTS, pinned: true, archived: true });
    expect(commands.map((command) => command.label)).toEqual(
      expect.arrayContaining(["Unpin session", "Unarchive session"]),
    );
    expect(commands.map((command) => command.id)).not.toContain("pin");
    expect(commands.map((command) => command.id)).not.toContain("archive");
  });

  it("keeps disabled actions listed with the reason they can't run", () => {
    const byId = new Map(
      openSessionCommands({
        ...OWNER_FACTS,
        pinSaving: true,
        shareDisabledReason: "Sharing is turned off on this server.",
        forkDisabledReason: "Forking this session isn't supported yet.",
      }).map((command) => [command.id, command.availability]),
    );
    expect(byId.get("pin")).toEqual(unavailableBecause("A pin change is still saving."));
    expect(byId.get("share")).toEqual(unavailableBecause("Sharing is turned off on this server."));
    expect(byId.get("fork")).toEqual(
      unavailableBecause("Forking this session isn't supported yet."),
    );
    expect(byId.get("rename")).toEqual(AVAILABLE);
  });

  it("shows the desktop menu bar's chords only where the desktop binds them", () => {
    const chords = (desktopShortcuts: boolean) =>
      openSessionCommands({ ...OWNER_FACTS, desktopShortcuts })
        .filter((command) => command.nativeShortcut)
        .map((command) => command.id);
    expect(chords(false)).toEqual([]);
    expect(chords(true)).toEqual(["pin", "fork", "archive"]);
  });

  it("marks only Delete as destructive", () => {
    expect(
      openSessionCommands(OWNER_FACTS)
        .filter((command) => command.destructive)
        .map((command) => command.id),
    ).toEqual(["delete"]);
  });
});
