// With quick_pick_palette on, ⌘K offers the open grid's row actions: Rename grid…, which
// edits its sidebar row's name in place, and Delete grid, as the row's menu does.

import { PencilIcon, Trash2Icon } from "lucide-react";
import { useMemo, useRef } from "react";

import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

const NO_COMMANDS: PaletteCommand[] = [];

export function useGridPaletteCommands({
  gridId,
  title,
  open,
  rename,
  remove,
}: {
  gridId: string;
  title: string;
  /** The grid on screen; only its row offers commands. */
  open: boolean;
  rename: () => void;
  remove: () => void;
}): void {
  const handlers = useRef({ rename, remove });
  handlers.current = { rename, remove };
  const commands = useMemo<PaletteCommand[]>(
    () =>
      open
        ? [
            {
              id: `grid.rename:${gridId}`,
              label: "Rename grid…",
              description: title,
              icon: PencilIcon,
              keywords: ["grid", "name", "title"],
              // The row's name field takes focus once ⌘K has closed.
              movesFocus: true,
              run: () => handlers.current.rename(),
            },
            {
              id: `grid.delete:${gridId}`,
              label: "Delete grid",
              description: title,
              icon: Trash2Icon,
              keywords: ["grid", "remove"],
              destructive: true,
              run: () => handlers.current.remove(),
            },
          ]
        : NO_COMMANDS,
    [open, gridId, title],
  );
  useQuickPickPaletteCommands(`grid:${gridId}`, "Grid", commands);
}
