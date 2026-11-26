// The conversation pane's header actions on a canvas, shared by the header's buttons and
// ⌘K: back to the board when the pane fills the window, else focus and close.

export type CanvasHeaderActionId = "close-conversation" | "toggle-focus";

export const BACK_TO_CANVAS_LABEL = "Back to canvas";
export const CLOSE_CONVERSATION_PANE_LABEL = "Close conversation pane";
export const SHOW_CANVAS_LABEL = "Show canvas beside conversation";
export const FOCUS_CONVERSATION_LABEL = "Focus conversation";

export interface CanvasHeaderAction {
  id: CanvasHeaderActionId;
  label: string;
}

/** The actions the header shows for `canvas`; none outside a canvas. */
export function canvasHeaderActions(
  canvas: { compact: boolean; focused: boolean } | null,
): CanvasHeaderAction[] {
  if (!canvas) return [];
  if (canvas.compact) return [{ id: "close-conversation", label: BACK_TO_CANVAS_LABEL }];
  return [
    {
      id: "toggle-focus",
      label: canvas.focused ? SHOW_CANVAS_LABEL : FOCUS_CONVERSATION_LABEL,
    },
    { id: "close-conversation", label: CLOSE_CONVERSATION_PANE_LABEL },
  ];
}
