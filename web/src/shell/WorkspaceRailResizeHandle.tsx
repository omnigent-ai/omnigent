// The workspace rail's drag edge: a labeled separator on its left border, inside the rail. With
// floating panes the gutter between the panes and the rail drags it too, as the left sidebar's does.

import type { HTMLAttributes, MouseEventHandler } from "react";

import { cn } from "@/lib/utils";

type ResizeHandleProps = HTMLAttributes<HTMLDivElement> & { tabIndex: number };

/** How far the gutter band reaches into the rail, matching the left sidebar's. */
const GUTTER_HANDLE_OVERLAP_PX = 5;
/** The gap between floating panes and the rail, where the page's styles don't set one. */
const PANE_GAP = "var(--pane-gap, 5px)";

/** A pending rail keeps its handle on screen but doesn't resize or take focus. */
export function workspaceRailHandleProps(
  handleProps: ResizeHandleProps,
  pending: boolean,
): ResizeHandleProps {
  if (!pending) return handleProps;
  return {
    ...handleProps,
    onMouseDown: undefined,
    onKeyDown: undefined,
    "aria-disabled": true,
    tabIndex: -1,
  };
}

/**
 * The rail's separator, on its left border. It is the one the keyboard and assistive tech use,
 * in every mode; beside floating panes (48rem and wider) it draws no bar, as the gutter band
 * carries the cursor.
 */
export function WorkspaceRailEdgeHandle({
  handleProps,
  pending,
  bare = false,
}: {
  handleProps: ResizeHandleProps;
  pending: boolean;
  bare?: boolean;
}) {
  return (
    <div
      {...workspaceRailHandleProps(handleProps, pending)}
      className={cn(
        "absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors",
        bare && "min-[48rem]:hover:bg-transparent min-[48rem]:active:bg-transparent",
        pending && "cursor-default hover:bg-transparent active:bg-transparent",
      )}
    />
  );
}

/**
 * Pointer-only gutter drag target, 48rem and wider; the rail separator remains the accessible
 * handle. Kept mounted so closing the rail does not make the rail its parent's last child.
 */
export function WorkspaceRailGutterHandle({
  onMouseDown,
  pending,
  railWidth,
  shown,
}: {
  onMouseDown: MouseEventHandler<HTMLDivElement> | undefined;
  pending: boolean;
  railWidth: number;
  /** Whether the rail is open, so the band shows beside it. */
  shown: boolean;
}) {
  return (
    <div
      aria-hidden
      onMouseDown={pending || !shown ? undefined : onMouseDown}
      data-workspace-rail-gutter-handle=""
      className={cn(
        "absolute inset-y-0 z-50 hidden",
        shown && "md:min-[48rem]:block",
        pending ? "cursor-default" : "cursor-col-resize",
      )}
      style={{
        right: Math.max(0, railWidth - GUTTER_HANDLE_OVERLAP_PX),
        width: `calc(${PANE_GAP} + ${GUTTER_HANDLE_OVERLAP_PX}px)`,
      }}
    />
  );
}
