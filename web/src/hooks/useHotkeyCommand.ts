// A workspace sidebar hotkey's ⌘K command: the hook's own action under the binding's
// label, with its chord shown as key caps, listed while the hook is mounted.

import { useId, useMemo, useRef } from "react";
import type { LucideIcon } from "lucide-react";

import type { Chord } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  shortcutBinding,
  type WorkspaceSidebarShortcutId,
} from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  isCommandListed,
  type PaletteCommand,
  usePaletteCommands,
} from "@/store/paletteCommands";

/** The palette heading the sidebar's hotkeys are listed under. */
export const WORKSPACE_HOTKEY_HEADING = "Workspace";

export interface HotkeyCommandOptions {
  icon: LucideIcon;
  /** The row's label now, for a toggle whose wording follows its state; the binding's otherwise. */
  label?: string;
  /** Extra terms the palette filter matches beyond the label. */
  keywords: readonly string[];
  /** The hotkey's action; the chord and the command both call it. */
  run: () => void;
  /** Where it can run; `enabled: false` without a reason hides it. Defaults to available. */
  availability?: CommandAvailability;
  /** Run after the palette closes and restores focus, for an action that moves focus. */
  movesFocus?: boolean;
}

/** Availability for a hotkey that is bound only while `enabled`; a reason greys it out instead. */
export function hotkeyAvailability(enabled: boolean, reason?: string): CommandAvailability {
  if (enabled) return AVAILABLE;
  return reason === undefined ? HIDDEN : { available: false, reason };
}

const NO_COMMANDS: PaletteCommand[] = [];

/** Offer binding `shortcut` as a ⌘K command running `options.run` while mounted. */
export function useHotkeyCommand(
  shortcut: WorkspaceSidebarShortcutId,
  options: HotkeyCommandOptions,
): void {
  // The latest closure runs, so a host re-rendering doesn't rebuild the command.
  const latest = useRef(options);
  latest.current = options;
  const { icon, movesFocus } = options;
  const label = options.label ?? shortcutBinding(shortcut).label;
  const keywords = options.keywords.join("\n");
  const availability = options.availability ?? AVAILABLE;
  const listed = isCommandListed(availability);
  const available = availability.available;
  const reason = availability.available ? undefined : availability.reason;
  // By value, so a fresh chord object each render doesn't rebuild the command.
  const chordText = JSON.stringify(activeShortcutChords(shortcut)[0] ?? null);

  const commands = useMemo<PaletteCommand[]>(
    () =>
      listed
        ? [
            {
              id: `hotkey:${shortcut}`,
              label,
              icon,
              keywords: keywords.split("\n"),
              // The sidebar's own chords aren't the app's, so they show as key caps only.
              nativeShortcut: (JSON.parse(chordText) as Chord | null) ?? undefined,
              movesFocus,
              availability: available ? AVAILABLE : { available: false, reason },
              run: () => latest.current.run(),
            },
          ]
        : NO_COMMANDS,
    [listed, shortcut, label, icon, keywords, chordText, movesFocus, available, reason],
  );
  // Each mount registers its own row set, so one host withdrawing its row leaves another's.
  const source = `hotkey:${shortcut}:${useId()}`;
  usePaletteCommands(source, WORKSPACE_HOTKEY_HEADING, commands);
}
