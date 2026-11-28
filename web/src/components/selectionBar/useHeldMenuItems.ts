// What a selection bar's open More menu lists. While it's open nothing it lists leaves it, so a
// bar that widens, or an action that leaves the selection, can't take the focused item away.

import type { LucideIcon } from "lucide-react";
import { useMemo, useState } from "react";

/** What an open menu keeps of an action it lists, should the action leave the selection. */
export interface HeldMenuItem {
  id: string;
  label: string;
  icon: LucideIcon;
}

const heldItem = ({ id, label, icon }: HeldMenuItem): HeldMenuItem => ({ id, label, icon });

/**
 * The folded actions More lists, in the bar's order. While open it also keeps what it listed
 * when it opened or since; an item whose action has gone stays as its held copy, which callers
 * show disabled. Pass `onOpenChange` to the menu.
 */
export function useHeldMenuItems<Action extends HeldMenuItem>(
  actions: readonly Action[],
  folded: readonly Action[],
): {
  open: boolean;
  items: readonly (Action | HeldMenuItem)[];
  onOpenChange: (opening: boolean) => void;
} {
  const [held, setHeld] = useState<ReadonlyMap<string, HeldMenuItem> | null>(null);
  // While open, what folds joins what the menu holds, so widening can't take it away again.
  if (held && folded.some((action) => !held.has(action.id))) {
    setHeld(new Map([...held, ...folded.map((action) => [action.id, heldItem(action)] as const)]));
  }
  const items = useMemo((): (Action | HeldMenuItem)[] => {
    if (!held) return [...folded];
    const listed = new Set([...held.keys(), ...folded.map((action) => action.id)]);
    const current = actions.filter((action) => listed.has(action.id));
    const gone = [...held.values()].filter(
      (item) => !actions.some((action) => action.id === item.id),
    );
    return [...current, ...gone];
  }, [actions, folded, held]);
  return {
    open: held !== null,
    items,
    onOpenChange: (opening) =>
      setHeld(opening ? new Map(folded.map((action) => [action.id, heldItem(action)])) : null),
  };
}
