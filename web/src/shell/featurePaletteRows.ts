// The ⌘K rows that mounted features offer through `usePaletteCommands`, such as the tabs
// sidebar's Back and Forward, as both palettes list them. Loaded with the
// workspace_sidebar_v2 flag only, so a deployment without it never downloads it.

import type { LucideIcon } from "lucide-react";

import { chordKeys } from "@/lib/shortcuts/chord";
import { shortcutKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import {
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
  type PaletteCommandGroup,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";

export interface FeaturePaletteRow {
  /** Unique among the palette's rows: the offering source and the command's own id. */
  id: string;
  /** The offering feature's heading, which groups its rows. */
  heading: string;
  label: string;
  icon: LucideIcon;
  keywords: readonly string[];
  /** Key caps for the chord that runs it outside the palette. */
  shortcut?: readonly string[];
  /** Set when it can't run now: the row stays listed and says why. */
  disabledReason?: string;
}

export interface FeaturePaletteSource {
  subscribe: (listener: () => void) => () => void;
  /** The rows offered now; the same array until the offered commands change. */
  rows: () => readonly FeaturePaletteRow[];
  /** The row's command as offered now, if it can run; null once withdrawn or unavailable. */
  command: (rowId: string) => PaletteCommand | null;
}

const rowId = (source: string, command: PaletteCommand) => `${source}\u0000${command.id}`;

function rowFor(source: string, heading: string, command: PaletteCommand): FeaturePaletteRow {
  const keys = command.shortcut
    ? shortcutKeys(command.shortcut)
    : command.nativeShortcut
      ? chordKeys(command.nativeShortcut)
      : [];
  const availability = command.availability;
  return {
    id: rowId(source, command),
    heading,
    label: command.label,
    icon: command.icon,
    keywords: command.keywords,
    shortcut: keys.length > 0 ? keys : undefined,
    disabledReason: availability?.available === false ? availability.reason : undefined,
  };
}

/**
 * Every listed command, grouped by its source. A setting or a command that asks which
 * first needs a page of its own, which these palettes don't have, so it isn't listed.
 */
export function featurePaletteRows(
  groups: Readonly<Record<string, PaletteCommandGroup>>,
): FeaturePaletteRow[] {
  return Object.entries(groups).flatMap(([source, group]) =>
    group.commands
      .filter(
        (command) => isCommandListed(command.availability) && !command.setting && !command.options,
      )
      .map((command) => rowFor(source, group.heading, command)),
  );
}

let cached: { groups: Record<string, PaletteCommandGroup>; rows: FeaturePaletteRow[] } | null =
  null;

export const featurePaletteSource: FeaturePaletteSource = {
  subscribe: (listener) => usePaletteCommandsStore.subscribe(listener),
  rows: () => {
    const { groups } = usePaletteCommandsStore.getState();
    if (cached?.groups !== groups) cached = { groups, rows: featurePaletteRows(groups) };
    return cached.rows;
  },
  command: (id) => {
    const { groups } = usePaletteCommandsStore.getState();
    for (const [source, group] of Object.entries(groups)) {
      const command = group.commands.find((candidate) => rowId(source, candidate) === id);
      if (command) return isCommandAvailable(command.availability) ? command : null;
    }
    return null;
  },
};
