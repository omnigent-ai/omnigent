// What the sidebar's selection bar offers for the selected sessions, as plain facts, so ⌘K
// lists the same actions with the same availability as the bar's buttons.

import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

export const BULK_SESSION_COMMAND_IDS = [
  "mark-read",
  "mark-unread",
  "archive",
  "unarchive",
  "move-to-project",
  "delete",
] as const;
export type BulkSessionCommandId = (typeof BULK_SESSION_COMMAND_IDS)[number];

/** Counts the selection bar reads off the selected rows. */
export interface BulkSessionFacts {
  selected: number;
  /** Selected rows marked unread, which offer Mark as read instead of Mark as unread. */
  unread: number;
  /** Selected rows that can be marked unread; muted ones can't. */
  flaggable: number;
  /** Selected rows the viewer owns; archive, move and delete act on these only. */
  owned: number;
  ownedArchived: number;
  /** Every selected nested session's top session is selected too, so the family moves together. */
  canMove: boolean;
  /** An archive, delete or move is still saving. */
  busy: boolean;
}

export interface BulkSessionCommand {
  id: BulkSessionCommandId;
  label: string;
  availability: CommandAvailability;
  destructive: boolean;
}

export const BULK_BUSY_REASON = "The last change to the selection is still saving.";
export const BULK_NONE_FLAGGABLE_REASON = "Muted sessions can't be marked unread.";
export const BULK_NONE_OWNED_REASON = "You can only change sessions you own.";
export const BULK_MOVE_FAMILY_REASON = "Select a nested session's top session to move it.";

export function bulkSessionCommands(facts: BulkSessionFacts): BulkSessionCommand[] {
  const ownedUnarchived = facts.owned - facts.ownedArchived;
  // The bar swaps Archive for Unarchive when every owned row is archived.
  const allArchived = facts.owned > 0 && ownedUnarchived === 0;
  const unlessBusy = (blocked: string | null): CommandAvailability => {
    if (facts.busy) return unavailableBecause(BULK_BUSY_REASON);
    if (blocked) return unavailableBecause(blocked);
    return AVAILABLE;
  };
  const listedIf = (listed: boolean, availability: CommandAvailability) =>
    listed ? availability : HIDDEN;
  const noneOwned = facts.owned === 0 ? BULK_NONE_OWNED_REASON : null;
  return [
    {
      id: "mark-read",
      label: "Mark selected as read",
      availability: listedIf(facts.unread > 0, unlessBusy(null)),
      destructive: false,
    },
    {
      id: "mark-unread",
      label: "Mark selected as unread",
      availability: listedIf(
        facts.unread === 0,
        unlessBusy(facts.flaggable === 0 ? BULK_NONE_FLAGGABLE_REASON : null),
      ),
      destructive: false,
    },
    {
      id: "archive",
      label: "Archive selected",
      // Archive is listed only while some owned row isn't archived, so it needs no reason of its own.
      availability: listedIf(!allArchived, unlessBusy(noneOwned)),
      destructive: false,
    },
    {
      id: "unarchive",
      label: "Unarchive selected",
      availability: listedIf(allArchived, unlessBusy(null)),
      destructive: false,
    },
    {
      id: "move-to-project",
      label: "Move selected to project",
      availability: unlessBusy(noneOwned ?? (facts.canMove ? null : BULK_MOVE_FAMILY_REASON)),
      destructive: false,
    },
    {
      id: "delete",
      // Delete skips others' sessions, so it says how many it deletes when that differs.
      label:
        facts.owned > 0 && facts.owned !== facts.selected
          ? `Delete ${facts.owned} selected`
          : "Delete selected",
      availability: unlessBusy(noneOwned),
      destructive: true,
    },
  ];
}
