// Which pane of a panes host has the keyboard. A pane whose shell ends takes its focused terminal
// with it before any effect can ask, so the host remembers the pane focus last went into.

import { type RefObject, useCallback, useEffect, useRef } from "react";

/** The pane in ``hostRef`` holding the keyboard now, or the one that did until it went away. */
export function usePaneKeyboardOwner(hostRef: RefObject<HTMLElement | null>): () => string | null {
  const owner = useRef<string | null>(null);
  useEffect(() => {
    // Focus anywhere else, in the page or another pane of this host, settles who has it.
    const onFocusIn = (event: FocusEvent) => {
      const host = hostRef.current;
      const target = event.target instanceof Element ? event.target : null;
      owner.current =
        host && target && host.contains(target)
          ? (target.closest<HTMLElement>("[data-terminal-pane]")?.dataset.terminalPane ?? null)
          : null;
    };
    // Focus left for nowhere, such as a click on empty space: a pane still in the page was left
    // on purpose, unlike one that went away with its shell.
    const onFocusOut = (event: FocusEvent) => {
      const left = event.target instanceof Element ? event.target : null;
      if (event.relatedTarget !== null || !left) return;
      queueMicrotask(() => {
        if (left.isConnected && document.activeElement === document.body) owner.current = null;
      });
    };
    document.addEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusout", onFocusOut, true);
    return () => {
      document.removeEventListener("focusin", onFocusIn, true);
      document.removeEventListener("focusout", onFocusOut, true);
    };
  }, [hostRef]);
  return useCallback(() => {
    const focused = document.activeElement;
    // A removed terminal leaves the keyboard on the page's body; anything else has it now.
    const stillHere =
      focused === null || focused === document.body || !!hostRef.current?.contains(focused);
    return stillHere ? owner.current : null;
  }, [hostRef]);
}
