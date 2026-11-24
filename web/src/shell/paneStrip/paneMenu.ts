// What a pane offers its strip row and the row's menu: the split's commands for this pane,
// shared with ⌘K and the pane keys so every entry point runs the same one.

import type { PaneStripCommands, PaneStripPane } from "./PaneTitleStrip";

/** One choice in a pane menu's list, such as a Swap with Pane direction or a Move Pane spot. */
export interface PaneMenuChoice {
  id: string;
  label: string;
  /** The pane command whose shortcut hint the menu shows beside it. */
  command?: string;
  run: () => void;
}

/** A pane's own commands while its split has more than one pane. */
export interface SplitPaneCommands {
  /** A pane of the split is maximized: the menu offers Restore Panes. */
  zoomed: boolean;
  toggleZoom: () => void;
  /** Absent for a pane that can't close here, such as the agent's. */
  close?: () => void;
  /** Swap with Pane toward each direction that has a pane; none while a pane is maximized. */
  swaps?: readonly PaneMenuChoice[];
  /** Move Pane's spots, read as its submenu opens; absent while no pane can move. */
  moveOptions?: () => readonly PaneMenuChoice[];
}

/** What a session terminal's pane can do: split, and its split's commands once it has some. */
export interface SessionPaneMenu {
  onSplit: (direction: "right" | "down") => void;
  /** A shell is being created, so another split waits. */
  splitting: boolean;
  /** The pane's own commands while its split has more than one pane, read as the row renders. */
  commands?: () => SplitPaneCommands | null;
}

/**
 * A session terminal pane's row: its name, and Split Right, Split Down, Maximize or Restore and
 * Close. A lone pane that can split offers Split as ⌘K does; one that can't shows its name.
 */
export function sessionPaneStrip(
  name: string,
  paneMenu: SessionPaneMenu | undefined,
  extra: Omit<PaneStripPane, "name" | "commands"> = {},
): PaneStripPane {
  if (!paneMenu) return { name, commands: null, ...extra };
  // Maximize and Close need more than one pane, so a lone pane has neither.
  const split = paneMenu.commands?.();
  const commands: PaneStripCommands = {
    ...(paneMenu.splitting
      ? {}
      : {
          splitRight: () => paneMenu.onSplit("right"),
          splitDown: () => paneMenu.onSplit("down"),
        }),
    ...(split ? { toggleZoom: split.toggleZoom } : {}),
    ...(split?.close ? { close: split.close } : {}),
    ...(split?.swaps ? { swaps: split.swaps } : {}),
    ...(split?.moveOptions ? { moveOptions: split.moveOptions } : {}),
  };
  return { name, commands, ...extra };
}
