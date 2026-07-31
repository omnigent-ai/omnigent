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
import { getSessionHost, modalHostId } from "@/lib/sessionHost";

// Single-user sentinel from `GET /v1/me` (server's RESERVED_USER_LOCAL);
// not a real actor, so never used as an author label.
const RESERVED_USER_LOCAL = "local";

// Dicer replica-routing header read by the managed server's sidecar. A host's
// control tunnel and its runners' tunnels are keyed by the host_id, so a
// request scoped to a host (or to a session running on one) must carry the
// same host_id to reach the replica holding that tunnel.
const SLICE_KEY_HEADER = "X-Databricks-Omnigent-Slice-Key";

// Server error code (see omnigent/errors.py ErrorCode.HOST_UNAVAILABLE) returned
// when a slice-keyed request landed on a replica that does NOT hold the session's
// host tunnel — i.e. the key routed it to the wrong pod (version skew between a
// slice-key-aware UI and the host's registration). Distinct from
// "runner_unavailable" (the runner is genuinely offline everywhere), where a
// keyless retry would be pointless. We retry ONCE without the slice key so the
// request routes by the workspace-id default instead.
const HOST_UNAVAILABLE_CODE = "host_unavailable";

/**
 * Resolve the host_id a request URL is scoped to, or ``null`` when the request
 * isn't host-scoped or the host is unknown:
 *
 * - ``/v1/hosts/{host_id}/...`` → the host_id straight from the path.
 * - ``/v1/sessions/{session_id}/...`` → the session's host_id from
 *   {@link getSessionHost} (the session must have been loaded).
 *
 * Everything else (session lists, ``/v1/sessions/updates``, ``/health``) is a
 * cross-host or DB-backed read that needs no sticky routing → ``null``. The
 * caller emits the returned host_id as the {@link SLICE_KEY_HEADER} routing key.
 *
 * This is an ALLOWLIST of the two host-scoped URL families that exist today
 * (verified: no other server route is host-scoped — ``/v1/sessions/{id}/agent``
 * is under the sessions prefix, and ``/v1/runners/{id}`` is not called from the
 * UI). If a future REQUIRE route adds a NEW host-scoped shape, extend this
 * matcher — a shape it doesn't recognize returns ``null`` here. That is no
 * longer silently unkeyed (the always-send {@link modalHostId} fallback still
 * stamps the modal host), but the modal host is a best-effort guess, so a
 * genuinely new host-scoped route should still be added here to key by its OWN
 * host. The regression test in identity.test.ts pins the known families so a
 * matcher change is a conscious edit.
 */
