import { useEffect, useRef } from "react";

/** True for Slack-style Shift+Escape, with no command or Alt modifier. */
export function isClearAllNotificationsHotkey(event: globalThis.KeyboardEvent): boolean {
  return (
    event.key === "Escape" &&
    event.shiftKey &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.getModifierState("AltGraph")
  );
}

/** Bind Shift+Escape once at the app shell to clear notification state. */
export function useClearAllNotificationsHotkey(onClear: () => void, enabled = true): void {
  const latest = useRef(onClear);
  latest.current = onClear;

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat || event.defaultPrevented || !isClearAllNotificationsHotkey(event)) {
        return;
      }
      event.preventDefault();
      // Capture phase keeps Shift+Escape from reaching plain-Escape handlers
      // owned by dialogs, menus, and the chat composer.
      event.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [enabled]);
}
