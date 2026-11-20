// App-wide commands only the QuickPick ⌘K offers, as registry commands: they can move focus
// once the palette closes, open a page of values, or keep the palette open. The commands both
// palettes list stay in `useAppCommands`.

import { useMemo } from "react";
import { KeyboardIcon, SearchIcon } from "lucide-react";

import { openKeyboardShortcuts } from "@/components/KeyboardShortcutsDialog";
import { openFindFromCommand, useCanOpenFindFromCommand } from "@/lib/findRouting";
import { AVAILABLE, HIDDEN, type PaletteCommand } from "@/store/paletteCommands";

export function useQuickPickAppCommands(): PaletteCommand[] {
  const canOpenFind = useCanOpenFindFromCommand();
  return useMemo<PaletteCommand[]>(
    () => [
      {
        id: "find",
        label: "Find…",
        icon: SearchIcon,
        keywords: ["search", "in file", "in conversation", "text", "match"],
        shortcut: "find.open",
        // Find opens in the surface the palette hands focus back to.
        movesFocus: true,
        availability: canOpenFind ? AVAILABLE : HIDDEN,
        run: openFindFromCommand,
      },
      {
        id: "show-keyboard-shortcuts",
        label: "Show keyboard shortcuts",
        icon: KeyboardIcon,
        keywords: ["hotkeys", "keys", "bindings", "chords", "cheat sheet", "help"],
        shortcut: "shortcuts.show",
        // Opens the reference once the palette has closed, so it keeps focus.
        movesFocus: true,
        run: openKeyboardShortcuts,
      },
    ],
    [canOpenFind],
  );
}
