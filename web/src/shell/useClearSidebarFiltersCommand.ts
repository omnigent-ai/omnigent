// ⌘K's row that clears the sidebar's filters, offered only while one is on.

import { FilterXIcon } from "lucide-react";
import { useMemo, useRef } from "react";

import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

export function useClearSidebarFiltersCommand({
  filtersOn,
  clear,
}: {
  /** Whether any of the sidebar's filters narrows the list. */
  filtersOn: boolean;
  clear: () => void;
}): void {
  const latestClear = useRef(clear);
  latestClear.current = clear;
  const commands = useMemo<PaletteCommand[]>(
    () =>
      filtersOn
        ? [
            {
              id: "sidebar-filters:clear",
              label: "Clear sidebar filters",
              icon: FilterXIcon,
              keywords: ["filter", "sessions", "reset", "show all"],
              run: () => latestClear.current(),
            },
          ]
        : [],
    [filtersOn],
  );
  usePaletteCommands("sidebar-filters", "Sidebar filters", commands);
}
