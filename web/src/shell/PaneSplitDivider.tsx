import type { KeyboardEvent, RefObject } from "react";
import { cn } from "@/lib/utils";
import {
  MAX_SPLIT_RATIO,
  MIN_SPLIT_RATIO,
  SPLIT_NUDGE_STEP,
  steppedSplitRatio,
  type TerminalDivider,
} from "@/lib/paneSplitLayout";

import type { PointerDragEnd } from "./paneDividerDragEnd";
import { usePaneDividerPointer } from "./usePaneDividerPointer";

// Defaults for a divider used without a layout's drag session.
const ignoreStart = () => {};
const ignoreAbort = () => {};

/**
 * Draggable boundary between two terminal panes. Pointer drags report draft ratios and end with
 * ``onCommit`` on release, or ``onAbort`` when the pointer is taken away; arrow keys report final
 * ratios.
 */
export function PaneSplitDivider({
  divider,
  containerRef,
  onResize,
  onCommit,
  onStart = ignoreStart,
  onAbort = ignoreAbort,
  dragging = false,
  inGap = false,
}: {
  divider: TerminalDivider;
  /**
   * Sits in the gap between two cards: the gap is the divider, so it draws no line, as the
   * sidebars' edges don't; only the keyboard's focus shows one.
   */
  inGap?: boolean;
  containerRef: RefObject<HTMLElement | null>;
  onResize: (path: number[], ratio: number, final: boolean) => void;
  onCommit: () => void;
  onStart?: (divider: TerminalDivider) => void;
  onAbort?: (reason: PointerDragEnd) => void;
  /** Whether the layout still has this divider's drag. */
  dragging?: boolean;
}) {
  const { axis, area, ratio, path } = divider;
  const columns = axis === "columns";
  const pointer = usePaneDividerPointer({
    containerRef,
    dragging,
    onStart: () => onStart(divider),
    onAbort,
    onMove: (point) =>
      onResize(
        path,
        columns ? (point.left - area.left) / area.width : (point.top - area.top) / area.height,
        false,
      ),
    onCommit,
  });

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, number> = columns
      ? { ArrowLeft: -1, ArrowRight: 1 }
      : { ArrowUp: -1, ArrowDown: 1 };
    const step = keys[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const next = steppedSplitRatio(ratio, step * SPLIT_NUDGE_STEP);
    if (next !== null) onResize(path, next, true);
  };

  const offset = columns ? area.left + area.width * ratio : area.top + area.height * ratio;
  const percent = Math.round(ratio * 100);
  return (
    <div
      role="separator"
      aria-orientation={columns ? "vertical" : "horizontal"}
      aria-label="Resize terminal panes"
      // A long row's split can sit past the resize limits; the range includes it.
      aria-valuemin={Math.min(Math.round(MIN_SPLIT_RATIO * 100), percent)}
      aria-valuemax={Math.max(Math.round(MAX_SPLIT_RATIO * 100), percent)}
      aria-valuenow={percent}
      tabIndex={0}
      data-testid="terminal-split-divider"
      {...pointer}
      onKeyDown={onKeyDown}
      // A 7px hit area centered on a 1px line.
      style={
        columns
          ? {
              left: `calc(${offset}% - 3px)`,
              top: `${area.top}%`,
              height: `${area.height}%`,
              width: 7,
            }
          : {
              top: `calc(${offset}% - 3px)`,
              left: `${area.left}%`,
              width: `${area.width}%`,
              height: 7,
            }
      }
      className={cn(
        "group absolute z-10 flex touch-none items-center justify-center outline-none",
        columns ? "cursor-col-resize" : "cursor-row-resize",
      )}
    >
      <div
        className={cn(
          inGap ? "bg-transparent" : "bg-border group-hover:bg-ring",
          "group-focus-visible:bg-ring",
          columns ? "h-full w-px" : "h-px w-full",
        )}
      />
    </div>
  );
}
