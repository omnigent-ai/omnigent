// Desktop: ⌘[ / ⌘], the Go menu, swipes and mouse buttons step through in-app history.
// The sidebar Back/Forward buttons work everywhere; browser tabs keep native Back. Bind ONCE.

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { useDesktopCommand } from "@/lib/desktopCommands";
import { useIsEmbedded } from "@/lib/embedded";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isElectronShell } from "@/lib/nativeBridge";
import { useLocation, useNavigate, useNavigationType } from "@/lib/routing";

/** Surfaces that keep bracket chords (Monaco outdent/indent, the PTY, the palette). */
const BRACKET_OWNING_SURFACES = ".xterm, .monaco-editor, [cmdk-input]";
/** Sent by the Back/Forward buttons; the bound hook takes the step. */
const HISTORY_NAVIGATION_REQUEST_EVENT = "omnigent:history-navigation";

export type HistoryNavigationDirection = -1 | 1;

/** React Router's position in the history stack. */
function historyIndex(): number {
  const index = (window.history.state as { idx?: unknown } | null)?.idx;
  return typeof index === "number" ? index : 0;
}

/**
 * The first in-app history entry: 0 standalone. Embedded, the host's router owns
 * the history and its own pages come first, so it is the entry the app opened on.
 */
let firstAppEntry = 0;

/** The last entry Forward can reach; a push drops the entries after it. */
let lastReachableIndex: number | null = null;

interface HistoryNavigationAvailability {
  canGoBack: boolean;
  canGoForward: boolean;
}
const NO_HISTORY: HistoryNavigationAvailability = { canGoBack: false, canGoForward: false };
let availability = NO_HISTORY;
const availabilityListeners = new Set<() => void>();

/** Record a navigation. The always-mounted shell hook calls this, so none are missed. */
function recordNavigation(navigationType: string): void {
  const index = historyIndex();
  lastReachableIndex =
    lastReachableIndex === null || navigationType === "PUSH"
      ? index
      : Math.max(lastReachableIndex, index);
  const canGoBack = index > firstAppEntry;
  const canGoForward = index < lastReachableIndex;
  if (canGoBack === availability.canGoBack && canGoForward === availability.canGoForward) return;
  availability = { canGoBack, canGoForward };
  for (const listener of availabilityListeners) listener();
}

function subscribeToAvailability(listener: () => void): () => void {
  availabilityListeners.add(listener);
  return () => availabilityListeners.delete(listener);
}

/** Whether Back and Forward have an in-app entry to step to, for the buttons. */
export function useHistoryNavigationAvailability(): HistoryNavigationAvailability {
  return useSyncExternalStore(subscribeToAvailability, () => availability);
}

/** Forget recorded history between tests. */
export function resetHistoryNavigationForTests(): void {
  firstAppEntry = 0;
  lastReachableIndex = null;
  availability = NO_HISTORY;
}

/** Step back or forward the way ⌘[ / ⌘] do. */
export function requestHistoryNavigation(direction: HistoryNavigationDirection): void {
  window.dispatchEvent(new CustomEvent(HISTORY_NAVIGATION_REQUEST_EVENT, { detail: direction }));
}

export function useHistoryNavigation(isMac = isMacPlatform()): void {
  const navigate = useNavigate();
  const location = useLocation();
  const navigationType = useNavigationType();
  const embedded = useIsEmbedded();
  // Set during the first render, before anything below reads it.
  useState(() => {
    firstAppEntry = embedded ? historyIndex() : 0;
  });
  useEffect(() => recordNavigation(navigationType), [location, navigationType]);
  // Back stops at the first in-app entry rather than leaving for a sign-in or setup page.
  const go = useCallback(
    (delta: HistoryNavigationDirection) => {
      if (delta === -1 && historyIndex() <= firstAppEntry) return;
      void navigate(delta);
    },
    [navigate],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.defaultPrevented || event.altKey || event.shiftKey) return;
      if (!hasCommandModifier(event, isMac)) return;
      const delta = event.code === "BracketLeft" ? -1 : event.code === "BracketRight" ? 1 : 0;
      if (delta === 0) return;
      const focused = document.activeElement;
      if (focused instanceof Element && focused.closest(BRACKET_OWNING_SURFACES)) return;
      event.preventDefault();
      go(delta);
    };
    // Chromium already maps the mouse back/forward buttons to history; taking them here
    // also stops them at the first in-app entry.
    const onMouseUp = (event: MouseEvent) => {
      const delta = event.button === 3 ? -1 : event.button === 4 ? 1 : 0;
      if (delta === 0) return;
      event.preventDefault();
      go(delta);
    };
    const onRequest = (event: Event) => {
      const { detail } = event as CustomEvent<unknown>;
      if (detail === -1 || detail === 1) go(detail);
    };
    const electron = isElectronShell();
    window.addEventListener(HISTORY_NAVIGATION_REQUEST_EVENT, onRequest);
    if (electron) {
      window.addEventListener("keydown", onKeyDown);
      window.addEventListener("mouseup", onMouseUp);
    }
    return () => {
      window.removeEventListener(HISTORY_NAVIGATION_REQUEST_EVENT, onRequest);
      if (electron) {
        window.removeEventListener("keydown", onKeyDown);
        window.removeEventListener("mouseup", onMouseUp);
      }
    };
  }, [go, isMac]);

  useDesktopCommand("go-back", () => go(-1));
  useDesktopCommand("go-forward", () => go(1));
}
