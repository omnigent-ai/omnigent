// Cmd/Ctrl+Shift+F focuses the sidebar session search. This intentionally
// avoids Cmd/Ctrl+K so that chord stays available for a future command palette.

import { useEffect, useRef } from "react";

interface SessionSearchHotkeyOptions {
  sidebarOpen: boolean;
  onOpenSidebar: () => void;
  onFocusSearch: () => void;
}

export function isSessionSearchHotkey(e: KeyboardEvent): boolean {
  return (e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === "f";
}

export function useSessionSearchHotkey({
  sidebarOpen,
  onOpenSidebar,
  onFocusSearch,
}: SessionSearchHotkeyOptions): void {
  const latest = useRef({ sidebarOpen, onOpenSidebar, onFocusSearch });
  latest.current = { sidebarOpen, onOpenSidebar, onFocusSearch };

  useEffect(() => {
    const handler = (e: KeyboardEvent): void => {
      if (!isSessionSearchHotkey(e)) return;

      e.preventDefault();

      const {
        sidebarOpen: open,
        onOpenSidebar: openSidebar,
        onFocusSearch: focusSearch,
      } = latest.current;
      if (!open) openSidebar();
      focusSearch();
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
