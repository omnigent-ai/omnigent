// Runs a menu action that moves focus only once its menu has closed, so the
// menu's own focus restoration can't fight the action.

import { useMemo, useRef } from "react";

export interface MenuCloseHandOff {
  /**
   * Holds `run` until the menu closes. With `afterFocusReturns`, the menu first
   * returns focus to its trigger, so a dialog `run` opens returns focus there too;
   * without it, `run` replaces that restoration, for actions that take the trigger away.
   */
  runAfterClose: (run: () => void, options?: { afterFocusReturns?: boolean }) => void;
  /** The menu content's `onCloseAutoFocus`. */
  onCloseAutoFocus: (event: Event) => void;
}

export function useMenuCloseHandOff(): MenuCloseHandOff {
  const pending = useRef<{ run: () => void; afterFocusReturns: boolean } | null>(null);
  return useMemo(
    () => ({
      runAfterClose: (run, options) => {
        pending.current = { run, afterFocusReturns: options?.afterFocusReturns ?? false };
      },
      onCloseAutoFocus: (event) => {
        const held = pending.current;
        pending.current = null;
        if (!held) return;
        if (held.afterFocusReturns) {
          // The menu restores focus right after this event; run once it has.
          setTimeout(held.run, 0);
          return;
        }
        event.preventDefault();
        held.run();
      },
    }),
    [],
  );
}
