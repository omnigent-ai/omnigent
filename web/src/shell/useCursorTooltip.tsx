import { useState } from "react";

/**
 * Returns mouse event handlers for a trigger element and a fixed-positioned
 * tooltip node that follows the cursor. Render `tooltip` as a sibling to the
 * trigger (outside any interactive element) — it uses `position: fixed` so it
 * escapes all overflow / stacking contexts automatically.
 */
export function useCursorTooltip(text: string): {
  handlers: {
    onMouseMove: (e: React.MouseEvent) => void;
    onMouseLeave: () => void;
  };
  tooltip: React.ReactNode;
} {
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number } | null>(null);

  const handlers = {
    onMouseMove: (e: React.MouseEvent) => setCursorPos({ x: e.clientX, y: e.clientY }),
    onMouseLeave: () => setCursorPos(null),
  };

  const tooltip = cursorPos ? (
    <div
      style={{ position: "fixed", left: cursorPos.x, top: cursorPos.y + 14, pointerEvents: "none" }}
      className="z-50 inline-flex w-fit items-center rounded-md border border-border bg-popover px-3 py-1.5 text-xs text-popover-foreground shadow-sm"
    >
      {text}
    </div>
  ) : null;

  return { handlers, tooltip };
}
