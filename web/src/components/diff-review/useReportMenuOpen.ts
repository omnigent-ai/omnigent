import { useEffect, useRef } from "react";

/**
 * Tell `onOpenChange` that a menu is open while `open` holds and the menu is mounted, and that
 * it closed once either ends, so a menu dropped by a re-render or re-key still reports closing.
 */
export function useReportMenuOpen(open: boolean, onOpenChange?: (open: boolean) => void): void {
  const latest = useRef(onOpenChange);
  latest.current = onOpenChange;
  useEffect(() => {
    if (!open) return;
    latest.current?.(true);
    return () => latest.current?.(false);
  }, [open]);
}
