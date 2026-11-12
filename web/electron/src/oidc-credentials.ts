// OIDC credentials: sign-in through the server's native loopback flow in the
// system browser (RFC 8252 + PKCE), and the per-origin refresh grant that
// renews sessions silently. The session cookie itself lives in Electron's jar.

import { diagnostics } from "./diagnostics.ts";
import { makePkce, runLoopbackAuthorization, type LoopbackCallback } from "./loopback-oauth.ts";
import { createTokenStore, type TokenEncryption } from "./token_store.ts";

// Per-request timeout for back-channel calls so a stalled socket can't hang
// an awaited connect or renewal.
const NETWORK_TIMEOUT_MS = 20_000;

/** A session token minted by the server, with its lifetime in seconds when known. */
export interface MintedSession {
  token: string;
  expiresIn: number | null;
}

export interface SignInOptions {
  signal?: AbortSignal | undefined;
  openExternal?: (url: string) => Promise<unknown>;
}

/** What main injects: Electron's `safeStorage` and a `shell.openExternal` wrapper. */
export interface OidcCredentialsDeps {
  safeStorage: TokenEncryption;
  openExternal: (url: string) => Promise<unknown>;
}

export interface OidcCredentials {
  signInWithBrowser: (serverUrl: string, options?: SignInOptions) => Promise<MintedSession>;
  refreshSession: (serverUrl: string) => Promise<MintedSession>;
  signOut: (serverUrl: string) => Promise<void>;
  hasStoredGrant: (serverUrl: string) => boolean;
  removeStoredGrant: (serverUrl: string) => boolean;
}

/** An error with a stable `code` the shell maps to a user-facing message. */
function oidcError(code: string, message: string | undefined, extra: object = {}) {
  return Object.assign(new Error(message), { code, ...extra });
}

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/**
 * A server route under the server URL's mount, e.g. `/auth/login` on
 * `https://host/omnigent/` → `https://host/omnigent/auth/login`.
 *
 * @param routePath An absolute route path.
 */
export function serverEndpoint(serverUrl: string, routePath: string): string {
  const url = new URL(serverUrl);
  url.pathname = url.pathname.replace(/\/+$/, "") + routePath;
  url.search = "";
  url.hash = "";
  return url.toString();
}

function timeoutSignal(signal: AbortSignal | undefined) {
  const timeout = AbortSignal.timeout(NETWORK_TIMEOUT_MS);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

/**
 * POST a form and return `{ status, body }` (body null when not JSON). Never
 * follows redirects: these endpoints answer JSON, so a 3xx must not carry a
 * code, verifier, or refresh token anywhere else.
 */
async function postForm(
  url: string,
  form: Record<string, string>,
  { signal }: { signal?: AbortSignal | undefined } = {},
): Promise<{ status: number; body: Record<string, unknown> | null }> {
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: new URLSearchParams(form).toString(),
      redirect: "manual",
      signal: timeoutSignal(signal),
    });
  } catch (error) {
    signal?.throwIfAborted();
    throw oidcError("network", `could not reach ${new URL(url).host}`, { cause: error });
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON answers are treated by status alone.
  }
  signal?.throwIfAborted();
  return {
    status: response.status,
    body: body && typeof body === "object" ? (body as Record<string, unknown>) : null,
  };
}

function tokenFrom(body: Record<string, unknown> | null): MintedSession | null {
  const token = body?.token ?? body?.access_token;
  if (typeof token !== "string" || token === "") return null;
  const lifetime = body?.expires_in;
  const expiresIn =
    typeof lifetime === "number" && Number.isFinite(lifetime) && lifetime > 0 ? lifetime : null;
  return { token, expiresIn };
}

/** The fields of a loopback or abort failure that sign-in maps to an `oidcError`. */
interface SignInFailure {
  name?: unknown;
  code?: unknown;
  errorCode?: unknown;
  message?: string;
  errorDescription?: unknown;
}

/**
 * Sign-in, renewal and sign-out for OIDC servers. Main creates one per process:
 * it owns the grant store and the per-origin in-flight refreshes.
 */
