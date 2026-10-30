import { useEffect } from "react";

/** Let the active picker consume Escape before the parent dialog's capture handler. */
export function useScheduleEscape(open: boolean, dismiss: () => boolean) {
  useEffect(() => {
    if (!open) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || !dismiss()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    window.addEventListener("keydown", handleEscape, true);
    return () => window.removeEventListener("keydown", handleEscape, true);
  }, [open, dismiss]);
}
