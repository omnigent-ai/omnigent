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

// ── Modal host_id for host-less / cross-host routes ─────────────────────────
//
// Routes with no host in their URL or body (`/v1/hosts` list, the session list,
// `WS /v1/sessions/updates`) still carry a slice key so they route by the
// managed sharding layer instead of relying on the server's implicit
// workspace-id default (OSS-version future-proofing) and tend to co-locate a
// user's traffic on the replica already holding the bulk of their sessions'
// tunnels (warm caches). The value is the MODAL host_id over every session seen
// this page (`_sessionHosts`) — the host backing the most of the user's
// sessions. It's a real host_id (not an opaque token): the whole client passes
// host_ids around, and the `host_id → X-Databricks-Omnigent-Slice-Key`
// translation happens in exactly one place (the request-header builder).
//
// RESOLVED ONCE, then never recomputed for the page's lifetime. The session
// list refetches continuously (poll + `/updates`-frame cache invalidations
// re-run `setSessionHost`), so `_sessionHosts` shifts all page long;
// recomputing the modal on every change would flap the value and, via
// `buildUpdatesUrl`, churn the `/updates` WebSocket. A hard reload starts a
// fresh module → a fresh pick. Correctness never depends on the guess: these
// routes serve from any replica, and a keyed request that guesses wrong
// self-heals via the host_unavailable keyless retry in `authenticatedFetch`.
let _modalHostId: string | null | undefined; // undefined = not yet resolved
let _modalHostResolved = false;

/**
 * The host id backing the most sessions in `hosts` (the modal / most common),
 * or `null` when the map is empty or every entry is hostless. Ties broken by
 * first-seen insertion order (Map iteration order), which is stable enough for
 * a best-effort cache-affinity hint.
 */
function pickModalHost(hosts: Map<string, string>): string | null {
  const counts = new Map<string, number>();
  let best: string | null = null;
  let bestCount = 0;
  for (const host of hosts.values()) {
    const next = (counts.get(host) ?? 0) + 1;
    counts.set(host, next);
    if (next > bestCount) {
      bestCount = next;
      best = host;
    }
  }
  return best;
}

/**
 * Compute and freeze the modal host_id ONCE, from the current `_sessionHosts`.
 * Idempotent: the first call resolves the value and flips
 * {@link isModalHostResolved} to `true`; every later call is a no-op (so
 * repeated list settles / refetches can't move the value). Call this on the
 * FIRST settle of the session-list query (success, empty, or error) — not gated
 * on a non-null modal, or a user with no sessions would never release the gate.
 *
 * @param readLastHostChoice - Injected accessor for the persisted last-picked
 *   host (from `hostPreferences`), used as a backstop when the session map is
 *   still empty at first settle (e.g. a returning user whose list is slow or
 *   empty). Injected rather than imported to keep this module dependency-light
 *   and easily testable.
 */
export function resolveModalHost(readLastHostChoice: () => string | null): void {
  if (_modalHostId !== undefined) return;
  _modalHostId = pickModalHost(_sessionHosts) ?? readLastHostChoice() ?? null;
  _modalHostResolved = true;
}

/**
 * The frozen modal host_id, or `null` (→ send no key → workspace-id default)
 * before it is resolved or when no host could be picked. Read by
 * `authenticatedFetch` (host-less routes) and `buildUpdatesUrl`.
 */
export function modalHostId(): string | null {
  return _modalHostId ?? null;
}

/**
 * Whether {@link resolveModalHost} has run. The `/updates` WebSocket gates its
 * start on this so it doesn't open with a pre-resolution (empty-map) key and
 * then have to re-key (a reconnect). Ordinary `authenticatedFetch` routes read
 * {@link modalHostId} eagerly (it simply returns `null` until resolved).
 */
export function isModalHostResolved(): boolean {
  return _modalHostResolved;
}

/**
 * Test-only reset of the resolution latch (module state persists across a test
 * file's cases). Not used in the app — a real reset is a page reload.
 */
export function _resetModalHostForTest(): void {
  _modalHostId = undefined;
  _modalHostResolved = false;
  _sessionHosts.clear();
}
