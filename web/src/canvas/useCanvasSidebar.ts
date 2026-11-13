import { useCallback, useLayoutEffect, useRef, useState, type SetStateAction } from "react";

/** Collapse once on Canvas entry, and remember subsequent manual changes. */
export function useCanvasSidebar(inCanvas: boolean, initialOpen: () => boolean) {
  const [open, setOpen] = useState(initialOpen);
  const beforeCanvas = useRef<boolean | null>(null);
  const inCanvasRef = useRef(inCanvas);
  inCanvasRef.current = inCanvas;

  useLayoutEffect(() => {
    if (inCanvas) {
      setOpen((previous) => {
        beforeCanvas.current ??= previous;
        return false;
      });
    } else if (beforeCanvas.current !== null) {
      setOpen(beforeCanvas.current);
      beforeCanvas.current = null;
    }
  }, [inCanvas]);

  const setManually = useCallback((update: SetStateAction<boolean>) => {
    setOpen((previous) => {
      const next = typeof update === "function" ? update(previous) : update;
      if (inCanvasRef.current && next !== previous) beforeCanvas.current = next;
      return next;
    });
  }, []);

  return [open, setManually] as const;
}
