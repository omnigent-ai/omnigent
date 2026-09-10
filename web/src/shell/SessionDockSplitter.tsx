import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { cn } from "@/lib/utils";

const MIN_PANE_PCT = 15;
const MAX_PANE_PCT = 85;
const KEYBOARD_RESIZE_STEP = 4;
export const SESSION_WORKSPACE_BASIS_VAR = "--session-workspace-basis";

interface SessionDockSplitterProps {
  dockRef: RefObject<HTMLDivElement | null>;
  label: string;
  sizePct: number;
  onResize: (sizePct: number) => void;
  onCollapse: () => void;
}

type ResizeAxis = "x" | "y";

function readAxis(dock: HTMLDivElement): ResizeAxis {
  return getComputedStyle(dock).flexDirection.startsWith("row") ? "x" : "y";
}

function clampSize(sizePct: number): number {
  return Math.max(MIN_PANE_PCT, Math.min(MAX_PANE_PCT, sizePct));
}

export function SessionDockSplitter({
  dockRef,
  label,
  sizePct,
  onResize,
  onCollapse,
}: SessionDockSplitterProps) {
  const [axis, setAxis] = useState<ResizeAxis>("y");
  const draggingRef = useRef(false);
  const axisRef = useRef<ResizeAxis>("y");
  const latestSizeRef = useRef(sizePct);
  const willCollapseRef = useRef(false);

  useLayoutEffect(() => {
    const dock = dockRef.current;
    if (!dock) return;
    const update = () => setAxis(readAxis(dock));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(dock);
    return () => observer.disconnect();
  }, [dockRef]);

  const setLiveBasis = useCallback(
    (nextSizePct: number) => {
      dockRef.current?.style.setProperty(SESSION_WORKSPACE_BASIS_VAR, `${nextSizePct}%`);
    },
    [dockRef],
  );

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const dock = dockRef.current;
      if (!dock) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      draggingRef.current = true;
      willCollapseRef.current = false;
      latestSizeRef.current = sizePct;
      axisRef.current = readAxis(dock);
    },
    [dockRef, sizePct],
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) return;
      const dock = dockRef.current;
      if (!dock) return;
      const rect = dock.getBoundingClientRect();
      const extent = axisRef.current === "x" ? rect.width : rect.height;
      if (extent <= 0) return;
      const rawSize =
        axisRef.current === "x"
          ? ((rect.right - event.clientX) / extent) * 100
          : ((rect.bottom - event.clientY) / extent) * 100;
      if (rawSize < MIN_PANE_PCT) {
        willCollapseRef.current = true;
        setLiveBasis(0);
        return;
      }
      willCollapseRef.current = false;
      const nextSize = clampSize(rawSize);
      latestSizeRef.current = nextSize;
      setLiveBasis(nextSize);
    },
    [dockRef, setLiveBasis],
  );

  const endDrag = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      if (willCollapseRef.current) {
        onCollapse();
        return;
      }
      onResize(latestSizeRef.current);
    },
    [onCollapse, onResize],
  );

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const delta =
        event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? KEYBOARD_RESIZE_STEP
          : event.key === "ArrowRight" || event.key === "ArrowDown"
            ? -KEYBOARD_RESIZE_STEP
            : null;
      if (delta === null) return;
      event.preventDefault();
      onResize(clampSize(sizePct + delta));
    },
    [onResize, sizePct],
  );

  return (
    <div
      role="separator"
      aria-label={`Resize workspace for ${label}`}
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-valuemin={MIN_PANE_PCT}
      aria-valuemax={MAX_PANE_PCT}
      aria-valuenow={Math.round(sizePct)}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onLostPointerCapture={endDrag}
      onKeyDown={onKeyDown}
      className={cn(
        "group relative z-20 hidden h-1.5 w-full shrink-0 cursor-row-resize bg-transparent focus:outline-none md:block",
        "@min-[720px]/session-column:h-auto @min-[720px]/session-column:w-1.5 @min-[720px]/session-column:cursor-col-resize",
      )}
    >
      <div
        className={cn(
          "absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border transition-colors group-hover:bg-primary/60 group-focus-visible:bg-primary/70",
          "@min-[720px]/session-column:inset-x-auto @min-[720px]/session-column:inset-y-0 @min-[720px]/session-column:left-1/2 @min-[720px]/session-column:h-auto @min-[720px]/session-column:w-px @min-[720px]/session-column:-translate-x-1/2 @min-[720px]/session-column:translate-y-0",
        )}
      />
    </div>
  );
}
