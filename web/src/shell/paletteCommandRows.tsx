// ⌘K rows for registry commands: the app's own and those mounted features offer through
// `usePaletteCommands`, and the command a row runs, read again as it's accepted.

import type { QuickPickItem } from "@/components/ui/quick-pick";
import { chordKeys } from "@/lib/shortcuts/chord";
import {
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
  type PaletteCommandGroup,
} from "@/store/paletteCommands";
import { currentChoiceLabel } from "@/store/paletteSettings";

import { shortcutKeys } from "./workspaceTabs/workspaceSidebarShortcuts";

/** What a command row runs, and the target its command was built for. */
export interface PaletteCommandRow {
  kind: "command";
  command: PaletteCommand;
  /** `commandTargetKey` when the row was built; a row for another target does nothing. */
  targetKey: string;
}

/** `feature` is the registering source and its heading; the app's own commands have none. */
export function paletteCommandItem(
  command: PaletteCommand,
  targetKey: string,
  feature?: { source: string; heading: string },
): QuickPickItem<PaletteCommandRow> {
  const Icon = command.icon;
  const availability = command.availability;
  const setting = command.setting;
  const keys = command.shortcut
    ? shortcutKeys(command.shortcut)
    : command.nativeShortcut
      ? chordKeys(command.nativeShortcut)
      : [];
  return {
    id: feature ? `${feature.source}:${command.id}` : `command:${command.id}`,
    label: command.label,
    // A setting's row shows its value, and any note on it, and opens its values.
    description: setting?.note ?? command.description,
    detail: setting ? currentChoiceLabel(setting) : undefined,
    opensPage: setting || command.options ? true : undefined,
    destructive: command.destructive,
    leadsSources: command.leadsSources,
    keywords: command.keywords,
    group: feature?.heading,
    icon: <Icon aria-hidden />,
    shortcut: keys.length > 0 ? keys : undefined,
    disabledReason: availability?.available === false ? availability.reason : undefined,
    value: { kind: "command", command, targetKey },
  };
}

/** The palette's command rows: the app's own, then each feature's, as long as they're listed. */
export function paletteCommandItems(
  appCommands: readonly PaletteCommand[],
  featureGroups: Readonly<Record<string, PaletteCommandGroup>>,
  targetKey: string,
): QuickPickItem<PaletteCommandRow>[] {
  return [
    ...appCommands.map((command) => paletteCommandItem(command, targetKey)),
    ...Object.entries(featureGroups).flatMap(([source, group]) =>
      group.commands.map((command) =>
        paletteCommandItem(command, targetKey, { source, heading: group.heading }),
      ),
    ),
  ].filter((item) => isCommandListed(item.value.command.availability));
}

/**
 * The command a row runs now: rebuilt from the commands offered now, for the target as it is
 * now, so a row built for another target, or one withdrawn or unavailable since, finds none.
 */
export function currentPaletteCommand(
  itemId: string,
  rowTargetKey: string,
  offered: {
    appCommands: readonly PaletteCommand[];
    featureGroups: Readonly<Record<string, PaletteCommandGroup>>;
    targetKey: string;
  },
): PaletteCommand | null {
  if (rowTargetKey !== offered.targetKey) return null;
  const current = paletteCommandItems(
    offered.appCommands,
    offered.featureGroups,
    offered.targetKey,
  ).find((item) => item.id === itemId)?.value.command;
  return current && isCommandAvailable(current.availability) ? current : null;
}
