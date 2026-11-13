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

import { stripBasePath, withBasePath } from "./basePath";
import { isAdminViewEnabled } from "./adminView";
import { getCachedServerInfo } from "./capabilities";
import {
  getOmnigentHostConfig,
  hostFetch,
  isDatabricksWorkspace,
  isHostRoutingEnabled,
} from "./host";
import { clientRoutingKey, SLICE_KEY_HEADER } from "./routingKey";
import {
  clearHostKeyless,
  getSessionHost,
  isHostKeyless,
  markHostKeyless,
  updatesRoutingKey,
} from "@/lib/sessionHost";

// Single-user sentinel from `GET /v1/me` (server's RESERVED_USER_LOCAL);
// not a real actor, so never used as an author label.
const RESERVED_USER_LOCAL = "local";

let currentUserId: string | null = null;

// The request is valid but reached a replica without its host tunnel.
// Unlike runner_unavailable, wrong_replica can recover by re-addressing.
export const WRONG_REPLICA_CODE = "wrong_replica";

/**
 * Classify a request URL for slice-key routing. A host-scoped request must key
 * by its OWN host (or send nothing) — never by the modal fallback, which would
 * route it to a guessed replica. Returns a discriminated result so the caller
 * can tell the two null-looking cases apart:
 *
 * - ``/v1/hosts/{host_id}/...`` → ``{scoped: true, hostId}`` from the path.
 * - ``/v1/sessions/{session_id}/...`` → ``{scoped: true, hostId}`` from
 *   {@link getSessionHost}; ``hostId`` is ``null`` when the session hasn't been
 *   loaded yet → send the client's own key, not the modal host (a wrong guess
 *   for a specific session); a wrong_replica miss resolves the host and
 *   re-addresses.
 * - ``/v1/skills`` → the query's ``host_id`` or the host for ``session_id``.
 * - everything else (session lists, ``/v1/sessions/projects``,
 *   ``/v1/sessions/updates``, ``/health``) is a cross-host / DB-backed read →
 *   ``{scoped: false}``, which any replica can serve.
 *
 * The host-scoped families are an ALLOWLIST of what exists today (verified:
 * ``/v1/sessions/{id}/agent`` is under the sessions prefix, and
 * ``/v1/runners/{id}`` isn't called from the client). A new host-scoped route
 * must be added here to key by its own host; the regression test pins the known
 * families so a matcher change is a conscious edit.
 */
type UrlHostScope = { scoped: true; hostId: string | null } | { scoped: false };

// Routes under /v1/sessions/ whose next segment is not a session id. Read as a
// session id, they would look up a host that can't exist.
const SESSION_COLLECTION_ROUTES = new Set(["orchestrated", "projects", "updates"]);

