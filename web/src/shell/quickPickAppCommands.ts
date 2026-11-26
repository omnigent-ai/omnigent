// App-wide commands only the QuickPick ⌘K offers, as registry commands: they can move focus
// once the palette closes, open a page of values, or keep the palette open. The commands both
// palettes list stay in `useAppCommands`.

import { useMemo } from "react";
import { KeyboardIcon, MessagesSquareIcon, PaletteIcon, SearchIcon } from "lucide-react";

import { openKeyboardShortcuts } from "@/components/KeyboardShortcutsDialog";
import { openFindFromCommand, useCanOpenFindFromCommand } from "@/lib/findRouting";
import { AVAILABLE, HIDDEN, type PaletteCommand } from "@/store/paletteCommands";
import { openThemePicker, SELECT_THEME_COMMAND } from "@/store/themePicker";

import { useSettingsCommands } from "./settingsCommands";

/** The picker ⌘K continues as for Go to Session…, which takes the palette's return target. */
export const SESSION_SWITCHER = "session-switcher";

export interface QuickPickAppCommandOptions {
  /** Switches the palette to searching sessions only; absent where it can't. */
  onGoToSession?: () => void;
}

export function useQuickPickAppCommands({
  onGoToSession,
}: QuickPickAppCommandOptions = {}): PaletteCommand[] {
  const canOpenFind = useCanOpenFindFromCommand();
  const settingsCommands = useSettingsCommands();
  return useMemo<PaletteCommand[]>(
    () => [
      ...(onGoToSession
        ? [
            {
              id: "go-to-session",
              label: "Go to Session…",
              icon: MessagesSquareIcon,
              keywords: ["open", "find", "switch", "session", "conversation", "chat"],
              shortcut: "palette.sessions" as const,
              // The palette stays open, now searching sessions.
              keepsPaletteOpen: true,
              continuesAs: SESSION_SWITCHER,
              run: onGoToSession,
            },
          ]
        : []),
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
      {
        ...SELECT_THEME_COMMAND,
        icon: PaletteIcon,
        // Opens the picker once the palette has closed, so it keeps focus.
        movesFocus: true,
        run: openThemePicker,
      },
      ...settingsCommands,
    ],
    [canOpenFind, onGoToSession, settingsCommands],
  );
}
