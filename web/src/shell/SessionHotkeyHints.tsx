import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { PINNED_HOTKEY_DIGITS } from "@/hooks/usePinnedSessionHotkeys";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";

const NO_HINTS: readonly string[] = [];
const SessionHotkeyContext = createContext(NO_HINTS);

export function SessionHotkeyHints({
  ids,
  children,
}: {
  ids: readonly string[];
  children: ReactNode;
}) {
  const [held, setHeld] = useState(false);
  useEffect(() => {
    const update = (event: KeyboardEvent) => setHeld(hasCommandModifier(event) && !event.shiftKey);
    const reset = () => setHeld(false);
    const visibilitychange = () => {
      if (document.hidden) reset();
    };
    window.addEventListener("keydown", update, true);
    window.addEventListener("keyup", update, true);
    window.addEventListener("blur", reset);
    document.addEventListener("visibilitychange", visibilitychange);
    return () => {
      window.removeEventListener("keydown", update, true);
      window.removeEventListener("keyup", update, true);
      window.removeEventListener("blur", reset);
      document.removeEventListener("visibilitychange", visibilitychange);
    };
  }, []);
  return (
    <SessionHotkeyContext.Provider value={held ? ids : NO_HINTS}>
      {children}
    </SessionHotkeyContext.Provider>
  );
}

export function SessionHotkeyHint({ id }: { id: string }) {
  const ids = useContext(SessionHotkeyContext);
  const digit = PINNED_HOTKEY_DIGITS[ids.indexOf(id)];
  if (!digit) return null;
  const modifier = isMacPlatform() ? "⌘" : "Ctrl+";
  const alt = isNativeShell() ? "" : isMacPlatform() ? "⌥" : "Alt+";
  return (
    <kbd
      aria-label={`Switch to session: ${modifier}${alt}${digit}`}
      className="ml-2 shrink-0 rounded border border-border px-1 font-sans text-xs text-muted-foreground/70"
    >
      {alt}
      {digit}
    </kbd>
  );
}
