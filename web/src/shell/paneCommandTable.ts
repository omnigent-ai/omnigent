// The commands a split's pane offers, in order and grouped: its right-click menu (`context`),
// its title strip's ⋯ menu (`pane`) and ⌘K (`palette`). Every entry point reads this table, so
// their labels and order can't drift.

export type PaneMenu = "context" | "pane" | "palette";

export interface PaneCommandEntry {
  readonly id: string;
  readonly label: string;
  /** The label while the command's state is on, such as Restore Panes while one is maximized. */
  readonly checkedLabel?: string;
  /** Entries in different groups get a separator between them. */
  readonly group: string;
  readonly menus: readonly PaneMenu[];
}

const PANE_COMMAND_DEFINITIONS = [
  { id: "copy", label: "Copy", group: "edit", menus: ["context", "palette"] },
  { id: "paste", label: "Paste", group: "edit", menus: ["context", "palette"] },
  { id: "find", label: "Find…", group: "buffer", menus: ["context", "palette"] },
  {
    id: "clear-scrollback",
    label: "Clear Scrollback",
    group: "buffer",
    menus: ["context", "pane"],
  },
  {
    id: "terminal.splitRight",
    label: "Split Right",
    group: "panes",
    menus: ["context", "pane", "palette"],
  },
  {
    id: "terminal.splitDown",
    label: "Split Down",
    group: "panes",
    menus: ["context", "pane", "palette"],
  },
  {
    id: "terminal.toggleZoom",
    label: "Maximize Pane",
    checkedLabel: "Restore Panes",
    group: "panes",
    menus: ["context", "pane"],
  },
  { id: "terminal.equalize", label: "Equalize Panes", group: "panes", menus: ["pane", "palette"] },
  // ⌘K's, for the pane you're in: it lists where the pane can go.
  { id: "terminal.movePane", label: "Move Pane…", group: "panes", menus: ["palette"] },
  { id: "terminal.swapLeft", label: "Swap with Pane Left", group: "panes", menus: ["palette"] },
  { id: "terminal.swapRight", label: "Swap with Pane Right", group: "panes", menus: ["palette"] },
  { id: "terminal.swapUp", label: "Swap with Pane Above", group: "panes", menus: ["palette"] },
  { id: "terminal.swapDown", label: "Swap with Pane Below", group: "panes", menus: ["palette"] },
  { id: "terminal.focusLeft", label: "Focus Pane Left", group: "focus", menus: ["palette"] },
  { id: "terminal.focusRight", label: "Focus Pane Right", group: "focus", menus: ["palette"] },
  { id: "terminal.focusUp", label: "Focus Pane Above", group: "focus", menus: ["palette"] },
  { id: "terminal.focusDown", label: "Focus Pane Below", group: "focus", menus: ["palette"] },
  { id: "close", label: "Close Pane", group: "terminal", menus: ["context", "pane", "palette"] },
] as const satisfies readonly PaneCommandEntry[];

export type PaneCommandId = (typeof PANE_COMMAND_DEFINITIONS)[number]["id"];

export const PANE_COMMANDS: readonly PaneCommandEntry[] = Object.freeze(
  PANE_COMMAND_DEFINITIONS.map((entry) =>
    Object.freeze({ ...entry, menus: Object.freeze([...entry.menus]) }),
  ),
);

const LABELS = Object.fromEntries(
  PANE_COMMAND_DEFINITIONS.map((entry) => [entry.id, entry.label]),
) as Record<PaneCommandId, string>;

/** The label every entry point shows for command `id`. */
export function paneCommandLabel(id: PaneCommandId): string {
  return LABELS[id];
}

export type PaneMenuItem =
  { kind: "command"; entry: PaneCommandEntry } | { kind: "separator"; key: string };

/**
 * `menu`'s commands that `offered` keeps, in order, with one separator between groups, never
 * leading, trailing or doubled.
 */
export function paneMenuEntries(
  menu: PaneMenu,
  offered: (entry: PaneCommandEntry) => boolean = () => true,
): PaneMenuItem[] {
  const entries: PaneMenuItem[] = [];
  let group: string | null = null;
  for (const entry of PANE_COMMANDS) {
    if (!entry.menus.includes(menu) || !offered(entry)) continue;
    if (group !== null && entry.group !== group) {
      entries.push({ kind: "separator", key: `before-${entry.id}` });
    }
    group = entry.group;
    entries.push({ kind: "command", entry });
  }
  return entries;
}

const ZOOM = PANE_COMMANDS.find((entry) => entry.id === "terminal.toggleZoom")!;

/** Maximize and Restore, as every entry point names them, and the maximized pane's own mark. */
export const PANE_ZOOM_LABELS = Object.freeze({
  maximize: ZOOM.label,
  restore: ZOOM.checkedLabel ?? ZOOM.label,
  maximized: "Maximized",
});
