// With quick_pick_palette on, ⌘K offers the open automation's header actions, greyed as its
// buttons are. Delete stays on its button, which deletes without asking.

import { PauseIcon, PencilIcon, PlayIcon, ShareIcon, type LucideIcon } from "lucide-react";
import { useMemo, useRef } from "react";

import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

import type { AutomationDetailAction, AutomationDetailActionId } from "./automationDetailActions";

export type AutomationPaletteActionId = Exclude<AutomationDetailActionId, "delete">;

const ICONS: Record<AutomationPaletteActionId, LucideIcon> = {
  "run-now": PlayIcon,
  edit: PencilIcon,
  share: ShareIcon,
  pause: PauseIcon,
  resume: PlayIcon,
};

// Edit and Share open a dialog, so they run once the palette has closed.
const OPENS_DIALOG: ReadonlySet<AutomationDetailActionId> = new Set(["edit", "share"]);

interface AutomationPaletteCommandsProps {
  taskId: string;
  actions: readonly AutomationDetailAction[];
  /** The header's own handlers. */
  run: Record<AutomationPaletteActionId, () => void>;
}

/** Renders nothing; registers the commands while the page shows. */
export function AutomationPaletteCommands({
  taskId,
  actions,
  run,
}: AutomationPaletteCommandsProps) {
  // The page builds its handlers each render; a command runs the newest.
  const handlers = useRef(run);
  handlers.current = run;
  const signature = JSON.stringify(actions.map(({ id, availability }) => [id, availability]));
  const commands = useMemo<PaletteCommand[]>(
    () =>
      actions.flatMap((action) => {
        if (action.id === "delete") return [];
        const id = action.id;
        return [
          {
            id: `automation.${id}:${taskId}`,
            label: action.label,
            icon: ICONS[id],
            keywords: ["automation", "task", "schedule"],
            availability: action.availability,
            movesFocus: OPENS_DIALOG.has(id),
            run: () => handlers.current[id](),
          },
        ];
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the signature covers `actions`.
    [taskId, signature],
  );
  useQuickPickPaletteCommands(`automation:${taskId}`, "Automation", commands);
  return null;
}
