import { isImeCompositionKeyEvent } from "@/lib/ime";
import { useEffect, useLayoutEffect, useRef } from "react";

/** Let the active picker consume Escape before the parent dialog's capture handler. */
export function useScheduleEscape(open: boolean, dismiss: () => boolean) {
  const dismissRef = useRef(dismiss);
  useLayoutEffect(() => {
    dismissRef.current = dismiss;
  });
  useEffect(() => {
    if (!open) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        isImeCompositionKeyEvent({ nativeEvent: event }) ||
        !dismissRef.current()
      )
        return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    window.addEventListener("keydown", handleEscape, true);
    return () => window.removeEventListener("keydown", handleEscape, true);
  }, [open]);
}
