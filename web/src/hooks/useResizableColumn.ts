import { useCallback, useEffect, useRef, useState } from "react";

export function useResizableColumn(
  defaultWidth = 176,
  minWidth = 100,
  maxWidth = 480,
  storageKey?: string,
) {
  const [preferredWidth, setPreferredWidth] = useState(() => {
    try {
      const stored = storageKey ? Number(window.localStorage.getItem(storageKey)) : NaN;
      return Number.isFinite(stored) && stored > 0 ? stored : defaultWidth;
    } catch {
      return defaultWidth;
    }
  });
  const width = Math.max(minWidth, Math.min(maxWidth, preferredWidth));
  const widthRef = useRef(width);
  widthRef.current = width;
  const dragging = useRef<{ startX: number; moved: boolean } | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);
  const minRef = useRef(minWidth);
  const maxRef = useRef(maxWidth);
  minRef.current = minWidth;
  maxRef.current = maxWidth;

  const resize = useCallback((nextWidth: number) => {
    const clamped = Math.max(minRef.current, Math.min(maxRef.current, nextWidth));
    widthRef.current = clamped;
    setPreferredWidth(clamped);
    return clamped;
  }, []);

  const persistWidth = useCallback(
    (nextWidth: number) => {
      if (!storageKey) return;
      try {
        window.localStorage.setItem(storageKey, String(nextWidth));
      } catch {
        // Resizing still works when browser storage is unavailable.
      }
    },
    [storageKey],
  );

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = { startX: e.clientX, moved: false };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      persistWidth(resize(widthRef.current + (event.key === "ArrowRight" ? 16 : -16)));
    },
    [persistWidth, resize],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current || !containerRef.current) return;
      if (!dragging.current.moved && e.clientX === dragging.current.startX) return;
      dragging.current.moved = true;
      const left = containerRef.current.getBoundingClientRect().left;
      resize(e.clientX - left);
    }
    function onMouseUp() {
      if (!dragging.current) return;
      if (dragging.current.moved) persistWidth(widthRef.current);
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
  }, [persistWidth, resize]);

  return {
    /** Pixel width for the left column (apply as inline style). */
    width,
    /** Attach to the flex-row container to anchor drag calculations. */
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
      "aria-valuemax": maxWidth,
    },
  };
}
