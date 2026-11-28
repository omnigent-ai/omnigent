// With quick_pick_palette on, ⌘K runs the composer's Add menu actions: the same
// availability and handlers as the menu's items, under "Composer".

import {
  LightbulbIcon,
  type LucideIcon,
  MessagesSquareIcon,
  PaperclipIcon,
  TargetIcon,
} from "lucide-react";
import { useMemo, useRef } from "react";

import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

import type { ComposerAddAction, ComposerAddActionId } from "./composerAddActions";

const ICONS: Record<ComposerAddActionId, LucideIcon> = {
  attach: PaperclipIcon,
  "side-chat": MessagesSquareIcon,
  goal: TargetIcon,
  plan: LightbulbIcon,
};

const KEYWORDS: Record<ComposerAddActionId, string[]> = {
  attach: ["upload", "file", "image", "add"],
  "side-chat": ["btw", "aside", "question", "fork"],
  goal: ["objective", "target"],
  plan: ["planning", "mode", "toggle"],
};

// They open a file picker, a dialog or the side chat, which should get focus once ⌘K closes.
const MOVES_FOCUS = new Set<ComposerAddActionId>(["attach", "side-chat", "goal"]);

export type ComposerAddHandlers = Partial<Record<ComposerAddActionId, () => void>>;

const NO_COMMANDS: PaletteCommand[] = [];

/** The heading the composer's ⌘K rows share. */
export const COMPOSER_PALETTE_HEADING = "Composer";

/** Offers `actions` under `source` while the menu is mounted; no source offers none. */
export function useComposerAddPaletteCommands(
  source: string | undefined,
  actions: readonly ComposerAddAction[],
  handlers: ComposerAddHandlers,
): void {
  const latest = useRef(handlers);
  latest.current = handlers;
  const signature = JSON.stringify(actions);
  const commands = useMemo<PaletteCommand[]>(
    () =>
      source === undefined
        ? NO_COMMANDS
        : actions.map((action) => ({
            id: `${source}.${action.id}`,
            label: action.label,
            icon: ICONS[action.id],
            keywords: KEYWORDS[action.id],
            availability: action.availability,
            movesFocus: MOVES_FOCUS.has(action.id),
            run: () => latest.current[action.id]?.(),
          })),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the signature covers `actions`.
    [source, signature],
  );
  useQuickPickPaletteCommands(source ?? "composer", COMPOSER_PALETTE_HEADING, commands);
}
