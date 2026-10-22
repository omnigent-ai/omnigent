import type { KeyboardEvent, PointerEvent, RefObject } from "react";
import { cn } from "@/lib/utils";
import { MAX_SPLIT_RATIO, MIN_SPLIT_RATIO, type TerminalDivider } from "@/lib/terminalSplitLayout";

const KEYBOARD_STEP = 0.05;

/**
 * Draggable boundary between two terminal panes. Pointer drags report
 * draft ratios and end with ``onCommit``; arrow keys report final ratios.
 */
export function TerminalSplitDivider({
  divider,
  containerRef,
  onResize,
  onCommit,
}: {
  divider: TerminalDivider;
  containerRef: RefObject<HTMLElement | null>;
  onResize: (path: number[], ratio: number, final: boolean) => void;
  onCommit: () => void;
}) {
  const { axis, area, ratio, path } = divider;
  const columns = axis === "columns";

  const resizeToPointer = (event: PointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId) || !containerRef.current) return;
    const bounds = containerRef.current.getBoundingClientRect();
    if (bounds.width === 0 || bounds.height === 0) return;
    const position = columns
      ? ((event.clientX - bounds.left) / bounds.width) * 100
      : ((event.clientY - bounds.top) / bounds.height) * 100;
    onResize(
      path,
      columns ? (position - area.left) / area.width : (position - area.top) / area.height,
      false,
    );
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, number> = columns
      ? { ArrowLeft: -1, ArrowRight: 1 }
      : { ArrowUp: -1, ArrowDown: 1 };
    const step = keys[event.key];
    if (step === undefined) return;
    event.preventDefault();
    onResize(path, ratio + step * KEYBOARD_STEP, true);
  };

  const offset = columns ? area.left + area.width * ratio : area.top + area.height * ratio;
  return (
    <div
      role="separator"
      aria-orientation={columns ? "vertical" : "horizontal"}
      aria-label="Resize terminal panes"
      aria-valuemin={MIN_SPLIT_RATIO * 100}
      aria-valuemax={MAX_SPLIT_RATIO * 100}
      aria-valuenow={Math.round(ratio * 100)}
      tabIndex={0}
      data-testid="terminal-split-divider"
      onPointerDown={(event) => {
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={resizeToPointer}
      onPointerUp={onCommit}
      onPointerCancel={onCommit}
      onLostPointerCapture={onCommit}
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
          "bg-border group-hover:bg-ring group-focus-visible:bg-ring",
          columns ? "h-full w-px" : "h-px w-full",
        )}
      />
    </div>
  );
}
