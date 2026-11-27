// Moving the open pinned session within Pinned from ⌘K: the keyboard path to what
// dragging a pin does, through the same order writes.

import { ArrowDownIcon, ArrowUpIcon, ArrowUpToLineIcon, type LucideIcon } from "lucide-react";
import { useMemo, useRef } from "react";

import type { PaletteCommand } from "@/store/paletteCommands";

import {
  currentPinMoveTarget,
  type PinOrderCommand,
  type PinOrderCommandId,
  pinOrderCommands,
} from "./pinOrderCommands";
import { useQuickPickPaletteCommands } from "./screenCommands/useQuickPickPaletteCommands";

/** The Pinned section's order and the move that rewrites it, from the sidebar list. */
export interface PinOrder {
  pinnedIds: readonly string[];
  /** Pins are stored on the server, which keeps their order. */
  reorderable: boolean;
  saving: boolean;
  /** Puts `sessionId` in `targetId`'s place, as a drop on that pin does. */
  move: (sessionId: string, targetId: string) => void;
}

const ICONS: Record<PinOrderCommandId, LucideIcon> = {
  "move-pin-to-top": ArrowUpToLineIcon,
  "move-pin-up": ArrowUpIcon,
  "move-pin-down": ArrowDownIcon,
};

const KEYWORDS: Record<PinOrderCommandId, string[]> = {
  "move-pin-to-top": ["pin", "order", "reorder", "first"],
  "move-pin-up": ["pin", "order", "reorder", "raise"],
  "move-pin-down": ["pin", "order", "reorder", "lower"],
};

/** ⌘K's rows for `commands`, each moving `sessionId` as the order is when it runs. */
export function pinOrderPaletteCommands(
  commands: readonly PinOrderCommand[],
  sessionId: string,
  run: (command: PinOrderCommand) => void,
): PaletteCommand[] {
  return commands.map((command) => ({
    // The target is part of the id, so ⌘K drops a row once the order changes its target.
    id: `sidebar.pin-order.${command.id}:${sessionId}:${command.targetId}`,
    label: command.label,
    icon: ICONS[command.id],
    keywords: KEYWORDS[command.id],
    availability: command.availability,
    run: () => run(command),
  }));
}

const NO_COMMANDS: PaletteCommand[] = [];

/** Offers the open session's moves in ⌘K while it's pinned. */
export function usePinOrderPaletteCommands(order: PinOrder, sessionId: string | null): void {
  const { pinnedIds, reorderable, saving } = order;
  const commands = useMemo(
    () => (sessionId ? pinOrderCommands({ pinnedIds, sessionId, reorderable, saving }) : []),
    [pinnedIds, sessionId, reorderable, saving],
  );
  const latest = useRef(order);
  latest.current = order;
  // Rebuilt only when a move or its availability changes, not on every pinned refetch.
  const signature = commands
    .map((command) => JSON.stringify([command.id, command.targetId, command.availability]))
    .join("|");
  const paletteCommands = useMemo(
    () =>
      sessionId
        ? pinOrderPaletteCommands(commands, sessionId, (command) => {
            // Read against the order now, so a row kept past a change does nothing.
            const current = latest.current;
            const targetId = currentPinMoveTarget(command, { ...current, sessionId });
            if (targetId) current.move(sessionId, targetId);
          })
        : NO_COMMANDS,
    // `signature` stands for `commands`.
    // oxlint-disable-next-line react-hooks/exhaustive-deps
    [signature, sessionId],
  );
  useQuickPickPaletteCommands("sidebar-pin-order", "Session list", paletteCommands);
}
