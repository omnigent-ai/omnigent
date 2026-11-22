import { type RefObject, useEffect } from "react";

import type { FilePosition } from "./FileViewerContext";
import { stopFilePosition } from "./filePositionState";

const READER_INPUT = ["wheel", "touchstart", "pointerdown", "keydown"] as const;

/**
 * Retire `position`, a citation, once the reader scrolls, taps, clicks or types inside
 * `area`: a remount then restores where they were rather than citing the line again.
 */
export function useStopFilePositionOnInput(
  area: RefObject<HTMLElement | null>,
  position: FilePosition | undefined,
): void {
  useEffect(() => {
    const element = area.current;
    if (!element || !position) return;
    const stop = () => stopFilePosition(position);
    for (const event of READER_INPUT) {
      element.addEventListener(event, stop, { passive: true, capture: true });
    }
    return () => {
      for (const event of READER_INPUT) element.removeEventListener(event, stop, { capture: true });
    };
  }, [area, position]);
}