function skillsParamsForUrl(url: string): URLSearchParams | null {
  const match = url.match(/\/v1\/skills(?:\?([^#]*))?(?:#.*)?$/);
  return match ? new URLSearchParams(match[1] ?? "") : null;
}

function hostScopeForUrl(url: string): UrlHostScope {
  const skills = skillsParamsForUrl(url);
  if (skills) {
    const sessionId = skills.get("session_id");
    return {
      scoped: true,
      hostId: sessionId ? getSessionHost(sessionId) : skills.get("host_id") || null,
    };
  }
  const hostMatch = url.match(/\/v1\/hosts\/([^/?#]+)/);
  if (hostMatch) return { scoped: true, hostId: decodeURIComponent(hostMatch[1]) };
  const sessionMatch = url.match(/\/v1\/sessions\/([^/?#]+)/);
  if (sessionMatch) {
    const sessionId = decodeURIComponent(sessionMatch[1]);
    if (!SESSION_COLLECTION_ROUTES.has(sessionId))
      return { scoped: true, hostId: getSessionHost(sessionId) };
  }
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

/** Options for resolving a session's routing host. */
export interface SessionHostResolveOptions {
  /** Refresh a hostless snapshot after a managed host has been assigned. */
  force?: boolean;
}

type SessionHostResolver = (
  sessionId: string,
  options?: SessionHostResolveOptions,
) => Promise<void>;
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
const _hostRefreshInFlight = new Map<string, Promise<void>>();
// Avoid repeated lookups for hostless sessions. A managed launch can assign
// its host later, so an explicit routing miss may force a fresh lookup.
const _hostResolveAttempted = new Set<string>();

// A session SUB-path: /v1/sessions/{id}/<something>. The `\/[^?#]` after the id
// (a slash then a real path char) is what separates a sub-path from the BARE
// snapshot the bootstrap itself GETs — /v1/sessions/{id} and
// /v1/sessions/{id}?query do NOT match, so the resolver can't recurse into
// itself. The list (/v1/sessions?query) and create (POST /v1/sessions) have no
// /{id} and also don't match.
const SESSION_SUBPATH_RE = /\/v1\/sessions\/([^/?#]+)\/[^?#]/;
const SESSION_ROOT_RE = /\/v1\/sessions\/([^/?#]+)(?:[?#]|$)/;

/**
 * The session whose host a wrong_replica miss should refresh: a sub-path's, or a
 * root mutation's (e.g. an archive PATCH). A root GET is the host lookup itself,
 * so it never triggers another lookup.
 */
function sessionIdForRecovery(url: string, method: string | undefined): string | null {
  const subpathSessionId = sessionIdForHostResolve(url);
  if (subpathSessionId !== null) return subpathSessionId;
  const verb = (method ?? "GET").toUpperCase();
  if (verb === "GET" || verb === "HEAD") return null;
  const match = url.match(SESSION_ROOT_RE);
  if (match === null) return null;
  const sessionId = decodeURIComponent(match[1]);
  return SESSION_COLLECTION_ROUTES.has(sessionId) ? null : sessionId;
}

/**
 * Bootstrap the session→host map for one session id so its host-scoped traffic
 * keys correctly on the first attempt. Returns the in-flight resolve to await,
 * or `null` when there's nothing to do — callers only await a non-null result.
 *
 * Normal lookups require an embedded fetcher or OSS host routing and run once
 * per session/page. A routing miss may force a fresh hostless lookup, including
 * workspace dev mode. Known hosts are reused; resolver failures are ignored.
 *
 * Concurrent callers in one tick share ONE resolve: the first creates the
 * promise synchronously through the `.set()`, the rest read and await it. Both
 * the HTTP chokepoint ({@link beginSessionHostResolve}) and the terminal-attach
 * WS route through here, so a terminal that mounts alongside its first HTTP
 * request joins that request's resolve rather than firing a second snapshot.
 */
export function resolveSessionHost(
  sessionId: string,
  options: SessionHostResolveOptions = {},
): Promise<void> | null {
  const canResolve =
    getOmnigentHostConfig().fetcher != null ||
    (isDatabricksWorkspace() ? options.force === true : isHostRoutingEnabled());
  if (_sessionHostResolver === null || !canResolve) return null;
  if (getSessionHost(sessionId) !== null) return null;
  const refresh = _hostRefreshInFlight.get(sessionId);
  if (refresh !== undefined) return refresh;
  if (!options.force && _hostResolveAttempted.has(sessionId)) return null;
  const pending = options.force ? _hostRefreshInFlight : _hostResolveInFlight;
  let inFlight = pending.get(sessionId);
  if (inFlight === undefined) {
    const resolver = _sessionHostResolver;
    // A pre-provisioning lookup may still be running; finish it before forcing
    // a fresh read, while concurrent routing misses share this refresh.
    const lookup = options.force
      ? Promise.resolve(_hostResolveInFlight.get(sessionId)).then(() =>
          resolver(sessionId, { force: true }),
        )
      : resolver(sessionId);
    inFlight = lookup
      .then(() => {
        _hostResolveAttempted.add(sessionId);
      })
      .catch(() => {
        // Best-effort — leave the map unseeded and fall through to modal/keyless.
        // Keep managed lookups one-shot; OSS retries on the next request.
        if (isDatabricksWorkspace()) {
          _hostResolveAttempted.add(sessionId);
        }
      })
      .finally(() => {
        pending.delete(sessionId);
      });
    pending.set(sessionId, inFlight);
  }
  return inFlight;
}

function sessionIdForHostResolve(url: string): string | null {
  const skillsSessionId = skillsParamsForUrl(url)?.get("session_id");
  if (skillsSessionId) return skillsSessionId;
  const match = url.match(SESSION_SUBPATH_RE);
  return match === null ? null : decodeURIComponent(match[1]);
}

/**
 * Before a session sub-path request whose host is unknown, bootstrap the
 * session→host map so the key stamps correctly on the first attempt. Returns
 * the in-flight resolve to await, or `null` when there's nothing to do — the
 * caller only awaits a non-null result, so the common no-op path adds NO
 * microtask hop (a fetch still dispatches synchronously, as before the gate).
 */
function beginSessionHostResolve(url: string, method: string | undefined): Promise<void> | null {
  const sessionId = sessionIdForRecovery(url, method);
  return sessionId === null ? null : resolveSessionHost(sessionId);
}

// Requests whose target host lives in a JSON body, not the URL, so
// {@link hostScopeForUrl} can't see it: the session create (POST /v1/sessions)
// and the host-mediated local import (POST /v1/imports/local[/stream]).
const BODY_HOST_KEYED_RE = /\/v1\/(?:sessions(?:\?|$)|imports\/local(?:[/?]|$))/;
// A managed fork, or a move onto a new sandbox, keys like a managed create: its
// launch must run where its own new host connects, never on the source
// session's replica.
const FORK_RE = /\/v1\/sessions\/[^/?#]+\/fork(?:\?|$)/;
const MANAGED_HOST_MOVE_RE = /\/v1\/sessions\/[^/?#]+\/managed-host(?:\?|$)/;

function isManagedLaunchRequest(url: string, body: BodyInit | null | undefined): boolean {
  if (typeof body !== "string") return false;
  if (MANAGED_HOST_MOVE_RE.test(url)) return true;
  if (!FORK_RE.test(url)) return false;
  try {
    return (JSON.parse(body) as { host_type?: unknown }).host_type === "managed";
  } catch {
    return false;
  }
}

/**
 * Recover the target host_id from a JSON request body for a body-host-keyed
 * request ({@link isBodyHostKeyedRequest}), a managed fork, or a move onto a
 * new sandbox, or null when the body names none.
 *
 * These requests carry the host in the body, not the URL, so left unkeyed they
 * round-robin and can miss the replica holding that host's runner tunnel. A
 * session create fails "runner is offline" (the server notifies the runner
 * inline over its pod-local tunnel); an import fails "host is not connected"
 * (the import reads the host's transcripts over that same tunnel). Keying by the
 * body host_id pins both to the right replica like every other host-scoped
 * request. A managed-sandbox create's ``host_id`` is the id it chose for its
 * new sandbox host, so the launch runs where that host's tunnel will land.
 * A managed fork's or move's ``host_id`` is the same choice. Multipart creates
 * carry the same routing fields in their metadata part. Creates, forks and moves
 * without a host ID carry the client's own key.
 */
function hostIdFromBody(url: string, body: BodyInit | null | undefined): string | null {
  const metadata = body instanceof FormData ? body.get("metadata") : body;
  if (typeof metadata !== "string") return null;
  if (!BODY_HOST_KEYED_RE.test(url) && !FORK_RE.test(url) && !MANAGED_HOST_MOVE_RE.test(url)) {
    return null;
  }
  try {
    const hostId = (JSON.parse(metadata) as { host_id?: unknown }).host_id;
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
 * - A managed create keys by the host_id it chose for its sandbox host. One
 *   without a host_id carries the client's own key: if the new host connects
 *   elsewhere, its first message must resolve the assigned host and re-address
 *   after wrong_replica. A create that names a host keeps that key.
 * - A local import reads the chosen host's transcripts over its tunnel, which is
 *   registered on the replica keyed by that host_id — never the importing user's
 *   modal host (null / a different host for a fresh user), which is why an
 *   unkeyed import lands off-replica and 409s "host is not connected".
 *
 * Multipart creates can launch on a host during creation, so they use the same
 * routing rule as JSON creates.
 */
function isBodyHostKeyedRequest(url: string, body: BodyInit | null | undefined): boolean {
  return (typeof body === "string" || body instanceof FormData) && BODY_HOST_KEYED_RE.test(url);
}

const SCHEDULED_TASK_WRITE_RE = /\/v1\/scheduled-tasks(?:\/[^/?#]+)?(?:[?#]|$)/;

// A scheduled-task create or edit naming a connected host is validated against
// that host's workspace over its tunnel, so it routes to the host's replica.
function scheduledTaskHostId(url: string, init: RequestInit | undefined): string | null {
  const verb = (init?.method ?? "GET").toUpperCase();
  if ((verb !== "POST" && verb !== "PATCH") || typeof init?.body !== "string") return null;
  if (!SCHEDULED_TASK_WRITE_RE.test(url)) return null;
  try {
    const hostId = (JSON.parse(init.body) as { host_id?: unknown }).host_id;
    return typeof hostId === "string" && hostId ? hostId : null;
  } catch {
    return null;
  }
}

// Admin flag from the same `/v1/me` probe. Mode-agnostic (the shared
// `users.is_admin` column), so the SPA can gate admin chrome in EVERY
// auth mode — including OIDC/SSO, where the accounts-only `/auth/me`
// endpoint doesn't exist. Defaults false until the probe resolves.
let currentIsAdmin = false;
let identityResolved = false;
let identityPromise: Promise<string | null> | null = null;
// A `/v1/me` that fails in transit says nothing about the viewer, so it is
// retried rather than cached. Retries run only while `subscribeIdentity` has
// listeners, so an unwatched probe never leaves a timer behind.
const IDENTITY_RETRY_INITIAL_MS = 1_000;
const IDENTITY_RETRY_MAX_MS = 30_000;
const identityListeners = new Set<() => void>();
let identityRetryPending = false;
let identityRetryDelayMs = IDENTITY_RETRY_INITIAL_MS;
let identityRetryTimer: ReturnType<typeof setTimeout> | null = null;
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
  window.location.href = `${withBasePath(loginUrl)}?return_to=${returnTo}`;
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
  // Compare against base-relative paths so the guard still recognizes the
  // login/register pages when served under a subpath proxy (e.g.
  // `/proxy/6767/login`).
  const path = stripBasePath(window.location.pathname);
  return path === "/login" || path === "/register" || path.startsWith("/auth/login");
}

/**
 * Whether a non-2xx `/v1/me` status may change on retry. A 401 or another
 * 4xx is the server's answer (signed out, route missing); 5xx, 408, and 429
 * come from an overloaded server or the gateway in front of it.
 */
function isTransientIdentityStatus(status: number): boolean {
  return status >= 500 || status === 408 || status === 429;
}

/**
 * Probe ``GET /v1/me`` once and record the identity it reports.
 *
 * :returns: True when the outcome is final, False when the request failed
 *     in a way a retry can fix.
 */
async function probeIdentity(): Promise<boolean> {
  try {
    const res = await hostFetch("/v1/me", undefined, { hostless: true });
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
          }
        }
      } catch {
        // Response body was not JSON — still a signed-out answer.
      }
      return true;
    }
    if (res.ok) {
      const data = (await res.json()) as {
        user_id: string | null;
        is_admin?: boolean;
      };
      currentUserId = data.user_id;
      currentIsAdmin = data.is_admin ?? false;
      return true;
    }
    return !isTransientIdentityStatus(res.status);
  } catch {
    // Server unreachable, connection dropped, or the body was cut off.
    return false;
  }
}

function cancelIdentityRetry(): void {
  if (identityRetryTimer === null) return;
  clearTimeout(identityRetryTimer);
  identityRetryTimer = null;
}

function scheduleIdentityRetry(): void {
  if (!identityRetryPending || identityRetryTimer !== null || identityListeners.size === 0) {
    return;
  }
  identityRetryTimer = setTimeout(() => {
    identityRetryTimer = null;
    // Grow the delay only when a retry runs; a cancelled timer made no request.
    identityRetryDelayMs = Math.min(identityRetryDelayMs * 2, IDENTITY_RETRY_MAX_MS);
    void resolveIdentity();
  }, identityRetryDelayMs);
}

/**
 * Fetch the current user identity from the server.
 * Called once on app load; subsequent calls return the cached value.
 * A transient failure resolves to null without being cached: the next
 * call probes again, and while {@link subscribeIdentity} has listeners
 * the probe is retried with backoff.
 *
 * When the server returns 401 with a ``login_url`` (OIDC mode),
 * redirects the browser to the login page.
 */
export async function resolveIdentity(): Promise<string | null> {
  if (identityResolved) return currentUserId;
  if (identityPromise) return identityPromise;
  const attempt = probeIdentity().then((final) => {
    identityPromise = null;
    if (final) {
      identityResolved = true;
      identityRetryPending = false;
      cancelIdentityRetry();
    } else {
      identityRetryPending = true;
      scheduleIdentityRetry();
    }
    for (const listener of identityListeners) listener();
    return currentUserId;
  });
  identityPromise = attempt;
  return attempt;
}

/**
 * Watch the viewer identity. The listener runs after each ``/v1/me`` probe
 * settles; while any listener is attached, a transient failure is retried
 * with backoff.
 *
 * :param listener: Called with no arguments; read {@link getCurrentUserId}.
 * :returns: A function that detaches the listener.
 */
export function subscribeIdentity(listener: () => void): () => void {
  identityListeners.add(listener);
  scheduleIdentityRetry();
  return () => {
    identityListeners.delete(listener);
    if (identityListeners.size === 0) cancelIdentityRetry();
  };
}

/** Return the cached user ID (null before resolveIdentity completes). */
export function getCurrentUserId(): string | null {
  return currentUserId;
}

/**
 * Whether the current user has admin access enabled in this browser.
 * Mode-agnostic — usable to gate admin chrome under header, accounts,
 * AND OIDC. Returns false before `resolveIdentity` completes.
 */
export function getCurrentIsAdmin(): boolean {
  return currentIsAdmin && (getCachedServerInfo()?.admin_view === false || isAdminViewEnabled());
}

/** The account's role, used to keep the admin-view switch available while off. */
export function getCurrentAdminEligibility(): boolean {
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
  // Route host-scoped requests with that host's key; other requests may use
  // the modal host for cache affinity. An explicit caller header wins.
  // OSS ingress deployments opt in to this routing.
  const url = typeof input === "string" ? input : input.toString();
  // Resolve this session's host BEFORE deriving the slice key below, so a fresh
  // session sub-path request keys to the right replica on the first attempt
  // instead of guessing the modal host and self-healing via the keyless retry.
  // Null (the common case: warm map, non-session route, or standalone) means no
  // bootstrap is needed — we skip the await so the fetch still dispatches in the
  // same tick, exactly as before the gate.
  const hostResolve = beginSessionHostResolve(url, init?.method);
  if (hostResolve !== null) await hostResolve;
  // Whether WE (not the caller) stamped a host's slice key on this request; only
  // then is a keyless re-address meaningful on a wrong-replica (wrong_replica) response.
  let stampedSliceKey = false;
  // The host this request is FOR, even when we deliberately send it keyless
  // (demoted). Lets the retry logic below demote/un-demote the right host.
  let derivedHostId: string | null = null;
  // Sent without a key because its host's tunnels connected without one.
  let unkeyed = false;
  // Names no host (lists, probes, discovery writes). It is keyed by the modal
  // host, and a wrong_replica reply to it is returned as-is.
  let hostless = false;
  const callerKeyed = headers.has(SLICE_KEY_HEADER);
  if (!callerKeyed && isHostRoutingEnabled()) {
    const scope = hostScopeForUrl(url);
    if (isManagedLaunchRequest(url, init?.body)) {
      derivedHostId = hostIdFromBody(url, init?.body);
    } else if (scope.scoped) {
      derivedHostId = scope.hostId;
    } else if (isBodyHostKeyedRequest(url, init?.body)) {
      derivedHostId = hostIdFromBody(url, init?.body);
    } else if (scheduledTaskHostId(url, init) !== null) {
      derivedHostId = scheduledTaskHostId(url, init);
    } else {
      hostless = true;
    }
    // Skip keying a host we've already PROVEN routes keyless (its tunnels dialed
    // in without a key → they live on the default replica). This spares the long
    // tail of control paths (approve/stop/interrupt/…) from each needing a
    // server-side wrong-replica guard: once demoted, every request for this host
    // goes keyless from the start and reaches the tunnel first try. A demoted
    // host that still returns wrong_replica keyless is un-demoted below.
    if (derivedHostId && (!isDatabricksWorkspace() || !isHostKeyless(derivedHostId))) {
      headers.set(SLICE_KEY_HEADER, derivedHostId);
      stampedSliceKey = true;
    } else if (derivedHostId) {
      unkeyed = true;
    } else if (hostless) {
      headers.set(SLICE_KEY_HEADER, updatesRoutingKey());
    } else if (!headers.has(SLICE_KEY_HEADER)) {
      headers.set(SLICE_KEY_HEADER, clientRoutingKey());
    }
  }
  // Bypass the browser HTTP cache for all API calls. Session
  // endpoints (GET /v1/sessions/{id}) carry volatile in-memory state
  // (pending_elicitations) that changes between fetches without any
  // URL change. Without no-store the browser may serve a stale
  // cached response — e.g. one captured before an elicitation was
  // published — causing the ApprovalCard to vanish on navigate-back.
  let res = await hostFetch(
    url,
    {
      ...init,
      headers,
      cache: "no-store",
    },
    { unkeyed },
  );

  // Wrong-replica fallback: a keyed request that reached the wrong replica comes
  // back 400 wrong_replica. Re-address ONCE with the key removed so it routes by
  // the default (client and host may be out of sync on which sharding strategy
  // the host registered under, so we can't know up front — try keyed, then fall
  // back). Only when WE stamped the key; a genuinely-offline runner returns
  // runner_unavailable and is not re-addressed here.
  if (isDatabricksWorkspace() && stampedSliceKey && (await _isWrongReplica(res))) {
    // Fresh Headers for the retry — mutating the first request's `headers`
    // object in place would also clear the key on the already-sent request
    // (callers/tests hold it by reference).
    const retryHeaders = new Headers(headers);
    retryHeaders.delete(SLICE_KEY_HEADER);
    res = await hostFetch(
      url,
      {
        ...init,
        headers: retryHeaders,
        cache: "no-store",
      },
      { unkeyed: true },
    );
    // Only a successful keyless retry proves this host uses the default replica.
    // Failed retries must preserve the host key for subsequent requests.
    if (derivedHostId && res.ok) {
      markHostKeyless(derivedHostId);
    }
  } else if (
    derivedHostId === null &&
    !hostless &&
    !callerKeyed &&
    isDatabricksWorkspace() &&
    (await _isWrongReplica(res))
  ) {
    // A session request whose host was unknown missed. Refresh its host (the first
    // managed message can outlive provisioning) and try that host's key; then try
    // the unkeyed default replica, which holds hosts whose tunnels connected
    // without a key.
    const sessionId = sessionIdForRecovery(url, init?.method);
    let hostId: string | null = null;
    let demoted = false;
    if (sessionId !== null) {
      init?.signal?.throwIfAborted();
      await resolveSessionHost(sessionId, { force: true });
      init?.signal?.throwIfAborted();
      hostId = getSessionHost(sessionId);
      demoted = hostId !== null && isHostKeyless(hostId);
      if (hostId !== null && !demoted) {
        const retryHeaders = new Headers(headers);
        retryHeaders.set(SLICE_KEY_HEADER, hostId);
        res = await hostFetch(url, { ...init, headers: retryHeaders, cache: "no-store" });
      }
    }
    if (await _isWrongReplica(res)) {
      const retryHeaders = new Headers(headers);
      retryHeaders.delete(SLICE_KEY_HEADER);
      res = await hostFetch(
        url,
        { ...init, headers: retryHeaders, cache: "no-store" },
        { unkeyed: true },
      );
      if (hostId !== null && res.ok) {
        markHostKeyless(hostId);
      } else if (hostId !== null && demoted && (await _isWrongReplica(res))) {
        // The default replica missed too, so the demotion is stale: try the host's key.
        clearHostKeyless(hostId);
        const keyedHeaders = new Headers(headers);
        keyedHeaders.set(SLICE_KEY_HEADER, hostId);
        res = await hostFetch(url, { ...init, headers: keyedHeaders, cache: "no-store" });
      }
    }
  } else if (
    isDatabricksWorkspace() &&
    derivedHostId &&
    unkeyed &&
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

// A light authenticated read the SPA already makes at boot. It reaches the same
// auth edge as the stream and the sends, so its answer speaks for them.
const SIGN_IN_PROBE_PATH = "/v1/me";

/**
 * Whether the sign-in has expired, judged from one request that does not follow
 * redirects. A redirect to a login page or a 401/403 means a retry cannot renew the
 * sign-in. The edge answers an invalid session with a redirect, never an HTML 200,
 * so a 200 (such as a maintenance page) reads as not expired. A failed probe also
 * reads as not expired, and the caller keeps retrying.
 */
export async function isSignInExpired(signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await hostFetch(
      SIGN_IN_PROBE_PATH,
      { redirect: "manual", cache: "no-store", signal },
      { hostless: true },
    );
    return res.type === "opaqueredirect" || res.status === 401 || res.status === 403;
  } catch {
    return false;
  }
}
