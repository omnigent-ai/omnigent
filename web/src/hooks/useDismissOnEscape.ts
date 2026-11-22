import { isImeCompositionKeyEvent } from "@/lib/ime";
import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { useEffect, useLayoutEffect, useRef } from "react";

/** While `open`, an Escape that nothing else handled, wherever focus is, calls `dismiss`. */
export function useDismissOnEscape(open: boolean, dismiss: () => void) {
  const isEmbedded = useIsEmbedded();
  const dismissRef = useRef(dismiss);
  useLayoutEffect(() => {
    dismissRef.current = dismiss;
  });
  useEffect(() => {
    if (!open) return;
    // Bubble phase on the document, so dialogs, menus and the composer claim Escape first.
    const handleEscape = (event: KeyboardEvent) => {
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        isImeCompositionKeyEvent({ nativeEvent: event })
      )
        return;
      event.preventDefault();
      dismissRef.current();
    };
    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [open, isEmbedded]);
}
