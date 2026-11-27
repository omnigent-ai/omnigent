// "Archive a Session…" and the other verb-first rows: each opens ⌘K's session picker for that
// action, so typing the verb finds it among the commands.

import { useMemo } from "react";

import type { PaletteCommand } from "@/store/paletteCommands";

import { openPaletteChildPage } from "../commandPaletteChildPage";
import { useQuickPickPaletteCommands } from "../screenCommands/useQuickPickPaletteCommands";
import { SESSION_COMMAND_ICONS } from "./icons";
import { SESSION_COMMAND_PICKS, type SessionCommandId } from "./model";

export const SESSION_PICK_COMMANDS_HEADING = "Any session";

/** One row per pickable action, in the row menu's order. */
export function sessionPickCommands(): PaletteCommand[] {
  return (Object.entries(SESSION_COMMAND_PICKS) as [SessionCommandId, { label: string }][]).map(
    ([command, pick]) => ({
      id: `pick-session:${command}`,
      label: pick.label,
      icon: SESSION_COMMAND_ICONS[command],
      keywords: ["session", "any session", "other session", "sidebar"],
      // Switches the palette to its session picker for this action.
      keepsPaletteOpen: true,
      run: () => openPaletteChildPage({ kind: "pick-session", command }),
    }),
  );
}

/** Registers the verb-first rows while quick_pick_palette is on. */
export function useSessionPickPaletteCommands(): void {
  const commands = useMemo(sessionPickCommands, []);
  useQuickPickPaletteCommands("session-picks", SESSION_PICK_COMMANDS_HEADING, commands);
}
