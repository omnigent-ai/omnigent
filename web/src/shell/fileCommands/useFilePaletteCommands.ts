// A file's actions in ⌘K, from the same model and host as its menus.

import { useMemo } from "react";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { FILE_COMMAND_ICONS } from "./icons";
import { fileCommandTargetKey } from "./target";
import type { FileCommandsForPath } from "./useFileCommands";

const NO_COMMANDS: PaletteCommand[] = [];

/**
 * Offer `file`'s actions under "File" while `active`. `surface` names the
 * caller, so two surfaces offering files register apart.
 */
export function useFilePaletteCommands(
  surface: string,
  file: FileCommandsForPath,
  active: boolean,
): void {
  const { target, commands, run } = file;
  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    if (!active || !target) return NO_COMMANDS;
    return commands.map((command) => ({
      id: `file.${command.id}`,
      label: command.label,
      icon: FILE_COMMAND_ICONS[command.id],
      keywords: ["file", ...command.keywords],
      availability: command.availability,
      run: () => run(command.id),
    }));
  }, [active, target, commands, run]);
  // The source names the target, so a row built for another file never matches.
  const source = `file:${surface}:${target ? fileCommandTargetKey(target) : "none"}`;
  usePaletteCommands(source, "File", paletteCommands);
}
