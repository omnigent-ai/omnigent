import { create } from "zustand";

// Asks the finder to open on its Sessions tab. Unlike Files and Text it needs no open session,
// so its requests come here rather than through the session's file-finder request.

export interface SessionSearchRequest {
  /** Changes with each request, so asking again while it shows is seen. */
  id: number;
  /** Text the Sessions tab starts with; undefined keeps what the finder has typed. */
  query?: string;
}

interface SessionSearchState {
  request: SessionSearchRequest | null;
}

let nextRequestId = 1;

export const useSessionSearchStore = create<SessionSearchState>(() => ({ request: null }));

/** Opens the finder on Sessions, starting from `query` when given. */
export function requestSessionSearch(query?: string): void {
  useSessionSearchStore.setState({ request: { id: nextRequestId++, query } });
}
