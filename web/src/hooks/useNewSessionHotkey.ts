// Cmd+N (Ctrl+N on Win/Linux) navigates to "/" for a new session, matching
// the sidebar "New session" button and the command palette's "New chat" action.
// Suppressed inside xterm / Monaco surfaces that bind Cmd+N themselves, and
// guards against AltGraph on international layouts.

import { useEffect } from "react";
import { useNavigate } from "@/lib/routing";

const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

export function useNewSessionHotkey(): void {
  const navigate = useNavigate();

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat) return;
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      if (e.key !== "n" && e.key !== "N") return;
      if (e.getModifierState("AltGraph")) return;

      const el = document.activeElement;
      if (el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES) !== null) return;

      e.preventDefault();
      e.stopPropagation();
      navigate("/");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate]);
}
