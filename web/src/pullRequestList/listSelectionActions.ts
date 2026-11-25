// What a list's selected rows offer beyond removing them from a session: one definition per
// action, read by the selection bar and ⌘K alike, so their labels and availability can't drift.

import type { LucideIcon } from "lucide-react";

export interface ListSelectionAction {
  id: string;
  /** The selection bar's button, e.g. "Approve…" or "Remove Me as Reviewer (2 of 3)". */
  label: string;
  /**
   * The bar's label when it's short of room, without the count, e.g. "Remove Me"; `fits` then
   * shows as a badge. Without it the bar keeps `label`.
   */
  shortLabel?: string;
  /** ⌘K's row for the current selection, e.g. "Approve 3 Pull Requests…". */
  paletteLabel: string;
  icon: LucideIcon;
  keywords?: readonly string[];
  /** Why it can't act on the selection now; the bar and ⌘K show it disabled, saying so. */
  unavailableReason?: string;
  /** Opens a dialog rather than acting at once, so ⌘K moves focus to it. */
  movesFocus?: boolean;
  /** Listed in the selection bar's More menu instead of as its own button; ⌘K lists it as usual. */
  inMoreMenu?: boolean;
  /** The row menu's Open › or Copy › submenu it sits in, under `nestedLabel`, e.g. "Links". */
  menuSection?: "open" | "copy";
  nestedLabel?: string;
  /** How many of the selection it acts on, where it counts them, e.g. 2 of 3. */
  fits?: { acting: number; of: number };
  run: () => void;
}
