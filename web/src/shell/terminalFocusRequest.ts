// An explicit request to focus a terminal surface, sent when the user asks to show it or
// move between its panes. A surface restored passively (a reload, a remembered sidebar)
// gets no request, so it doesn't take focus. The split view resolves a request to its pane.
//
// A request stays outstanding until the surface's pane takes it, even if that surface
// hasn't mounted yet, and lapses when focus moves outside the surface or the surface hides.

import { useCallback, useSyncExternalStore } from "react";

import { canReturnFocusTo } from "@/lib/focusReturn";

export type TerminalFocusSurface = "main" | "rail";

export interface TerminalFocusRequest {
  conversationId: string;
  surface: TerminalFocusSurface;
  /** The terminal tab to focus; a split view picks its saved pane within it. Null: the view's own. */
  tabKey: string | null;
}

export interface PendingTerminalFocus extends TerminalFocusRequest {
  /** Tells one request from the next, so a pane takes each one once. */
  id: number;
}

/** Marks the element a surface's terminals render in; focus moving outside it cancels its request. */
export const TERMINAL_FOCUS_SURFACE_ATTRIBUTE = "data-terminal-focus-surface";

export function terminalFocusSurfaceId(conversationId: string, surface: TerminalFocusSurface) {
  return `${surface}:${conversationId}`;
}

function surfaceElements(
  conversationId: string,
  surface: TerminalFocusSurface,
  doc: Document,
): HTMLElement[] {
  const id = terminalFocusSurfaceId(conversationId, surface);
  return Array.from(
    doc.querySelectorAll<HTMLElement>(`[${TERMINAL_FOCUS_SURFACE_ATTRIBUTE}]`),
  ).filter((element) => element.getAttribute(TERMINAL_FOCUS_SURFACE_ATTRIBUTE) === id);
}

/** Whether a terminal in the surface has the keyboard now. */
export function isTerminalSurfaceFocused(
  conversationId: string,
  surface: TerminalFocusSurface,
  doc: Document = document,
): boolean {
  // A native embedded page can hold the keyboard while the shell stays the active element.
  if (!doc.hasFocus()) return false;
  const active = doc.activeElement;
  if (active === null) return false;
  return surfaceElements(conversationId, surface, doc).some((element) => element.contains(active));
}

/** Ask the surface for focus when it's on screen; false when it isn't. */
export function focusShownTerminalSurface(
  conversationId: string,
  surface: TerminalFocusSurface,
  doc: Document = document,
): boolean {
  if (!surfaceElements(conversationId, surface, doc).some(canReturnFocusTo)) return false;
  requestTerminalFocus({ conversationId, surface, tabKey: null });
  return true;
}

/** Take the keyboard away from the surface, so typing never lands in a shell that's gone. */
export function releaseTerminalSurfaceFocus(
  conversationId: string,
  surface: TerminalFocusSurface,
  doc: Document = document,
): void {
  const active = doc.activeElement;
  if (!(active instanceof HTMLElement)) return;
  if (surfaceElements(conversationId, surface, doc).some((element) => element.contains(active))) {
    active.blur();
  }
}

type Listener = (request: TerminalFocusRequest) => void;

const requestListeners = new Set<Listener>();
const changeListeners = new Set<() => void>();
const pending = new Map<string, PendingTerminalFocus>();
let nextId = 1;

function changed(): void {
  for (const listener of changeListeners) listener();
  if (pending.size > 0) document.addEventListener("focusin", dropRequestsFocusLeft, true);
  else document.removeEventListener("focusin", dropRequestsFocusLeft, true);
}

function dropRequestsFocusLeft(event: FocusEvent): void {
  if (!(event.target instanceof Element)) return;
  const focusedSurface = event.target
    .closest(`[${TERMINAL_FOCUS_SURFACE_ATTRIBUTE}]`)
    ?.getAttribute(TERMINAL_FOCUS_SURFACE_ATTRIBUTE);
  let dropped = false;
  for (const surfaceId of [...pending.keys()]) {
    if (surfaceId === focusedSurface) continue;
    pending.delete(surfaceId);
    dropped = true;
  }
  if (dropped) changed();
}

export function requestTerminalFocus(request: TerminalFocusRequest): void {
  pending.set(terminalFocusSurfaceId(request.conversationId, request.surface), {
    ...request,
    id: nextId++,
  });
  changed();
  for (const listener of requestListeners) listener(request);
}

/** The surface's outstanding request, if any. */
export function pendingTerminalFocus(
  conversationId: string,
  surface: TerminalFocusSurface,
): PendingTerminalFocus | null {
  return pending.get(terminalFocusSurfaceId(conversationId, surface)) ?? null;
}

/** Whether one of the session's terminals was asked for the keyboard and hasn't taken it yet. */
export function hasPendingTerminalFocus(conversationId: string): boolean {
  return (
    pendingTerminalFocus(conversationId, "main") !== null ||
    pendingTerminalFocus(conversationId, "rail") !== null
  );
}

/** Drop the surface's request: taken by `id`'s pane, or any request when `id` is omitted. */
export function settleTerminalFocus(
  conversationId: string,
  surface: TerminalFocusSurface,
  id?: number,
): void {
  const surfaceId = terminalFocusSurfaceId(conversationId, surface);
  const current = pending.get(surfaceId);
  if (!current || (id !== undefined && current.id !== id)) return;
  pending.delete(surfaceId);
  changed();
}

/** Hears each request as it's made; returns the unsubscribe function. */
export function subscribeTerminalFocusRequests(listener: Listener): () => void {
  requestListeners.add(listener);
  return () => {
    requestListeners.delete(listener);
  };
}

/** The surface's outstanding request, re-rendering as it changes. */
export function usePendingTerminalFocus(
  conversationId: string,
  surface: TerminalFocusSurface,
): PendingTerminalFocus | null {
  const subscribe = useCallback((onChange: () => void) => {
    changeListeners.add(onChange);
    return () => {
      changeListeners.delete(onChange);
    };
  }, []);
  return useSyncExternalStore(subscribe, () => pendingTerminalFocus(conversationId, surface));
}