export function createOidcCredentials({
  safeStorage,
  openExternal: defaultOpenExternal,
}: OidcCredentialsDeps): OidcCredentials {
  const store = createTokenStore({
    fileName: "oidc_tokens.json",
    label: "OIDC sign-in tokens",
    safeStorage,
  });

  /**
   * Sign in through the system browser and return the new session token. Stores
   * the server's refresh grant for silent renewal (and drops any older one).
   *
   * @param serverUrl The connected server URL (origin or origin+mount).
   * @throws An error whose `code` is `sign_in_failed` (with `description` when
   *   the server explained why), `timed_out`, `browser_unavailable`, `network`,
   *   or an `AbortError` when cancelled.
   */
  async function signInWithBrowser(
    serverUrl: string,
    { signal, openExternal }: SignInOptions = {},
  ): Promise<MintedSession> {
    signal?.throwIfAborted();
    const { verifier, challenge } = makePkce();
    const loginUrl = serverEndpoint(serverUrl, "/auth/login");
    let callback: LoopbackCallback;
    try {
      callback = await runLoopbackAuthorization({
        // RFC 8252 §8.3: the IP literal, never `localhost`, which could resolve elsewhere.
        hostname: "127.0.0.1",
        callbackPath: "/callback",
        redirectUri: (port) => `http://127.0.0.1:${port}/callback`,
        authorizeUrl: (redirectUri, state) =>
          `${loginUrl}?${new URLSearchParams({
            native_redirect_uri: redirectUri,
            native_state: state,
            code_challenge: challenge,
            code_challenge_method: "S256",
          })}`,
        openExternal: openExternal ?? defaultOpenExternal,
        onOpened: () => console.log("[omnigent] oidc sign-in: opened system browser"),
        pages: {
          received: [
            "Sign-in received",
            "Return to Omnigent to finish connecting, then close this tab.",
          ],
          failed: [
            "Sign-in was not completed",
            "Close this tab and try connecting again in Omnigent.",
          ],
          incomplete: [
            "Sign-in could not be completed",
            "Close this tab and try connecting again in Omnigent.",
          ],
        },
        signal,
      });
    } catch (caught) {
      const error = caught as SignInFailure | null | undefined;
      diagnostics.record("oidc_browser", {
        origin: new URL(serverUrl).origin,
        outcome: error?.name === "AbortError" || signal?.aborted ? "cancelled" : "failed",
        code: error?.code ?? error?.errorCode,
      });
      if (error?.name === "AbortError" || signal?.aborted) throw caught;
      if (error?.code === "LOOPBACK_TIMEOUT") throw oidcError("timed_out", error.message);
      if (error?.code === "BROWSER_UNAVAILABLE") {
        throw oidcError("browser_unavailable", error.message);
      }
      throw oidcError("sign_in_failed", error?.message ?? "sign-in failed", {
        description: error?.errorDescription,
      });
    }

    diagnostics.record("oidc_browser", { origin: new URL(serverUrl).origin, outcome: "accepted" });
    const { status, body } = await postForm(
      serverEndpoint(serverUrl, "/auth/native-token"),
      {
        // The runner only resolves with a code; String() is how URLSearchParams reads it anyway.
        code: String(callback.params.get("code")),
        code_verifier: verifier,
        redirect_uri: callback.redirectUri,
      },
      { signal },
    );
    const minted = status === 200 ? tokenFrom(body) : null;
    diagnostics.record("oidc_exchange", {
      origin: new URL(serverUrl).origin,
      status,
      outcome: minted ? "accepted" : "rejected",
    });
    if (!minted || !body) {
      throw oidcError("sign_in_failed", `native sign-in exchange returned HTTP ${status}`);
    }
    const origin = new URL(serverUrl).origin;
    if (typeof body.refresh_token === "string" && body.refresh_token) {
      store.save(origin, { refresh_token: body.refresh_token, user_id: body.user_id ?? null });
    } else {
      // A server without refresh grants: an older grant for this origin is stale.
      store.remove(origin);
    }
    console.log("[omnigent] oidc sign-in: signed in", {
      origin,
      refreshGrant: Boolean(body.refresh_token),
    });
    return minted;
  }

  // One in-flight refresh per origin, shared by every window on that server.
  const inflightRefresh = new Map<string, Promise<MintedSession>>();

  async function doRefresh(serverUrl: string, origin: string): Promise<MintedSession> {
    const refreshToken = propertyOf(store.load(origin), "refresh_token");
    if (typeof refreshToken !== "string" || !refreshToken) {
      throw oidcError("NO_STORED_TOKEN", `no stored sign-in for ${origin}`);
    }
    const { status, body } = await postForm(serverEndpoint(serverUrl, "/oauth/token"), {
      grant_type: "refresh_token",
      refresh_token: refreshToken,
    });
    const minted = status === 200 ? tokenFrom(body) : null;
    if (minted) {
      diagnostics.record("oidc_refresh", { origin, status, outcome: "accepted" });
      return minted;
    }
    const error = typeof body?.error === "string" ? body.error : null;
    diagnostics.record("oidc_refresh", { origin, status, outcome: "rejected" });
    // A dead grant (revoked, past its lifetime) or a server without the token
    // route: forget it, so the next connect signs in through the browser.
    if (error === "invalid_grant" || error === "expired_token" || status === 404) {
      store.remove(origin);
      throw oidcError(
        error ?? "NO_STORED_TOKEN",
        `refresh rejected for ${origin} (HTTP ${status})`,
      );
    }
    throw oidcError("network", `refresh failed for ${origin} (HTTP ${status})`);
  }

  /**
   * Mint a new session token from the stored refresh grant — no browser.
   *
   * @throws An error whose `code` is `NO_STORED_TOKEN`, `invalid_grant`,
   *   `expired_token` (the grant is then forgotten), or `network` (kept).
   */
  function refreshSession(serverUrl: string): Promise<MintedSession> {
    const origin = new URL(serverUrl).origin;
    const existing = inflightRefresh.get(origin);
    if (existing) return existing;
    const pending = doRefresh(serverUrl, origin).finally(() => inflightRefresh.delete(origin));
    inflightRefresh.set(origin, pending);
    return pending;
  }

  /**
   * Forget this origin's refresh grant and revoke it on the server. Best effort:
   * the local copy is gone even if the revoke request fails.
   */
  function signOut(serverUrl: string): Promise<void> {
    const origin = new URL(serverUrl).origin;
    const entry = store.load(origin);
    // Forgotten before any network wait, so local sign-out holds even offline.
    store.remove(origin);
    return revoke(serverUrl, origin, entry);
  }

  async function revoke(serverUrl: string, origin: string, entry: unknown): Promise<void> {
    const refreshToken = propertyOf(entry, "refresh_token");
    if (typeof refreshToken !== "string" || !refreshToken) return;
    try {
      const { status } = await postForm(serverEndpoint(serverUrl, "/oauth/revoke"), {
        refresh_token: refreshToken,
      });
      if (status < 200 || status >= 300) {
        console.warn(
          `[omnigent] oidc sign-out: revoking the grant for ${origin} returned HTTP ${status}`,
        );
      }
    } catch (error) {
      console.warn(
        `[omnigent] oidc sign-out: could not revoke the grant for ${origin}`,
        (error as { code?: unknown }).code,
      );
    }
  }

  /** Whether a refresh grant is stored for this server's origin. */
  function hasStoredGrant(serverUrl: string): boolean {
    const token = propertyOf(store.load(new URL(serverUrl).origin), "refresh_token");
    return typeof token === "string" && token !== "";
  }

  /** Dev-only: forget the stored refresh grant without revoking it. */
  function removeStoredGrant(serverUrl: string): boolean {
    const origin = new URL(serverUrl).origin;
    if (!store.load(origin)) return false;
    store.remove(origin);
    return true;
  }

  return { signInWithBrowser, refreshSession, signOut, hasStoredGrant, removeStoredGrant };
}
