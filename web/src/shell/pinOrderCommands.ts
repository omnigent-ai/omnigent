// What the row menu and ⌘K offer for moving a pinned session within Pinned. Pure:
// both surfaces render from `pinOrderCommands`, so they can't drift.

import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

export const PIN_ORDER_COMMAND_IDS = ["move-pin-to-top", "move-pin-up", "move-pin-down"] as const;

export type PinOrderCommandId = (typeof PIN_ORDER_COMMAND_IDS)[number];

export interface PinOrderFacts {
  /** Pinned sessions in the order Pinned shows them. */
  pinnedIds: readonly string[];
  sessionId: string;
  /** Pins are stored on the server, which is where their order is kept. */
  reorderable: boolean;
  /** A pin write, including a reorder, is still saving. */
  saving: boolean;
}

export interface PinOrderCommand {
  id: PinOrderCommandId;
  /** The menu row's name, also its ⌘K label. */
  label: string;
  availability: CommandAvailability;
  /** The pinned session whose place the moved one takes; null when hidden. */
  targetId: string | null;
}

export const PIN_ORDER_SAVING_REASON = "Wait for the pinned order to finish saving.";

const LABELS: Record<PinOrderCommandId, string> = {
  "move-pin-to-top": "Move to top of Pinned",
  "move-pin-up": "Move up in Pinned",
  "move-pin-down": "Move down in Pinned",
};

function targetFor(id: PinOrderCommandId, pinnedIds: readonly string[], index: number) {
  switch (id) {
    // Second place is covered by Move up, so Move to top starts at third.
    case "move-pin-to-top":
      return index > 1 ? pinnedIds[0] : null;
    case "move-pin-up":
      return index > 0 ? pinnedIds[index - 1] : null;
    case "move-pin-down":
      return index >= 0 && index < pinnedIds.length - 1 ? pinnedIds[index + 1] : null;
  }
}

/**
 * Each move in display order. A move is offered only for a pinned session it would
 * move, and only while pins are server-backed; it waits while a pin write saves.
 */
export function pinOrderCommands(facts: PinOrderFacts): PinOrderCommand[] {
  const index = facts.reorderable ? facts.pinnedIds.indexOf(facts.sessionId) : -1;
  return PIN_ORDER_COMMAND_IDS.map((id) => {
    const targetId = index < 0 ? null : (targetFor(id, facts.pinnedIds, index) ?? null);
    const availability =
      targetId === null
        ? HIDDEN
        : facts.saving
          ? unavailableBecause(PIN_ORDER_SAVING_REASON)
          : AVAILABLE;
    return { id, label: LABELS[id], availability, targetId };
  });
}
