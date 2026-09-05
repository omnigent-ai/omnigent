/**
 * User identity discovery and request header injection.
 *
 * On app load, calls ``GET /v1/me`` to discover the current user.
 * All subsequent API calls use ``authenticatedFetch`` which injects
 * the ``X-Forwarded-Email`` header so session routes know who's
 * making the request.
 *
 * When OIDC or accounts auth is active, the server returns 401 with
 * a ``login_url`` if the user is unauthenticated. The frontend
 * redirects to that URL — for accounts mode this is the SPA route
 * ``/login`` (LoginPage), for OIDC it's the server-side
 * ``/auth/login`` redirect. In header mode the server reports no
 * ``login_url`` (single-user, no login), so 401s are never turned
 * into a login redirect.
 */

import { getCachedServerInfo } from "./capabilities";
import { getOmnigentHostConfig, hostFetch, isDatabricksWorkspace } from "./host";
import {
  clearHostKeyless,
  getSessionHost,
  isHostKeyless,
  markHostKeyless,
  modalHostId,
} from "@/lib/sessionHost";

// Single-user sentinel from `GET /v1/me` (server's RESERVED_USER_LOCAL);
// not a real actor, so never used as an author label.
const RESERVED_USER_LOCAL = "local";

let currentUserId: string | null = null;

// Replica-routing header, used when the server shards replicas by host. A
// host's control tunnel and its runners' tunnels register on the same replica,
// keyed by host_id, so a request scoped to a host (or a session running on one)
// must carry that host_id to reach the replica holding the tunnel.
const SLICE_KEY_HEADER = "X-Databricks-Omnigent-Slice-Key";

// Server error code (errors.py ErrorCode.WRONG_REPLICA) returned when a keyed
// request reached a replica that doesn't hold the session's host tunnel — the
// key doesn't match where the tunnel lives. The request is valid, just
// misrouted, so we re-address it ONCE without the key and route by the default.
// Distinct from "runner_unavailable" (the runner is offline everywhere), which
// no re-addressing can fix.
const WRONG_REPLICA_CODE = "wrong_replica";

/**
 * Classify a request URL for slice-key routing. A host-scoped request must key
 * by its OWN host (or send nothing) — never by the modal fallback, which would
 * route it to a guessed replica. Returns a discriminated result so the caller
 * can tell the two null-looking cases apart:
 *
 * - ``/v1/hosts/{host_id}/...`` → ``{scoped: true, hostId}`` from the path.
 * - ``/v1/sessions/{session_id}/...`` → ``{scoped: true, hostId}`` from
 *   {@link getSessionHost}; ``hostId`` is ``null`` when the session hasn't been
 *   loaded yet → send NO key (the modal host would be a wrong guess for a
 *   specific session; a keyless miss re-addresses instead).
 * - everything else (session lists, ``/v1/sessions/updates``, ``/health``) is a
 *   cross-host / DB-backed read → ``{scoped: false}``, where the modal host is a
 *   legitimate cache-affinity hint.
 *
 * The two host-scoped families are an ALLOWLIST of what exists today (verified:
 * ``/v1/sessions/{id}/agent`` is under the sessions prefix, and
 * ``/v1/runners/{id}`` isn't called from the client). A new host-scoped route
 * must be added here to key by its own host; the regression test pins the known
 * families so a matcher change is a conscious edit.
 */
type UrlHostScope = { scoped: true; hostId: string | null } | { scoped: false };

