// The draggable inner edge of a pane sized by `useRememberedPaneWidth`: its right edge, or its
// left for a pane at the right. Its parent is the pane, positioned `relative`, so the handle
// straddles the pane's border.

import type { RememberedPaneWidth } from "@/hooks/useRememberedPaneWidth";
import { cn } from "@/lib/utils";

export function PaneResizeHandle({
  resize,
  testId,
  side = "right",
  grip = false,
}: {
  resize: RememberedPaneWidth;
  testId?: string;
  side?: "left" | "right";
  /** Show a grip on hover and focus, for an edge that's easy to miss. */
  grip?: boolean;
}) {
  return (
    // Drag the edge, or focus it and use the arrow keys, to resize; double-click resets.
    <div
      {...resize.handleProps}
      data-testid={testId}
      data-collapsing={resize.collapsing || undefined}
      className={cn(
        "group/resize absolute inset-y-0 z-10 w-1 cursor-col-resize transition-colors hover:bg-primary/30 focus-visible:bg-primary/50 focus-visible:outline-none",
        side === "right" ? "-right-0.5" : "-left-0.5",
        resize.resizing && "bg-primary/50",
        // A release here collapses the pane: the edge says so before the button goes up.
        resize.collapsing && "bg-primary",
      )}
    >
      {grip && (
        <span
          aria-hidden
          className="absolute top-1/2 left-1/2 h-8 w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-muted-foreground/60 opacity-0 transition-opacity group-hover/resize:opacity-100 group-focus-visible/resize:opacity-100"
        />
      )}
    </div>
  );
}
