// One owner for Cmd/Ctrl+F. Every find the page offers (an open file's, a
// review's) registers here, and the shortcut goes to the one the user is working in.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { matchesShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { isFocusInDialogOrMenu, isMacPlatform } from "./hotkeys";
import { isFocusInFindBar } from "./findBarFocus";

// A terminal, whose shell owns Ctrl+F (forward one character).
const TERMINAL_SURFACE = "[data-terminal-id]";

export interface FindSurface {
  /** Whether the user is working in this surface right now. */
  isActive: () => boolean;
  openFind: () => void;
  /** Takes find only where nothing more specific is active. */
  fallback?: boolean;
  /** What the find searches, such as one open file, so its menus can ask whether ⌘F opens it. */
  owner?: string;
}

/** The owner key for one file's find, shared by every surface that searches that file. */
export function fileFindOwner(conversationId: string | null | undefined, path: string): string {
  return `file:${conversationId ?? ""}:${path}`;
}

const surfaces: FindSurface[] = [];
const surfaceListeners = new Set<() => void>();

function notifySurfaceListeners(): void {
  for (const listener of surfaceListeners) listener();
}

/**
 * The platform's find chord: Cmd+F on macOS (where Ctrl+F moves the caret in
 * text fields), Ctrl+F elsewhere, without Shift (workspace search) or Alt.
 */
export function isFindShortcut(
  event: Pick<
    globalThis.KeyboardEvent,
    "altKey" | "ctrlKey" | "defaultPrevented" | "key" | "metaKey" | "shiftKey"
  >,
  isMac?: boolean,
): boolean {
  // One chord opens find in a file or in the conversation, whichever surface is active.
  return (
    !event.defaultPrevented &&
    matchesShortcut(event, "find.open", isMac === undefined ? undefined : { mac: isMac })
  );
}

/** Where a find request goes: `"app"` skips every component's find for the app's own. */
export interface FindRequest {
  to?: "app";
}

/**
 * Where a find request goes. `target` is null when nothing takes it; `handled` says the request
 * is the page's even then, as a repeat from a component's find bar with no app find to go to.
 */
export interface FindRoute {
  target: FindSurface | null;
  handled: boolean;
}

function fallbackSurface(): FindSurface | null {
  return surfaces.findLast((surface) => surface.fallback && surface.isActive()) ?? null;
}

/**
 * Where ⌘F goes: the find of the component the user is in (which refocuses it
 * when it's already open), or, from inside that component's find bar (or for an `"app"`
 * request), the app's find. A repeat never goes to another component.
 */
export function routeFind(request: FindRequest = {}): FindRoute {
  const component =
    request.to === "app"
      ? null
      : (surfaces.findLast((surface) => !surface.fallback && surface.isActive()) ?? null);
  if (component !== null && !isFocusInFindBar()) {
    return { target: component, handled: true };
  }
  const fallback = fallbackSurface();
  return { target: fallback, handled: fallback !== null || component !== null };
}

/** The surface a find request goes to: the newest active one, else the fallback. */
export function findTarget(request: FindRequest = {}): FindSurface | null {
  return routeFind(request).target;
}

/**
 * Off macOS the find chord is Ctrl+F, which a focused terminal's shell owns
 * (forward one character); there the terminal's find opens from ⌘K or its menu.
 */
function isShellChordInTerminal(): boolean {
  return !isMacPlatform() && !!document.activeElement?.closest(TERMINAL_SURFACE);
}

// Capture phase, so no other listener sees the shortcut before it is routed.
function handleKeyDown(event: KeyboardEvent): void {
  if (!isFindShortcut(event) || isFocusInDialogOrMenu() || isShellChordInTerminal()) return;
  const { target, handled } = routeFind();
  if (!handled) return;
  event.preventDefault();
  event.stopPropagation();
  target?.openFind();
}

/** Offer `surface` for Cmd/Ctrl+F until the returned function is called. */
export function registerFindSurface(surface: FindSurface): () => void {
  surfaces.push(surface);
  notifySurfaceListeners();
  if (surfaces.length === 1) {
    window.addEventListener("keydown", handleKeyDown, true);
  }
  return () => {
    const index = surfaces.indexOf(surface);
    if (index === -1) return;
    surfaces.splice(index, 1);
    notifySurfaceListeners();
    if (surfaces.length > 0) return;
    window.removeEventListener("keydown", handleKeyDown, true);
  };
}

/** Offer a find surface while mounted and `surface` is non-null. Callbacks may change freely. */
export function useFindSurface(surface: FindSurface | null): void {
  const latest = useRef(surface);
  latest.current = surface;
  const registered = surface !== null;
  const fallback = surface?.fallback ?? false;
  const owner = surface?.owner;
  useEffect(() => {
    if (!registered) return;
    return registerFindSurface({
      fallback,
      owner,
      isActive: () => latest.current?.isActive() ?? false,
      openFind: () => latest.current?.openFind(),
    });
  }, [registered, fallback, owner]);
}

/** Opens find where the user is working, as the chord does. */
export function openFindForActiveSurface(request: FindRequest = {}): void {
  routeFind(request).target?.openFind();
}

/**
 * The surface the ⌘K Find command opens: the active component's (even with its find open, which
 * it refocuses), else the app's, else the newest specific one, else any app find. So it opens
 * something whenever `useCanOpenFindFromCommand` offers it.
 */
function commandFindTarget(): FindSurface | null {
  return (
    surfaces.findLast((surface) => !surface.fallback && surface.isActive()) ??
    fallbackSurface() ??
    surfaces.findLast((surface) => !surface.fallback) ??
    surfaces.findLast((surface) => surface.fallback) ??
    null
  );
}

/** Opens find for the ⌘K Find command, which runs once focus is back where the user was. */
export function openFindFromCommand(): void {
  commandFindTarget()?.openFind();
}

function subscribeToSurfaces(listener: () => void): () => void {
  surfaceListeners.add(listener);
  return () => surfaceListeners.delete(listener);
}

/** Whether the ⌘K Find command has a find to open. */
export function useCanOpenFindFromCommand(): boolean {
  return useSyncExternalStore(subscribeToSurfaces, () => surfaces.length > 0);
}

/** Whether a find for `owner` is registered, so a menu row can show the chord that opens it. */
export function useHasFindSurface(owner: string): boolean {
  return useSyncExternalStore(subscribeToSurfaces, () =>
    surfaces.some((surface) => surface.owner === owner && !surface.fallback),
  );
}
