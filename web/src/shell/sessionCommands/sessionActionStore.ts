// The session action ⌘K asked for last. `SessionActionHost` runs it once; a newer request
// replaces it, and the writes it started report how they went on their own.

import { create } from "zustand";

import type { Conversation } from "@/hooks/useConversations";

import type { SessionCommandId } from "./model";

export interface SessionActionRequest {
  /** The session as the palette listed it; the host looks up its current state by ID. */
  conversation: Conversation;
  command: SessionCommandId;
  /** Whether the sidebar's lists held the session when its actions opened. */
  listed: boolean;
  /** New per request, and rising, so asking again for the same action starts it again. */
  key: number;
}

interface SessionActionState {
  request: SessionActionRequest | null;
}

export const useSessionActionStore = create<SessionActionState>(() => ({ request: null }));

let nextKey = 0;
// Requests up to this key have started, so a host mounted again doesn't repeat one.
let startedKey = 0;

/** Carry out `command` on `conversation`, unless it no longer applies when the host reads it. */
export function requestSessionAction(
  conversation: Conversation,
  command: SessionCommandId,
  { listed = false }: { listed?: boolean } = {},
): void {
  nextKey += 1;
  useSessionActionStore.setState({ request: { conversation, command, listed, key: nextKey } });
}

/** The newest request, which the host runs. */
export function latestSessionActionRequest(): SessionActionRequest | null {
  return useSessionActionStore.getState().request;
}

/** Claim `key`'s action to start it: false once it, or a newer one, has started. */
export function startSessionAction(key: number): boolean {
  if (key <= startedKey) return false;
  startedKey = key;
  return true;
}
