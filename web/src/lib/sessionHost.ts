// Client-side `session_id → host_id` map for managed-server slice-key routing.
//
// The managed server shards replicas by host_id; a session's runner tunnel
// lives on its host's replica, so session-scoped traffic (turn dispatch,
// terminal attach) must carry that host_id to reach it. Recorded when a session
// object is parsed (`sessionFromWire`) and read by the two client chokepoints —
// `authenticatedFetch` (header on `/v1/sessions/{id}/*`) and the terminal-attach
// WS URL builder (query param) — so neither call site has to thread host_id
// down from wherever it loaded the session.
//
// A small standalone map (rather than reading the TanStack Query cache) keeps
// this decoupled from the query-key convention and needs no QueryClient handle.
// A session's host_id is fixed for its lifetime, so the recorded value can't go
// stale. Returns `null` when the session hasn't been loaded yet or is a hostless
// local session, leaving routing to the workspace-id fallback.

const _sessionHosts = new Map<string, string>();

/**
 * Record (or clear) the host a session is bound to. Called wherever a session
 * object is parsed; a null/absent host clears any stale mapping so a session
 * that loses its host binding stops routing to the old replica.
 */
export function setSessionHost(sessionId: string, hostId: string | null | undefined): void {
  if (hostId) {
    _sessionHosts.set(sessionId, hostId);
  } else {
    _sessionHosts.delete(sessionId);
  }
}

/**
 * Resolve a session's host_id for slice-key routing, or `null` when unknown
 * (session not loaded yet, or a hostless local session).
 */
export function getSessionHost(sessionId: string): string | null {
  return _sessionHosts.get(sessionId) ?? null;
}
