// The session action ⌘K asked for last. `SessionActionHost` checks it against the session's
// current facts, then runs it or opens its dialog.

import { create } from "zustand";

import type { Conversation } from "@/hooks/useConversations";

import type { SessionCommandId } from "./model";

export interface SessionActionRequest {
  /** The session as the palette listed it. */
  conversation: Conversation;
  command: SessionCommandId;
  /** New per request, so asking again for the same action starts it again. */
  key: number;
}

interface SessionActionState {
  request: SessionActionRequest | null;
}

export const useSessionActionStore = create<SessionActionState>(() => ({ request: null }));

let nextKey = 0;

/** Carry out `command` on `conversation`, unless it no longer applies when the host reads it. */
export function requestSessionAction(conversation: Conversation, command: SessionCommandId): void {
  nextKey += 1;
  useSessionActionStore.setState({ request: { conversation, command, key: nextKey } });
}
