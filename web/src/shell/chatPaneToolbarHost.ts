// Portal slots that place the chat and its Chat/Terminal switch inside panes; absent slots leave
// them in the column and the header.

import { useCallback, useRef, useSyncExternalStore } from "react";

type SlotKind = "body" | "viewToggle";

const hosts = new Map<string, HTMLElement>();
const listeners = new Set<() => void>();

const slotKey = (kind: SlotKind, conversationId: string) => `${kind}:${conversationId}`;

function setHost(kind: SlotKind, conversationId: string, element: HTMLElement | null): void {
  const key = slotKey(kind, conversationId);
  if (element) {
    if (hosts.get(key) === element) return;
    hosts.set(key, element);
  } else {
    if (!hosts.has(key)) return;
    hosts.delete(key);
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function useHost(kind: SlotKind, conversationId: string | null | undefined): HTMLElement | null {
  return useSyncExternalStore(subscribe, () =>
    conversationId ? (hosts.get(slotKey(kind, conversationId)) ?? null) : null,
  );
}

/** Register `element` as the slot over the agent's terminal where the web chat renders. */
export const setChatPaneBodyHost = (conversationId: string, element: HTMLElement | null) =>
  setHost("body", conversationId, element);

/** Where `conversationId`'s web chat renders as a pane of its split, or null for the column. */
export const useChatPaneBodyHost = (conversationId: string | null | undefined) =>
  useHost("body", conversationId);

// The switch's spot can be claimed by two strips at once, as the view changes: the chat's own
// strip mounting while the Terminal view's lets go. Each releases only its own claim, and the
// latest claim still held is the host.
const viewToggleClaims = new Map<string, HTMLElement[]>();

function updateViewToggleHost(conversationId: string): void {
  const claims = viewToggleClaims.get(conversationId) ?? [];
  setHost("viewToggle", conversationId, claims.at(-1) ?? null);
}

/** Claim `element` as `conversationId`'s spot for the Chat/Terminal switch. */
export function claimChatPaneViewToggleHost(conversationId: string, element: HTMLElement): void {
  const claims = (viewToggleClaims.get(conversationId) ?? []).filter((held) => held !== element);
  viewToggleClaims.set(conversationId, [...claims, element]);
  updateViewToggleHost(conversationId);
}

/** Release `element`'s claim; an earlier claim still held becomes the host again. */
export function releaseChatPaneViewToggleHost(conversationId: string, element: HTMLElement): void {
  const claims = (viewToggleClaims.get(conversationId) ?? []).filter((held) => held !== element);
  if (claims.length > 0) viewToggleClaims.set(conversationId, claims);
  else viewToggleClaims.delete(conversationId);
  updateViewToggleHost(conversationId);
}

/** A ref callback that claims the element it's given and releases that one when it goes. */
export function useChatPaneViewToggleHostRef(
  conversationId: string,
): (element: HTMLElement | null) => void {
  const claimed = useRef<{ conversationId: string; element: HTMLElement } | null>(null);
  return useCallback(
    (element: HTMLElement | null) => {
      if (claimed.current) {
        releaseChatPaneViewToggleHost(claimed.current.conversationId, claimed.current.element);
        claimed.current = null;
      }
      if (element) {
        claimed.current = { conversationId, element };
        claimChatPaneViewToggleHost(conversationId, element);
      }
    },
    [conversationId],
  );
}

/** Where the header's Chat/Terminal switch renders, in the chat pane's strip, or null. */
export const useChatPaneViewToggleHost = (conversationId: string | null | undefined) =>
  useHost("viewToggle", conversationId);

const besideSplit = new Set<string>();

/**
 * Whether `conversationId`'s split has panes besides the chat's, so it stays shown in Chat.
 * Kept in Terminal too, so switching to Chat shows the split from its first render.
 */
export function setSplitBesideChat(conversationId: string, shown: boolean): void {
  if (besideSplit.has(conversationId) === shown) return;
  if (shown) besideSplit.add(conversationId);
  else besideSplit.delete(conversationId);
  for (const listener of listeners) listener();
}

export const useSplitBesideChat = (conversationId: string | null | undefined) =>
  useSyncExternalStore(subscribe, () => !!conversationId && besideSplit.has(conversationId));

const otherPaneMaximized = new Set<string>();

/** Whether `conversationId` shows a pane other than the chat maximized, which hides the chat. */
export function setOtherPaneMaximized(conversationId: string, maximized: boolean): void {
  if (otherPaneMaximized.has(conversationId) === maximized) return;
  if (maximized) otherPaneMaximized.add(conversationId);
  else otherPaneMaximized.delete(conversationId);
  for (const listener of listeners) listener();
}

/** While another pane is maximized, the header's Chat/Terminal switch has no pane to switch. */
export const useOtherPaneMaximized = (conversationId: string | null | undefined) =>
  useSyncExternalStore(subscribe, () => !!conversationId && otherPaneMaximized.has(conversationId));
