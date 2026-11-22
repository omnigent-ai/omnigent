// With quick_pick_palette on, ⌘K offers the canvas conversation header's actions, with
// the header buttons' labels and handlers.

import { ArrowLeftIcon, type LucideIcon, Maximize2Icon, PanelLeftIcon, XIcon } from "lucide-react";
import { useMemo } from "react";

import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

import { type CanvasHeaderActionId, canvasHeaderActions } from "./canvasHeaderActions";
import type { useCanvasWorkspace } from "./CanvasWorkspace";

type CanvasControls = NonNullable<ReturnType<typeof useCanvasWorkspace>>;

/** The icon the header's button shows for `id` in the pane's current layout. */
function actionIcon(id: CanvasHeaderActionId, canvas: CanvasControls): LucideIcon {
  if (id === "toggle-focus") return canvas.focused ? PanelLeftIcon : Maximize2Icon;
  return canvas.compact ? ArrowLeftIcon : XIcon;
}

export function useCanvasHeaderPaletteCommands(canvas: CanvasControls | null): void {
  const commands = useMemo<PaletteCommand[]>(
    () =>
      // Only while a conversation pane is open: the bare board has no header actions.
      canvasHeaderActions(canvas?.conversationOpen ? canvas : null).map((action) => {
        return {
          id: `canvas.${action.id}`,
          label: action.label,
          icon: canvas ? actionIcon(action.id, canvas) : XIcon,
          keywords: ["canvas", "board", "pane"],
          run: () =>
            action.id === "toggle-focus" ? canvas?.toggleFocus() : canvas?.closeConversation(),
        };
      }),
    [canvas],
  );
  useQuickPickPaletteCommands("canvas-header", "Canvas", commands);
}
