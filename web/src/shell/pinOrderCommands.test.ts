import { describe, expect, it } from "vitest";

import {
  PIN_ORDER_COMMAND_IDS,
  PIN_ORDER_SAVING_REASON,
  type PinOrderFacts,
  pinOrderCommands,
} from "./pinOrderCommands";

const PINNED = ["a", "b", "c", "d"];

function facts(overrides: Partial<PinOrderFacts> = {}): PinOrderFacts {
  return { pinnedIds: PINNED, sessionId: "c", reorderable: true, saving: false, ...overrides };
}

function targets(input: Partial<PinOrderFacts>) {
  return Object.fromEntries(
    pinOrderCommands(facts(input)).map((command) => [command.id, command.targetId]),
  );
}

describe("pinOrderCommands", () => {
  it("lists every move once, in menu order", () => {
    expect(pinOrderCommands(facts()).map((command) => command.id)).toEqual([
      ...PIN_ORDER_COMMAND_IDS,
    ]);
  });

  it("targets the first pin, the one above, and the one below", () => {
    expect(targets({ sessionId: "c" })).toEqual({
      "move-pin-to-top": "a",
      "move-pin-up": "b",
      "move-pin-down": "d",
    });
  });

  it("offers only the moves that would move the session", () => {
    expect(targets({ sessionId: "a" })).toEqual({
      "move-pin-to-top": null,
      "move-pin-up": null,
      "move-pin-down": "b",
    });
    // Second place: Move up already reaches the top.
    expect(targets({ sessionId: "b" })["move-pin-to-top"]).toBeNull();
    expect(targets({ sessionId: "d" })["move-pin-down"]).toBeNull();
    expect(targets({ pinnedIds: ["a"], sessionId: "a" })).toEqual({
      "move-pin-to-top": null,
      "move-pin-up": null,
      "move-pin-down": null,
    });
  });

  it("hides every move for an unpinned session or pins that aren't server-backed", () => {
    for (const input of [{ sessionId: "z" }, { reorderable: false }]) {
      for (const command of pinOrderCommands(facts(input))) {
        expect(command.availability).toEqual({ available: false });
        expect(command.targetId).toBeNull();
      }
    }
  });

  it("waits, with its reason, while a pin write saves", () => {
    for (const command of pinOrderCommands(facts({ saving: true }))) {
      expect(command.availability).toEqual({ available: false, reason: PIN_ORDER_SAVING_REASON });
    }
  });

  it("names each move by where it puts the session in Pinned", () => {
    expect(pinOrderCommands(facts()).map((command) => command.label)).toEqual([
      "Move to top of Pinned",
      "Move up in Pinned",
      "Move down in Pinned",
    ]);
  });
});
