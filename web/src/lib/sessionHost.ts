// Client-side `session_id → host_id` map for host-sharded routing.
//
// When the server shards replicas by host_id, a session's runner tunnel lives
// on its host's replica, so session-scoped traffic (turn dispatch, terminal
// attach) must carry that host_id to reach it. Recorded when a session object
// is parsed (`sessionFromWire`) and read by the two client chokepoints —
// `authenticatedFetch` (header on `/v1/sessions/{id}/*`) and the terminal-attach
// WS URL builder (query param) — so neither call site has to thread host_id
// down from wherever it loaded the session.
//
// A small standalone map (rather than reading the TanStack Query cache) keeps
// this decoupled from the query-key convention and needs no QueryClient handle.
// A session's host_id is fixed for its lifetime, so the recorded value can't go
// stale. Returns `null` when the session hasn't been loaded yet or is a hostless
// local session, leaving routing to the default.

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

// ── Keyless-demoted hosts ────────────────────────────────────────────────────
//
// A host whose tunnels dialed in WITHOUT a key (an older client that predates
// host sharding) homes ALL its tunnels on the default replica, not
// `hash(host_id)`. So every keyed request for such a host lands on the wrong
// replica and must fall back to keyless — not just the routes with an explicit
// server-side wrong-replica guard, but the long tail of control paths (approve,
// stop, interrupt, /compact, …) that otherwise silently 2xx on the wrong
// replica. Rather than guard each server-side, the client REMEMBERS the
// demotion: once a keyed request to host B has PROVEN keyless works
// (keyed→503→retry-keyless→2xx), every later request for B goes keyless from
// the start, reaching the tunnel first try, and no control path is stranded.
//
// Evidence-based, not signal-based: we demote only after a keyless re-address
// SUCCEEDS, so a transient blip on a correctly-keyed host — whose keyless
// re-address would ALSO fail — never wrongly strands it. And we clear the
// demotion if a keyless request later returns wrong_replica (keying is no longer
// the problem, e.g. the host re-registered keyed after an upgrade). Per-host,
// in-memory, reset on reload — a routing-affinity hint; correctness never
// depends on it (the keyless re-address backstops a not-yet-demoted host).
const _keylessHosts = new Set<string>();

/** Record that host `hostId` should be addressed WITHOUT a key (its tunnels
 * live on the default replica). Called after a keyless re-address to it succeeds. */
export function markHostKeyless(hostId: string): void {
  _keylessHosts.add(hostId);
}

/** Whether `hostId` is demoted to keyless routing (see {@link markHostKeyless}). */
export function isHostKeyless(hostId: string): boolean {
  return _keylessHosts.has(hostId);
}

/** Clear a keyless demotion — a request we sent keyless (because demoted) still
 * came back wrong_replica, so keying wasn't the problem; re-evaluate by
 * letting the next request key normally. */
export function clearHostKeyless(hostId: string): void {
  _keylessHosts.delete(hostId);
}

// ── Modal host_id for host-less / cross-host routes ─────────────────────────
//
// Routes with no host in their URL or body (`/v1/hosts` list, the session list,
// `WS /v1/sessions/updates`) still carry a key so they route through the
// sharding layer instead of the server's implicit default, and tend to
// co-locate a user's traffic on the replica already holding the bulk of their
// sessions' tunnels (warm caches). The value is the MODAL host_id over every
// session seen this page (`_sessionHosts`) — the host backing the most of the
// user's sessions. It's a real host_id (not an opaque token): the whole client
// passes host_ids around, and the host_id → header translation happens in
// exactly one place (the request-header builder).
//
// RESOLVED ONCE — on the first SUCCESSFUL session-list settle — then never
// recomputed for the page's lifetime. The session list refetches continuously
// (poll + `/updates`-frame cache invalidations re-run `setSessionHost`), so
// `_sessionHosts` shifts all page long; recomputing the modal on every change
// would flap the value and, via `buildUpdatesUrl`, churn the `/updates`
// WebSocket. A hard reload starts a fresh module → a fresh pick. Correctness
// never depends on the guess: these routes serve from any replica, and a keyed
// request that guesses wrong self-heals via the wrong_replica keyless re-address
// in `authenticatedFetch`.
//
// The latch fires on a SUCCESS settle only — not an error or pre-data settle.
// The list queryFn seeds `_sessionHosts` from every row BEFORE it resolves, so
// by the time the query is `success` the map already holds this user's hosts
// (or is genuinely empty for a zero-session user, which correctly freezes a
// null / no-key fallback). Freezing on an error/empty-pre-data settle instead
// would pin the page to a null key even after real hosts later load, so the
// caller waits for success.
let _modalHostId: string | null = null;
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
 * first SUCCESSFUL settle of the session-list query — by then the queryFn has
 * seeded `_sessionHosts`, so an empty map means a genuinely zero-session user
 * (freeze a null / no-key fallback) rather than pre-data. The value is always a
 * real host_id observed on one of the user's sessions, or null; there is no
 * persisted-preference backstop, so a stale/sandbox-sentinel choice can never
 * become the slice key.
 */
export function resolveModalHost(): void {
  if (_modalHostResolved) return;
  _modalHostId = pickModalHost(_sessionHosts);
  _modalHostResolved = true;
}

/**
 * The frozen modal host_id, or `null` (→ send no key → default route) before
 * it is resolved or when no host could be picked. Read by `authenticatedFetch`
 * (host-less routes) and `buildUpdatesUrl`.
 */
export function modalHostId(): string | null {
  return _modalHostId;
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
