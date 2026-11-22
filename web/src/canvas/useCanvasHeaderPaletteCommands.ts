// With quick_pick_palette on, ⌘K offers the canvas conversation header's actions, with
// the header buttons' labels and handlers.

import { ArrowLeftIcon, type LucideIcon, Maximize2Icon, PanelLeftIcon, XIcon } from "lucide-react";
import { useMemo } from "react";

import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

import { canvasHeaderActions } from "./canvasHeaderActions";
import type { useCanvasWorkspace } from "./CanvasWorkspace";

type CanvasControls = NonNullable<ReturnType<typeof useCanvasWorkspace>>;

export function useCanvasHeaderPaletteCommands(canvas: CanvasControls | null): void {
  const commands = useMemo<PaletteCommand[]>(
    () =>
      // Only while a conversation pane is open: the bare board has no header actions.
      canvasHeaderActions(canvas?.conversationOpen ? canvas : null).map((action) => {
        const icon: LucideIcon =
          action.id === "toggle-focus"
            ? canvas?.focused
              ? PanelLeftIcon
              : Maximize2Icon
            : canvas?.compact
              ? ArrowLeftIcon
              : XIcon;
        return {
          id: `canvas.${action.id}`,
          label: action.label,
          icon,
          keywords: ["canvas", "board", "pane"],
          run: () =>
            action.id === "toggle-focus" ? canvas?.toggleFocus() : canvas?.closeConversation(),
        };
      }),
    [canvas],
  );
  useQuickPickPaletteCommands("canvas-header", "Canvas", commands);
}
