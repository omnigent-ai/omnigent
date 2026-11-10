import { useCallback, useEffect, useRef, useState } from "react";

export function useResizableColumn(
  defaultWidth = 176,
  minWidth = 100,
  maxWidth = 480,
  storageKey?: string,
) {
  const [preferredWidth, setPreferredWidth] = useState(() => {
    try {
      const stored = storageKey ? Number(localStorage.getItem(storageKey)) : NaN;
      if (Number.isFinite(stored) && stored > 0) return stored;
    } catch {
      // Storage can be unavailable in embedded or private contexts.
    }
    return defaultWidth;
  });
  const maximum = Math.max(minWidth, maxWidth);
  const width = Math.max(minWidth, Math.min(maximum, preferredWidth));
  const current = useRef({ width, preferredWidth, minWidth, maximum, storageKey });
  current.current = { width, preferredWidth, minWidth, maximum, storageKey };
  const dragging = useRef<{ startX: number; startWidth: number; changed: boolean } | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);

  const persist = useCallback(() => {
    const state = current.current;
    if (!state.storageKey) return;
    try {
      localStorage.setItem(state.storageKey, String(state.preferredWidth));
    } catch {
      // A failed preference write must not prevent resizing.
    }
  }, []);

  const resize = useCallback((requested: number) => {
    const state = current.current;
    const next = Math.max(state.minWidth, Math.min(state.maximum, requested));
    if (next === state.width) return false;
    state.width = next;
    state.preferredWidth = next;
    setPreferredWidth(next);
    return true;
  }, []);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dragging.current = { startX: e.clientX, startWidth: current.current.width, changed: false };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      if (resize(current.current.width + (e.key === "ArrowLeft" ? -16 : 16))) persist();
    },
    [persist, resize],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      const drag = dragging.current;
      if (!drag) return;
      if (resize(drag.startWidth + e.clientX - drag.startX)) drag.changed = true;
    }
    function onMouseUp() {
      if (!dragging.current) return;
      if (dragging.current.changed) persist();
      dragging.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      if (dragging.current) {
        dragging.current = null;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
    };
  }, [persist, resize]);

  return {
    /** Pixel width for the left column (apply as inline style). */
    width,
    /** Optional ref for the flex-row container that owns the resize handle. */
    containerRef,
    /** Spread onto the resize handle element at the right edge of the left column. */
    handleProps: {
      onMouseDown,
      onKeyDown,
      tabIndex: 0,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize terminal list",
      "aria-valuenow": width,
      "aria-valuemin": minWidth,
      "aria-valuemax": maximum,
    },
  };
}
