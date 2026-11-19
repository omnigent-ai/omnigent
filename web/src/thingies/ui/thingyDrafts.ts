// Sessions with a New thingy tab open in their side panel, whose thingy isn't created yet.
import { create } from "zustand";

interface ThingyDraftsState {
  sessions: ReadonlySet<string>;
}

export const useThingyDrafts = create<ThingyDraftsState>(() => ({ sessions: new Set() }));

export function useHasThingyDraft(sessionId: string): boolean {
  return useThingyDrafts((state) => state.sessions.has(sessionId));
}

export function setThingyDraft(sessionId: string, open: boolean): void {
  useThingyDrafts.setState((state) => {
    if (state.sessions.has(sessionId) === open) return state;
    const sessions = new Set(state.sessions);
    if (open) sessions.add(sessionId);
    else sessions.delete(sessionId);
    return { sessions };
  });
}
