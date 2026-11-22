// The session actions ⌘K asked for. `SessionActionHost` runs the newest, and keeps an older
// one mounted, with its dialogs closed, until the writes it started have settled.

import { create } from "zustand";

import type { Conversation } from "@/hooks/useConversations";

import type { SessionCommandId } from "./model";

export interface SessionActionRequest {
  /** The session as the palette listed it; the host looks up its current state by ID. */
  conversation: Conversation;
  command: SessionCommandId;
  /** Whether the sidebar's lists held the session when its actions opened. */
  listed: boolean;
  /** New per request, so asking again for the same action starts it again. */
  key: number;
}

interface SessionActionState {
  /** Oldest first; the last is the one the host shows. */
  requests: readonly SessionActionRequest[];
}

export const useSessionActionStore = create<SessionActionState>(() => ({ requests: [] }));

let nextKey = 0;
// Keys whose action has started, so a host mounted again doesn't repeat one.
const startedKeys = new Set<number>();

/** Carry out `command` on `conversation`, unless it no longer applies when the host reads it. */
export function requestSessionAction(
  conversation: Conversation,
  command: SessionCommandId,
  { listed = false }: { listed?: boolean } = {},
): void {
  nextKey += 1;
  const request = { conversation, command, listed, key: nextKey };
  useSessionActionStore.setState((state) => ({ requests: [...state.requests, request] }));
}

/** The newest request, which the host shows. */
export function latestSessionActionRequest(): SessionActionRequest | null {
  return useSessionActionStore.getState().requests.at(-1) ?? null;
}

/** Claim `key`'s action to start it: false once it has started, on any host. */
export function startSessionAction(key: number): boolean {
  if (startedKeys.has(key)) return false;
  startedKeys.add(key);
  return true;
}

/** Done with `key`'s request: its writes settled after a newer one replaced it. */
export function settleSessionAction(key: number): void {
  useSessionActionStore.setState((state) => ({
    requests: state.requests.filter((request) => request.key !== key),
  }));
}
