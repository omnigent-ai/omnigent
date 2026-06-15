import { useCallback, useEffect, useRef, useState } from "react";

export function useResizableColumn(defaultWidth = 176, minWidth = 100, maxWidth = 480) {
  const [width, setWidth] = useState(defaultWidth);
  const dragging = useRef(false);
  const containerRef = useRef<HTMLElement | null>(null);
  const minRef = useRef(minWidth);
  const maxRef = useRef(maxWidth);
  minRef.current = minWidth;
  maxRef.current = maxWidth;

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current || !containerRef.current) return;
      const left = containerRef.current.getBoundingClientRect().left;
      setWidth(Math.max(minRef.current, Math.min(maxRef.current, e.clientX - left)));
    }
    function onMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      if (dragging.current) {
        dragging.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
    };
  }, []);

  return {
    /** Pixel width for the left column (apply as inline style). */
    width,
    /** Attach to the flex-row container to anchor drag calculations. */
    containerRef,
    /** Spread onto the resize handle element at the right edge of the left column. */
    handleProps: {
      onMouseDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize terminal list",
    },
  };
}
