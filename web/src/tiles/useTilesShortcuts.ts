// Tile view keys, active when focus isn't in a text field or an open menu:
// [ and ] switch the active agent, F toggles full screen, Esc leaves it.

import { useEffect, useRef } from "react";

interface TilesShortcuts {
  ids: readonly string[];
  activeId: string | null;
  onActivate: (sessionId: string) => void;
  fullScreen: boolean;
  onToggleFullScreen: () => void;
}

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return (
    target.closest(
      'input, textarea, select, [contenteditable="true"], [role="dialog"], [role="menu"], [role="listbox"]',
    ) !== null
  );
}

/** The tile `step` places from the active one, wrapping around. */
export function neighborTile(
  ids: readonly string[],
  activeId: string | null,
  step: number,
): string | null {
  if (ids.length === 0) return null;
  const index = activeId ? ids.indexOf(activeId) : -1;
  if (index < 0) return ids[0];
  return ids[(index + step + ids.length) % ids.length];
}

export function useTilesShortcuts(options: TilesShortcuts) {
  const latest = useRef(options);
  latest.current = options;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;
      const { ids, activeId, onActivate, fullScreen, onToggleFullScreen } = latest.current;
      if (event.key === "Escape" && fullScreen) {
        event.preventDefault();
        onToggleFullScreen();
      } else if (event.key === "f" || event.key === "F") {
        event.preventDefault();
        onToggleFullScreen();
      } else if (event.key === "[" || event.key === "]") {
        const next = neighborTile(ids, activeId, event.key === "]" ? 1 : -1);
        if (next && next !== activeId) {
          event.preventDefault();
          onActivate(next);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