function hostScopeForUrl(url: string): UrlHostScope {
  const hostMatch = url.match(/\/v1\/hosts\/([^/?#]+)/);
  if (hostMatch) return { scoped: true, hostId: decodeURIComponent(hostMatch[1]) };
  const sessionMatch = url.match(/\/v1\/sessions\/([^/?#]+)/);
  if (sessionMatch)
    return { scoped: true, hostId: getSessionHost(decodeURIComponent(sessionMatch[1])) };
  return { scoped: false };
}

// ── Session host-resolve chokepoint ─────────────────────────────────────────
//
// A session's runner tunnel lives on the replica keyed by its host_id, so a
// session-scoped request must carry that key on the FIRST attempt. The key comes
// from the session→host map (`getSessionHost`), which is seeded when a session
// object is parsed. But on a fresh page load, ChatPage's resource hooks fire
// their requests (`/resources/terminals`, `/resources/environments/...`,
// `/items`, ...) concurrently with — and often before — the session snapshot
// that would seed the map. Those early requests would read a null host and fall
// back to the modal host (a best-effort guess = possibly the WRONG host) → land
// on the wrong replica → 400 wrong_replica → keyless re-address. That wastes a
// round-trip and, worse, conscripts the keyless fallback (whose real job is the
// genuine wrong-replica case: an old CLI that dialed its tunnel keyless) to paper
// over "we hadn't learned the host yet."
//
// So before a session SUB-path request whose host is still unknown, bootstrap the
// map from a host-AGNOSTIC session snapshot (GET /v1/sessions/{id}, which reads
// the conversation store and returns 200 on any replica), THEN let the request
// key correctly. This generalizes the same host-first-resolve `bindStream` already
// does for the SSE stream (store/chatStore.ts). The resolver is injected (not
// imported) so identity.ts keeps no import dependency on sessionsApi.ts, which
// imports `authenticatedFetch` from here — importing back would be a cycle.

/** Seeds the session→host map for one session id (registered at bootstrap). */
type SessionHostResolver = (sessionId: string) => Promise<void>;
let _sessionHostResolver: SessionHostResolver | null = null;

/**
 * Register the bootstrap host resolver (a thin wrapper over `getSessionSlim`,
 * wired at the app entry — main.tsx / embed.tsx — NOT in a data module, so unit
 * tests that partially-mock this module needn't stub the export). Null (the
 * default) disables the gate, so a caller that never registers — or any
 * pre-registration request — simply keeps the prior modal-fallback +
 * keyless-retry behavior.
 */
export function setSessionHostResolver(resolver: SessionHostResolver | null): void {
  _sessionHostResolver = resolver;
}

// Concurrent-dedup: the ~4 session-scoped requests ChatPage fires in one tick
// share ONE in-flight resolve (cleared on settle) rather than each firing a
// snapshot.
const _hostResolveInFlight = new Map<string, Promise<void>>();
// Sessions already bootstrapped this page (success OR failure). A genuinely
// hostless session keeps `getSessionHost(id) === null` forever, and a bad-id /
// transient failure would too — so without this guard every later sub-path
// request would re-resolve. host_id is fixed for a session's life, so one attempt
// per session per page load is enough.
const _hostResolveAttempted = new Set<string>();

// A session SUB-path: /v1/sessions/{id}/<something>. The `\/[^?#]` after the id
// (a slash then a real path char) is what separates a sub-path from the BARE
// snapshot the bootstrap itself GETs — /v1/sessions/{id} and
// /v1/sessions/{id}?query do NOT match, so the resolver can't recurse into
// itself. The list (/v1/sessions?query) and create (POST /v1/sessions) have no
// /{id} and also don't match.
const SESSION_SUBPATH_RE = /\/v1\/sessions\/([^/?#]+)\/[^?#]/;

/**
 * Bootstrap the session→host map for one session id so its host-scoped traffic
 * keys correctly on the first attempt. Returns the in-flight resolve to await,
 * or `null` when there's nothing to do — callers only await a non-null result.
 *
 * No-op (returns `null`) unless a host fetcher is installed AND a resolver is
 * registered, or when the host is already known / was already attempted this
 * page. Best-effort: a resolver throw (bad id / transient) leaves the map
 * unseeded and callers fall through to their modal/keyless path.
 *
 * Concurrent callers in one tick share ONE resolve: the first creates the
 * promise synchronously through the `.set()`, the rest read and await it. Both
 * the HTTP chokepoint ({@link beginSessionHostResolve}) and the terminal-attach
 * WS route through here, so a terminal that mounts alongside its first HTTP
 * request joins that request's resolve rather than firing a second snapshot.
 */
export function resolveSessionHost(sessionId: string): Promise<void> | null {
  if (!getOmnigentHostConfig().fetcher || _sessionHostResolver === null) return null;
  // Host already known (warm map from the list-seed / a prior resolve), or we
  // already tried once this page — nothing to do.
  if (getSessionHost(sessionId) !== null || _hostResolveAttempted.has(sessionId)) return null;
  let inFlight = _hostResolveInFlight.get(sessionId);
  if (inFlight === undefined) {
    const resolver = _sessionHostResolver;
    inFlight = resolver(sessionId)
      .catch(() => {
        // Best-effort — leave the map unseeded and fall through to modal/keyless.
      })
      .finally(() => {
        _hostResolveAttempted.add(sessionId);
        _hostResolveInFlight.delete(sessionId);
      });
    _hostResolveInFlight.set(sessionId, inFlight);
  }
  return inFlight;
}

/**
 * Before a session sub-path request whose host is unknown, bootstrap the
 * session→host map so the key stamps correctly on the first attempt. Returns
 * the in-flight resolve to await, or `null` when there's nothing to do — the
 * caller only awaits a non-null result, so the common no-op path adds NO
 * microtask hop (a fetch still dispatches synchronously, as before the gate).
 */
function beginSessionHostResolve(url: string): Promise<void> | null {
  const match = url.match(SESSION_SUBPATH_RE);
  if (match === null) return null;
  return resolveSessionHost(decodeURIComponent(match[1]));
}

// Requests whose target host lives in a JSON body, not the URL, so
// {@link hostScopeForUrl} can't see it: the session create (POST /v1/sessions)
// and the host-mediated local import (POST /v1/imports/local[/stream]).
const BODY_HOST_KEYED_RE = /\/v1\/(?:sessions(?:\?|$)|imports\/local(?:[/?]|$))/;

/**
 * Recover the target host_id from a JSON request body for a body-host-keyed
 * request ({@link isBodyHostKeyedRequest}), or null when the body names none.
 *
 * These requests carry the host in the body, not the URL, so left unkeyed they
 * round-robin and can miss the replica holding that host's runner tunnel. A
 * session create fails "runner is offline" (the server notifies the runner
 * inline over its pod-local tunnel); an import fails "host is not connected"
 * (the import reads the host's transcripts over that same tunnel). Keying by the
 * body host_id pins both to the right replica like every other host-scoped
 * request. Bundled (multipart) creates are hostless here (non-string body) and a
 * managed-sandbox create carries ``host_type`` but no ``host_id``; both yield
 * null → unkeyed (see {@link isBodyHostKeyedRequest}).
 */
function hostIdFromBody(url: string, body: BodyInit | null | undefined): string | null {
  if (typeof body !== "string") return null;
  if (!BODY_HOST_KEYED_RE.test(url)) return null;
  try {
    const hostId = (JSON.parse(body) as { host_id?: unknown }).host_id;
    return typeof hostId === "string" && hostId ? hostId : null;
  } catch {
    return null;
  }
}

/**
 * Whether this request is keyable only by its own body host_id, never the modal.
 *
 * The modal host is a cache-affinity hint for reads, but these requests have a
 * side effect pinned to a replica and must key by their OWN target host:
 *
 * - A session create's managed launch task lives on whichever pod served it and
 *   needs the new host's tunnel in its LOCAL registry to send
 *   ``host.launch_runner``. A managed create carries no ``host_id`` (the host
 *   doesn't exist yet) → null → unkeyed, landing on the same default replica the
 *   host will dial back to. A create that NAMES a host keeps that key.
 * - A local import reads the chosen host's transcripts over its tunnel, which is
 *   registered on the replica keyed by that host_id — never the importing user's
 *   modal host (null / a different host for a fresh user), which is why an
 *   unkeyed import lands off-replica and 409s "host is not connected".
 *
 * A bundled (multipart) create has a non-string body and is not matched: it only
 * writes rows and binds the runner via a separate ``POST /v1/hosts/{id}/runners``
 * (keyed from the URL), so it spawns no replica-pinned work and may ride the modal.
 */
function isBodyHostKeyedRequest(url: string, body: BodyInit | null | undefined): boolean {
  return typeof body === "string" && BODY_HOST_KEYED_RE.test(url);
}

// Admin flag from the same `/v1/me` probe. Mode-agnostic (the shared
// `users.is_admin` column), so the SPA can gate admin chrome in EVERY
// auth mode — including OIDC/SSO, where the accounts-only `/auth/me`
// endpoint doesn't exist. Defaults false until the probe resolves.
let currentIsAdmin = false;
let identityResolved = false;
let identityPromise: Promise<string | null> | null = null;
// Cache the server-provided login URL on the first /v1/me probe so
// later session-expiry redirects in authenticatedFetch hit the right
// path per provider — "/login" for accounts, "/auth/login" for OIDC.
// Hardcoding "/login" here previously sent OIDC users to an accounts
// password form that had no connection to their IdP.
let serverLoginUrl: string | null = null;
// Set the moment we hand the browser to the login page. Assigning
// `location.href` starts a navigation but does NOT stop this document:
// requests already in flight keep landing, and every 401 among them used
// to re-assign the same URL, so one logged-out page load queued ~28
// navigations. Boot also reads this to skip mounting the app when the
// session is already on its way out (see `isLoginRedirectPending`).
let loginRedirectPending = false;

/**
 * Hand the browser to `loginUrl`, at most once per document.
 *
 * :param loginUrl: Login path from the capabilities probe or `/v1/me`.
 * :returns: True if this call started the navigation, False if one was
 *     already under way.
 */
function redirectToLogin(loginUrl: string): boolean {
  if (loginRedirectPending) return false;
  loginRedirectPending = true;
  const returnTo = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `${loginUrl}?return_to=${returnTo}`;
  return true;
}

/**
 * Whether a login redirect is under way.
 *
 * The navigation is asynchronous, so this document keeps running until
 * the login page commits. Boot checks this before mounting React —
 * otherwise the app renders and fans its queries out against a session
 * we already know is invalid, and each one 401s behind the pending
 * navigation. Always false in header mode, which has no login page.
 */
export function isLoginRedirectPending(): boolean {
  return loginRedirectPending;
}

/**
 * Whether the current page IS the login or register page, so we
 * shouldn't trigger another redirect on top of it. Without this,
 * an unauthed user landing on ``/login`` would hit /v1/me → 401 →
 * redirect to /login → reload → redirect → infinite loop. Same
 * for ``/register?invite=...`` — invitees redeeming an invite
 * arrive unauthed by design.
 *
 * Matches both the SPA routes (``/login``, ``/register``) and the
 * OIDC server-side path (``/auth/login``) so the guard covers
 * every mode.
 */
function isOnLoginPath(): boolean {
  const path = window.location.pathname;
  return path === "/login" || path === "/register" || path.startsWith("/auth/login");
}

/**
 * Fetch the current user identity from the server.
 * Called once on app load; subsequent calls return the cached value.
 *
 * When the server returns 401 with a ``login_url`` (OIDC mode),
 * redirects the browser to the login page.
 */
export async function resolveIdentity(): Promise<string | null> {
  if (identityResolved) return currentUserId;
  if (identityPromise) return identityPromise;
  identityPromise = (async () => {
    try {
      const res = await hostFetch("/v1/me");
      if (res.status === 401) {
        // OIDC / accounts mode: server requires authentication.
        // Redirect to the login URL provided in the response body —
        // unless we're already there (avoid an infinite reload loop
        // when the LoginPage itself calls resolveIdentity).
        try {
          const data = (await res.json()) as {
            user_id: null;
            login_url?: string;
          };
          if (data.login_url) {
            serverLoginUrl = data.login_url;
            if (!isOnLoginPath()) {
              redirectToLogin(data.login_url);
              return null;
            }
          }
        } catch {
          // Response body was not JSON — fall through.
        }
      }
      if (res.ok) {
        const data = (await res.json()) as {
          user_id: string | null;
          is_admin?: boolean;
        };
        currentUserId = data.user_id;
        currentIsAdmin = data.is_admin ?? false;
      }
    } catch {
      // Server unreachable — leave as null.
    }
    identityResolved = true;
    return currentUserId;
  })();
  return identityPromise;
}

/** Return the cached user ID (null before resolveIdentity completes). */
export function getCurrentUserId(): string | null {
  return currentUserId;
}

/**
 * Whether the current user is an admin, per the `/v1/me` probe.
 * Mode-agnostic — usable to gate admin chrome under header, accounts,
 * AND OIDC. Returns false before `resolveIdentity` completes.
 */
export function getCurrentIsAdmin(): boolean {
  return currentIsAdmin;
}

/**
 * Viewer id for labeling own optimistic bubbles, the client analog of
 * the server's `attribution_user`. Returns null before identity
 * resolves and for the `"local"` sentinel, so those stay unlabeled.
 */
export function getCurrentAuthorId(): string | null {
  if (currentUserId === null || currentUserId === RESERVED_USER_LOCAL) {
    return null;
  }
  return currentUserId;
}

/**
 * Fetch wrapper that injects ``X-Forwarded-Email`` on every request.
 * Drop-in replacement for ``window.fetch`` — same signature.
 *
 * When a request returns 401 (session expired in OIDC mode),
 * redirects to the login page.
 */
/**
 * Whether a response is the server's wrong-replica signal
 * ({@link WRONG_REPLICA_CODE}). Gated on 400 first so the body is only read
 * on the rare failure; reads a CLONE so the original response body stays intact
 * for the caller when this returns false.
 *
 * @param res The response to inspect.
 * @returns `true` when the JSON body carries `error.code === "wrong_replica"`.
 */
async function _isWrongReplica(res: Response): Promise<boolean> {
  if (res.status !== 400) return false;
  try {
    const body = (await res.clone().json()) as { error?: { code?: string } };
    return body.error?.code === WRONG_REPLICA_CODE;
  } catch {
    // Non-JSON / empty body — not the structured wrong_replica error.
    return false;
  }
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (currentUserId && currentUserId !== RESERVED_USER_LOCAL && !headers.has("X-Forwarded-Email")) {
    headers.set("X-Forwarded-Email", currentUserId);
  }
  // Pin host- and session-scoped requests to the replica holding that host's
  // runner tunnel (key = host_id). Derived centrally so no call site has to
  // thread it; a caller that set the header explicitly wins, and non-host-scoped
  // requests get no key (any replica). Only against a Databricks workspace-hosted
  // server — the embedded (managed) UI, or `npm run dev` pointed at a workspace URL.
  // A standalone/self-hosted server has no Dicer, so the key would just dirty
  // its logs (see isDatabricksWorkspace).
  const url = typeof input === "string" ? input : input.toString();
  // Resolve this session's host BEFORE deriving the slice key below, so a fresh
  // session sub-path request keys to the right replica on the first attempt
  // instead of guessing the modal host and self-healing via the keyless retry.
  // Null (the common case: warm map, non-session route, or standalone) means no
  // bootstrap is needed — we skip the await so the fetch still dispatches in the
  // same tick, exactly as before the gate.
  const hostResolve = beginSessionHostResolve(url);
  if (hostResolve !== null) await hostResolve;
  // Whether WE (not the caller) stamped the slice key on this request; only then
  // is a keyless re-address meaningful on a wrong-replica (wrong_replica) response.
  let stampedSliceKey = false;
  // The host this request is FOR, even when we deliberately send it keyless
  // (demoted). Lets the retry logic below demote/un-demote the right host.
  let derivedHostId: string | null = null;
  if (!headers.has(SLICE_KEY_HEADER) && isDatabricksWorkspace()) {
    // Key by the request's OWN host when it's host-scoped; otherwise (a
    // cross-host / DB-backed read) fall back to the modal host as a
    // cache-affinity hint. The distinction matters: a host-scoped request whose
    // host isn't known yet must send NO key rather than the modal — the modal
    // is the wrong guess for a SPECIFIC session, so it would route to a replica
    // that doesn't hold the tunnel; a keyless miss re-addresses instead. Only
    // an unscoped route (list / updates) may ride the modal — a JSON request
    // whose target host is in the body may not (see {@link isBodyHostKeyedRequest}):
    // it is keyable ONLY by that body host_id (a managed create with none stays
    // unkeyed).
    const scope = hostScopeForUrl(url);
    if (scope.scoped) {
      derivedHostId = scope.hostId;
    } else if (isBodyHostKeyedRequest(url, init?.body)) {
      derivedHostId = hostIdFromBody(url, init?.body);
    } else {
      derivedHostId = modalHostId();
    }
    // Skip keying a host we've already PROVEN routes keyless (its tunnels dialed
    // in without a key → they live on the default replica). This spares the long
    // tail of control paths (approve/stop/interrupt/…) from each needing a
    // server-side wrong-replica guard: once demoted, every request for this host
    // goes keyless from the start and reaches the tunnel first try. A demoted
    // host that still returns wrong_replica keyless is un-demoted below.
    if (derivedHostId && !isHostKeyless(derivedHostId)) {
      headers.set(SLICE_KEY_HEADER, derivedHostId);
      stampedSliceKey = true;
    }
  }
  // Bypass the browser HTTP cache for all API calls. Session
  // endpoints (GET /v1/sessions/{id}) carry volatile in-memory state
  // (pending_elicitations) that changes between fetches without any
  // URL change. Without no-store the browser may serve a stale
  // cached response — e.g. one captured before an elicitation was
  // published — causing the ApprovalCard to vanish on navigate-back.
  let res = await hostFetch(url, {
    ...init,
    headers,
    cache: "no-store",
  });

  // Wrong-replica fallback: a keyed request that reached the wrong replica comes
  // back 400 wrong_replica. Re-address ONCE with the key removed so it routes by
  // the default (client and host may be out of sync on which sharding strategy
  // the host registered under, so we can't know up front — try keyed, then fall
  // back). Only when WE stamped the key; a genuinely-offline runner returns
  // runner_unavailable and is not re-addressed here.
  if (stampedSliceKey && (await _isWrongReplica(res))) {
    // Fresh Headers for the retry — mutating the first request's `headers`
    // object in place would also clear the key on the already-sent request
    // (callers/tests hold it by reference).
    const retryHeaders = new Headers(headers);
    retryHeaders.delete(SLICE_KEY_HEADER);
    res = await hostFetch(url, {
      ...init,
      headers: retryHeaders,
      cache: "no-store",
    });
    // Sticky demotion: the keyless re-address PROVED this host routes keyless
    // (the keyed attempt returned wrong_replica, the keyless one didn't).
    // Remember it so every later request for this host — including the control
    // paths with no server-side wrong-replica guard — goes keyless from the
    // start. Evidence-based: we demote only on a keyless SUCCESS, so a
    // correctly-keyed host having a transient blip (whose keyless re-address
    // would also fail) is never stranded.
    if (derivedHostId && !(await _isWrongReplica(res))) {
      markHostKeyless(derivedHostId);
    }
  } else if (
    derivedHostId &&
    !stampedSliceKey &&
    isHostKeyless(derivedHostId) &&
    (await _isWrongReplica(res))
  ) {
    // We sent this keyless BECAUSE the host was demoted, yet it still came back
    // wrong_replica — so keying was not the problem (e.g. the host re-registered
    // keyed after a CLI upgrade). Clear the demotion so the next request
    // re-evaluates by trying keyed again.
    clearHostKeyless(derivedHostId);
  }

  if (
    // When embedded, the host owns auth (e.g. cookie/session via
    // workspaceFetch) and a 401 should surface to the caller, not
    // trigger web's standalone OIDC redirect.
    !getOmnigentHostConfig().fetcher &&
    res.status === 401 &&
    !input.toString().includes("/v1/me") &&
    !input.toString().includes("/auth/") &&
    !isOnLoginPath()
  ) {
    // Session expired or cookie invalid — redirect to login IFF the
    // server actually has a login page. Don't redirect on /auth/*
    // paths (the LoginPage POSTs /auth/login and handles 401 itself)
    // or when we're already on a login page (avoid the loop).
    //
    // Source the login URL from the capabilities probe (/v1/info →
    // login_url): "/login" for accounts, "/auth/login" for OIDC, and
    // **null for header mode (no login)**. In header mode a stray 401
    // must NOT bounce the user to a phantom /login form — header is
    // the default for a bare local server, so we surface the 401 to
    // the caller instead. (serverLoginUrl from the /v1/me probe is a
    // fallback for the brief window before capabilities resolves.)
    // Once-only (see redirectToLogin): a burst of 401s from one page load
    // all reach here, and re-assigning the same URL per failure just piles
    // up navigations to a page we're already going to.
    const loginUrl = getCachedServerInfo()?.login_url ?? serverLoginUrl;
    if (loginUrl) redirectToLogin(loginUrl);
  }
  return res;
}
