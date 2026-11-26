// Handing focus to the Settings search field from ⌘K. The field mounts only once Settings
// opens, after the command navigates, so the request waits here until the field takes it;
// the shell opens a collapsed sidebar on the same request so the field can be seen.

import { useEffect, useRef } from "react";

/** How long a request waits for the field to mount and be seen before it lapses. */
export const SETTINGS_SEARCH_FOCUS_TIMEOUT_MS = 1000;

/** When the open request was made; it lasts until the field takes focus or it lapses. */
let requestedAt: number | null = null;
/** Which request is open, so ending an older one can't end a newer one. */
let requestId = 0;
/** Counts focus-target effect runs, so a cleanup can tell a replay from a real unmount. */
let targetRuns = 0;
const listeners = new Set<() => void>();

/** Asks the Settings search field to take focus, now or as soon as it mounts and can be seen. */
export function requestSettingsSearchFocus(now: number = performance.now()): void {
  requestedAt = now;
  requestId += 1;
  for (const listener of [...listeners]) listener();
}

/** Whether a request is waiting; one that has lapsed is dropped. */
export function hasPendingSettingsSearchFocus(now: number = performance.now()): boolean {
  if (requestedAt !== null && now - requestedAt > SETTINGS_SEARCH_FOCUS_TIMEOUT_MS) {
    requestedAt = null;
  }
  return requestedAt !== null;
}

/** Drops the waiting request, once the field has taken focus. */
export function clearSettingsSearchFocus(): void {
  requestedAt = null;
}

export function subscribeSettingsSearchFocus(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The field can take focus only once nothing around it hides it from users. */
function canTakeFocus(field: HTMLElement): boolean {
  return field.isConnected && field.closest("[aria-hidden='true'], [inert]") === null;
}

/**
 * Focuses `fieldRef` for each request: one waiting when the field mounts, and later ones while
 * it is mounted, as soon as the sidebar around it shows it. A request stays waiting until the
 * field takes it, so an effect that is torn down and run again, as in Strict Mode, resumes it.
 */
export function useSettingsSearchFocusTarget(fieldRef: React.RefObject<HTMLInputElement | null>) {
  useEffect(() => {
    targetRuns += 1;
    let frame = 0;
    const attempt = () => {
      cancelAnimationFrame(frame);
      if (!hasPendingSettingsSearchFocus()) return;
      const field = fieldRef.current;
      if (field && canTakeFocus(field)) {
        clearSettingsSearchFocus();
        field.focus();
        field.select();
        return;
      }
      frame = requestAnimationFrame(attempt);
    };
    attempt();
    const unsubscribe = subscribeSettingsSearchFocus(attempt);
    return () => {
      unsubscribe();
      cancelAnimationFrame(frame);
      // Strict Mode runs the effect again at once; a real unmount doesn't, and leaving takes
      // the open request with it, unless a newer one has been made since.
      const runs = targetRuns;
      const id = requestId;
      queueMicrotask(() => {
        if (targetRuns === runs && requestId === id) clearSettingsSearchFocus();
      });
    };
  }, [fieldRef]);
}

/** Opens the sidebar for each request, so the field it holds can be seen and focused. */
export function useOpenSidebarForSettingsSearch(openSidebar: () => void) {
  const openRef = useRef(openSidebar);
  openRef.current = openSidebar;
  useEffect(() => subscribeSettingsSearchFocus(() => openRef.current()), []);
}