function hostIdForUrl(url: string): string | null {
  const hostMatch = url.match(/\/v1\/hosts\/([^/?#]+)/);
  if (hostMatch) return decodeURIComponent(hostMatch[1]);
  const sessionMatch = url.match(/\/v1\/sessions\/([^/?#]+)/);
  if (sessionMatch) return getSessionHost(decodeURIComponent(sessionMatch[1]));
  return null;
}

/**
 * The create endpoint (``POST /v1/sessions``) carries its target host_id in the
 * request body, not the URL, so {@link hostIdForUrl} can't see it. Left unkeyed,
 * the create round-robins and can miss the replica holding the host's runner
 * tunnel — the server notifies the runner inline over that pod-local tunnel, so
 * an off-replica create fails with "runner is offline". Recover the host_id from
 * a JSON create body so the create is pinned like every other host-scoped
 * request. Bundled (multipart) creates are hostless here — they only write rows
 * and bind the runner via a separate ``POST /v1/hosts/{id}/runners`` — and a
 * sandbox create carries ``host_type`` but no ``host_id``; both yield null.
 */
function hostIdForCreateBody(url: string, body: BodyInit | null | undefined): string | null {
  if (typeof body !== "string") return null;
  if (!/\/v1\/sessions(?:\?|$)/.test(url)) return null;
  try {
    const hostId = (JSON.parse(body) as { host_id?: unknown }).host_id;
    return typeof hostId === "string" && hostId ? hostId : null;
  } catch {
    return null;
  }
}

let _currentUserId: string | null = null;
// Admin flag from the same `/v1/me` probe. Mode-agnostic (the shared
// `users.is_admin` column), so the SPA can gate admin chrome in EVERY
// auth mode — including OIDC/SSO, where the accounts-only `/auth/me`
// endpoint doesn't exist. Defaults false until the probe resolves.
let _currentIsAdmin = false;
let _resolved = false;
let _resolvePromise: Promise<string | null> | null = null;
// Cache the server-provided login URL on the first /v1/me probe so
// later session-expiry redirects in authenticatedFetch hit the right
// path per provider — "/login" for accounts, "/auth/login" for OIDC.
// Hardcoding "/login" here previously sent OIDC users to an accounts
// password form that had no connection to their IdP.
let _serverLoginUrl: string | null = null;

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
function _isOnLoginPath(): boolean {
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
  if (_resolved) return _currentUserId;
  if (_resolvePromise) return _resolvePromise;
  _resolvePromise = (async () => {
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
            _serverLoginUrl = data.login_url;
            if (!_isOnLoginPath()) {
              const returnTo = encodeURIComponent(
                window.location.pathname + window.location.search,
              );
              window.location.href = `${data.login_url}?return_to=${returnTo}`;
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
        _currentUserId = data.user_id;
        _currentIsAdmin = data.is_admin ?? false;
      }
    } catch {
      // Server unreachable — leave as null.
    }
    _resolved = true;
    return _currentUserId;
  })();
  return _resolvePromise;
}

/** Return the cached user ID (null before resolveIdentity completes). */
export function getCurrentUserId(): string | null {
  return _currentUserId;
}

/**
 * Whether the current user is an admin, per the `/v1/me` probe.
 * Mode-agnostic — usable to gate admin chrome under header, accounts,
 * AND OIDC. Returns false before `resolveIdentity` completes.
 */
export function getCurrentIsAdmin(): boolean {
  return _currentIsAdmin;
}

/**
 * Viewer id for labeling own optimistic bubbles, the client analog of
 * the server's `attribution_user`. Returns null before identity
 * resolves and for the `"local"` sentinel, so those stay unlabeled.
 */
export function getCurrentAuthorId(): string | null {
  if (_currentUserId === null || _currentUserId === RESERVED_USER_LOCAL) {
    return null;
  }
  return _currentUserId;
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
 * ({@link HOST_UNAVAILABLE_CODE}). Gated on 503 first so the body is only read
 * on the rare failure; reads a CLONE so the original response body stays intact
 * for the caller when this returns false.
 *
 * @param res The response to inspect.
 * @returns `true` when the JSON body carries `error.code === "host_unavailable"`.
 */
async function _isHostUnavailable(res: Response): Promise<boolean> {
  if (res.status !== 503) return false;
  try {
    const body = (await res.clone().json()) as { error?: { code?: string } };
    return body.error?.code === HOST_UNAVAILABLE_CODE;
  } catch {
    // Non-JSON / empty body — not the structured host_unavailable error.
    return false;
  }
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (
    _currentUserId &&
    _currentUserId !== RESERVED_USER_LOCAL &&
    !headers.has("X-Forwarded-Email")
  ) {
    headers.set("X-Forwarded-Email", _currentUserId);
  }
  // Pin host- and session-scoped requests to the managed-server replica
  // holding that host's runner tunnel (Dicer slice key = host_id). Derived
  // centrally so no call site has to thread it; a caller that set the header
  // explicitly wins, and non-host-scoped requests get no key (any replica).
  // Only on the workspace-embedded (managed) UI — a standalone/self-hosted
  // server has no Dicer, so the key would just dirty its logs. The embed host
  // installs a fetcher; standalone has none.
  const url = typeof input === "string" ? input : input.toString();
  // Whether WE (not the caller) stamped the slice key on this request; only then
  // is a keyless retry meaningful on a wrong-replica (host_unavailable) response.
  let stampedSliceKey = false;
  // Stamp on the workspace-hosted UI: the embed (a host fetcher is installed)
  // OR a standalone `npm run dev` bundle pointed at a workspace URL
  // (VITE_DATABRICKS_WORKSPACE, via isDatabricksWorkspace). A self-hosted server
  // has no Dicer, so the key would just dirty its logs.
  if (!headers.has(SLICE_KEY_HEADER) && isDatabricksWorkspace()) {
    // Prefer the request's own host (path / session map / create body); else
    // fall back to the frozen modal host so EVERY workspace request carries a
    // key (the always-send invariant). The modal host is a best-effort guess —
    // safe because host-less routes serve from any replica and a wrong guess
    // self-heals via the host_unavailable keyless retry below. Returns null
    // (→ no key → workspace-id default) before the modal host is resolved.
    const hostId =
      hostIdForUrl(url) ?? hostIdForCreateBody(url, init?.body) ?? modalHostId();
    if (hostId) {
      headers.set(SLICE_KEY_HEADER, hostId);
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

  // Wrong-replica fallback: a slice-keyed request that missed the host's pod
  // comes back 503 host_unavailable. Retry ONCE with the key removed so it
  // routes by the workspace-id default (the managed UI and the OSS host may be
  // out of sync on which sharding strategy the host registered under, so we
  // can't know up front — we try keyed, then fall back). Only when WE stamped
  // the key; a genuinely-offline runner returns runner_unavailable and is not
  // retried here.
  if (stampedSliceKey && (await _isHostUnavailable(res))) {
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
  }

  if (
    // When embedded, the host owns auth (e.g. cookie/session via
    // workspaceFetch) and a 401 should surface to the caller, not
    // trigger web's standalone OIDC redirect.
    !getOmnigentHostConfig().fetcher &&
    res.status === 401 &&
    !input.toString().includes("/v1/me") &&
    !input.toString().includes("/auth/") &&
    !_isOnLoginPath()
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
    // the caller instead. (_serverLoginUrl from the /v1/me probe is a
    // fallback for the brief window before capabilities resolves.)
    const loginUrl = getCachedServerInfo()?.login_url ?? _serverLoginUrl;
    if (loginUrl) {
      window.location.href = `${loginUrl}?return_to=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    }
  }
  return res;
}
