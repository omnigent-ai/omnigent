// Bridge a Databricks OAuth access token into a DBAUTH web-session cookie.
//
// Calls the login service's /auth/session/create (the OAuth→session bridge from
// the Databricks One mobile design) with a bearer token; that endpoint mints an
// SMv2 session and returns it as a 302 -> next_url with Set-Cookie: DBAUTH. We
// let net.request FOLLOW that redirect so Electron commits the cookie into the
// target window's session jar, then confirm it landed, so the /omnigent SPA
// opens already authenticated. databricks-oauth.ts supplies/refreshes the token;
// main.js calls ensureDatabricksSession at the pre-load and session-expiry seams.

import { diagnostics } from "./diagnostics.ts";

import { isTrustedDatabricksOrigin, type DatabricksOAuth } from "./databricks-oauth.ts";
import {
  parseAccountFromToken,
  listRunningWorkspaces,
  type RunningWorkspace,
} from "./databricks-account.ts";
import { isDatabricksOAuthServerUrl } from "./url.ts";
import {
  cookieMatchesOrigin,
  isTransientRenewalError,
  IP_ACL_BLOCKED,
  SESSION_TRANSPORT,
  type RenewalTimer,
  type WorkspaceCookie,
} from "./databricks-auth.ts";

const SESSION_CREATE_PATH = "/auth/session/create";
// Bound on the session-create request so a stalled socket can't hang connect.
const NETWORK_TIMEOUT_MS = 20_000;

/** A DBAUTH cookie as the jar reports it; minting compares these fields. */
export interface SessionCookie extends WorkspaceCookie {
  path?: string | undefined;
  value?: string | undefined;
}

type CookieChangedListener = (
  event: unknown,
  cookie: SessionCookie,
  cause: string,
  removed: boolean,
) => void;

/** The part of Electron's `Session` the bridge uses; it is also passed to `net.request`. */
export interface DatabricksBridgeSession {
  cookies: {
    get: (filter: { url: string; name: string }) => Promise<SessionCookie[]>;
    on: (event: "changed", listener: CookieChangedListener) => unknown;
    removeListener: (event: "changed", listener: CookieChangedListener) => unknown;
  };
}

/** A session-create response as Electron's `IncomingMessage` delivers it. */
export interface BridgeResponse {
  statusCode: number;
  headers?: Record<string, string | string[] | undefined>;
  on: ((event: "data", listener: (chunk: unknown) => void) => unknown) &
    ((event: "end" | "aborted" | "close", listener: () => void) => unknown) &
    ((event: "error", listener: (error: Error) => void) => unknown);
}

/** The part of Electron's `ClientRequest` the bridge uses. */
export interface BridgeRequest {
  setHeader: (name: string, value: string) => void;
  followRedirect: () => void;
  abort: () => void;
  end: () => void;
  on: ((
    event: "redirect",
    listener: (statusCode: number, method: string, redirectUrl: string) => void,
  ) => unknown) &
    ((event: "response", listener: (response: BridgeResponse) => void) => unknown) &
    ((event: "error", listener: (error: Error) => void) => unknown) &
    ((event: "abort", listener: () => void) => unknown);
}

export interface BridgeRequestOptions {
  method: "GET";
  url: string;
  session: DatabricksBridgeSession;
  useSessionCookies: true;
  redirect: "manual";
}

/** The part of Electron's `net` the bridge uses. */
export interface BridgeNet {
  request: (options: BridgeRequestOptions) => BridgeRequest;
}

/** The account-workspaces lookup; defaults to databricks-account. */
export interface AccountWorkspaces {
  parseAccountFromToken: typeof parseAccountFromToken;
  listRunningWorkspaces: typeof listRunningWorkspaces;
}

/** What main injects: Electron's `net` and the process's one Databricks OAuth instance. */
export interface DatabricksSessionDeps {
  net: BridgeNet;
  oauth: Pick<
    DatabricksOAuth,
    "runInteractiveLogin" | "getValidStoredToken" | "saveWorkspaceToken"
  >;
  accountWorkspaces?: AccountWorkspaces;
}

export interface EnsureSessionOptions {
  interactive?: boolean | undefined;
  useStoredCredentials?: boolean | undefined;
  storedOrigin?: string | undefined;
  nextPath?: string | undefined;
  /** From a `?o=` hint: auto-selects that workspace for an account-scoped login. */
  workspaceId?: string | undefined;
  signal?: AbortSignal | undefined;
  pickWorkspace?:
    | ((workspaces: RunningWorkspace[]) => Promise<{ fqdn: string; name: string } | null>)
    | undefined;
}

export interface MintSessionCookieOptions {
  signal?: AbortSignal | undefined;
  setTimeoutFn?: (callback: () => void, delay: number) => RenewalTimer;
  clearTimeoutFn?: (timer: RenewalTimer) => void;
}

export interface DatabricksSession {
  ensureDatabricksSession: (
    ses: DatabricksBridgeSession,
    origin: string,
    options?: EnsureSessionOptions,
  ) => Promise<string>;
  mintSessionCookie: (
    ses: DatabricksBridgeSession,
    origin: string,
    accessToken: string,
    nextPath: string,
    options?: MintSessionCookieOptions,
  ) => Promise<void>;
}

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** The DBAUTH bridge over Electron's `net`; main creates one per process. */
export function createDatabricksSession({
  net,
  oauth,
  accountWorkspaces = { parseAccountFromToken, listRunningWorkspaces },
}: DatabricksSessionDeps): DatabricksSession {
  /**
   * Ensure ``ses`` holds a live DBAUTH cookie for a workspace, and return that
   * workspace origin. Two entry points with deliberately different behavior:
   *
   * - Explicit connect/login (``interactive: true``): try the stored credentials
   *   for ``storedOrigin`` or else the entered origin (unless
   *   ``useStoredCredentials: false``), else authenticate fresh in the browser.
   *   Tokens are keyed by WORKSPACE origin, so a SPOG URL re-runs the account
   *   flow + picker for THIS window unless the caller names the workspace it
   *   reached before as ``storedOrigin``. The result is persisted keyed by the
   *   resolved workspace origin.
   * - Restore/renewal (``interactive: false``): reuse the stored token for this
   *   (already-resolved) workspace, refreshing if needed, and re-mint the cookie
   *   against the SAME workspace — no browser, no picker.
   *
   * @param ses The session whose cookie jar to seed.
   * @param origin The entered/pinned origin (account or workspace host).
   * @returns The workspace origin the session was created for.
   */
  async function ensureDatabricksSession(
    ses: DatabricksBridgeSession,
    origin: string,
    {
      interactive = true,
      useStoredCredentials = true,
      storedOrigin = origin,
      nextPath = "/omnigent",
      pickWorkspace,
      workspaceId,
      signal,
    }: EnsureSessionOptions = {},
  ): Promise<string> {
    signal?.throwIfAborted();
    if (!isDatabricksOAuthServerUrl(origin)) {
      throw new Error("Browser OAuth requires an HTTPS Databricks workspace/account URL");
    }
    console.log("[omnigent] databricks session: prepare", {
      origin,
      interactive,
      workspaceHint: workspaceId ?? null,
    });
    if (interactive && useStoredCredentials) {
      try {
        return await ensureDatabricksSession(ses, storedOrigin, {
          interactive: false,
          nextPath,
          signal,
        });
      } catch (error) {
        signal?.throwIfAborted();
        // A browser sign-in can't finish while the workspace is unreachable either.
        // Reconnecting resumes the workspace whose credentials were tried.
        if (isTransientRenewalError(error)) {
          if (storedOrigin !== origin)
            (error as { storedOrigin?: string }).storedOrigin = storedOrigin;
          throw error;
        }
        console.log("[omnigent] databricks session: stored credentials unusable", {
          origin: storedOrigin,
        });
      }
    }
    let bridgeOrigin;
    let accessToken;
    // Silent reconnects reuse credentials stored for `origin`; a failure before those exist can't resume.
    const notResumable = (error: unknown): never => {
      if (error && typeof error === "object") (error as { resumable?: boolean }).resumable = false;
      throw error;
    };

    if (!interactive) {
      // Shared refreshes must persist rotated credentials even if this caller cancels.
      // The cancellation check below stops this caller before cookie minting.
      accessToken = await oauth.getValidStoredToken(origin);
      bridgeOrigin = origin;
    } else {
      // Explicit login: authenticate fresh, never reusing the cache.
      const { tokens, issuerOrigin } = await oauth
        .runInteractiveLogin(origin, { signal })
        .catch(notResumable);
      signal?.throwIfAborted();
      const account = accountWorkspaces.parseAccountFromToken(tokens.access_token);
      console.log("[omnigent] databricks session: token routing", {
        issuerOrigin,
        accountScoped: Boolean(account),
      });
      if (account) {
        // Account-scoped (SPOG): the account host has no /auth/session/create, so
        // resolve the account's workspaces, let the user pick, and bridge to that
        // workspace. Persist keyed by the workspace origin (with the account
        // context) so silent refresh later hits the account token endpoint.
        if (!isTrustedDatabricksOrigin(account.accountOrigin)) {
          throw new Error(`refusing to use an untrusted account origin: ${account.accountOrigin}`);
        }
        const workspaces = await accountWorkspaces
          .listRunningWorkspaces(account, tokens.access_token, {
            signal,
          })
          .catch(notResumable);
        signal?.throwIfAborted();
        console.log("[omnigent] databricks session: workspace lookup", {
          accountOrigin: account.accountOrigin,
          count: workspaces.length,
        });
        if (workspaces.length === 0) {
          throw new Error("no running workspaces available for this account");
        }
        // A `?o=<workspace_id>` hint from the entered URL names the workspace, so
        // auto-select it and skip the picker. Fall back to the picker when there's
        // no hint, or the hint doesn't match a workspace the user can access.
        let picked: { fqdn: string; name: string } | null | undefined = workspaceId
          ? workspaces.find((w) => w.workspaceId === String(workspaceId))
          : undefined;
        if (picked) {
          console.log(`[omnigent] databricks session: auto-selected workspace o=${workspaceId}`);
        } else {
          if (workspaceId) {
            console.warn(
              `[omnigent] databricks session: o=${workspaceId} not in the account's workspaces; showing picker`,
            );
          }
          if (typeof pickWorkspace !== "function") {
            throw new Error("account-scoped login requires a workspace picker");
          }
          picked = await pickWorkspace(workspaces);
        }
        signal?.throwIfAborted();
        if (!picked) {
          throw Object.assign(new Error("Workspace selection cancelled"), { name: "AbortError" });
        }
        bridgeOrigin = `https://${picked.fqdn}`;
        oauth.saveWorkspaceToken(bridgeOrigin, tokens, {
          origin: account.accountOrigin,
          id: account.accountId,
        });
        console.log(`[omnigent] databricks session: bridging to workspace ${bridgeOrigin}`);
      } else {
        // Workspace-scoped: the token targets the issuer (the entered workspace).
        bridgeOrigin = issuerOrigin;
        oauth.saveWorkspaceToken(bridgeOrigin, tokens, null);
      }
      accessToken = tokens.access_token;
    }

    signal?.throwIfAborted();
    // Never send the bearer to a non-Databricks host.
    if (!isTrustedDatabricksOrigin(bridgeOrigin)) {
      throw new Error(
        `refusing to send credentials to untrusted workspace origin: ${bridgeOrigin}`,
      );
    }

    const minted = mintSessionCookie(ses, bridgeOrigin, accessToken, nextPath, { signal });
    await (bridgeOrigin === origin ? minted : minted.catch(notResumable));
    signal?.throwIfAborted();
    return bridgeOrigin;
  }

  /**
   * Exchange the bearer token for a DBAUTH cookie via /auth/session/create. The
   * endpoint replies 302 -> next_url with Set-Cookie: DBAUTH; we let net.request
   * follow the redirect so Electron commits that cookie into ``ses`` (Electron
   * hides Set-Cookie from the JS-visible redirect headers, so we read the jar
   * rather than parse the header), then confirm DBAUTH is present.
   */
  async function mintSessionCookie(
    ses: DatabricksBridgeSession,
    origin: string,
    accessToken: string,
    nextPath: string,
    {
      signal,
      setTimeoutFn = setTimeout,
      clearTimeoutFn = clearTimeout,
    }: MintSessionCookieOptions = {},
  ): Promise<void> {
    signal?.throwIfAborted();
    const target = new URL(nextPath, origin);
    if (!isDatabricksOAuthServerUrl(origin) || target.origin !== origin) {
      throw new Error("Session creation requires a same-origin Databricks destination");
    }
    const writtenCookies: SessionCookie[] = [];
    const sameCookie = (a: SessionCookie, b: SessionCookie) =>
      a.domain === b.domain &&
      a.path === b.path &&
      a.value === b.value &&
      a.expirationDate === b.expirationDate;
    const onCookieChanged: CookieChangedListener = (_event, cookie, _cause, removed) => {
      if (!removed && cookieMatchesOrigin(cookie, origin)) writtenCookies.push(cookie);
    };
    const before = await ses.cookies.get({ url: target.href, name: "DBAUTH" });
    signal?.throwIfAborted();
    console.log("[omnigent] databricks session: bridge request", {
      origin,
      path: SESSION_CREATE_PATH,
      nextPath: target.pathname,
      nextWorkspaceSelector: target.searchParams.get("o"),
      existingCookies: before.length,
    });
    ses.cookies.on("changed", onCookieChanged);
    let phase: "session-create" | "workspace landing" = "session-create";
    let errorBody = "";
    let requestId: string | undefined;
    try {
      const status = await new Promise<number>((resolve, reject) => {
        const url = `${origin}${SESSION_CREATE_PATH}?next_url=${encodeURIComponent(nextPath)}`;
        const request = net.request({
          method: "GET",
          url,
          session: ses,
          useSessionCookies: true,
          redirect: "manual",
        });
        let settled = false;
        let redirects = 0;
        // Settles once: an error rejects; otherwise `code` is the final status.
        const finish = (error: unknown, code?: number) => {
          if (settled) return;
          settled = true;
          clearTimeoutFn(timer);
          signal?.removeEventListener("abort", onAbort);
          if (error) {
            console.warn("[omnigent] databricks session: bridge transport failed", {
              origin,
              phase,
            });
            reject(error);
          } else resolve(code as number);
        };
        const transportError = (message: string) =>
          Object.assign(new Error(message), { errorCode: SESSION_TRANSPORT });
        const abort = (error: Error) => {
          finish(error);
          request.abort();
        };
        // Only registered on, or called for, a present signal.
        const onAbort = () => {
          finish(signal!.reason);
          request.abort();
        };
        const timer = setTimeoutFn(
          () => abort(transportError("Databricks session creation timed out")),
          NETWORK_TIMEOUT_MS,
        );
        request.setHeader("Authorization", `Bearer ${accessToken}`);
        request.on("redirect", (redirectStatus, method, redirectUrl) => {
          let destination;
          try {
            destination = new URL(redirectUrl);
          } catch {
            /* Reject malformed redirects below. */
          }
          const accepted = ++redirects === 1 && redirectUrl === target.href;
          console.log("[omnigent] databricks session: bridge redirect", {
            origin,
            status: redirectStatus,
            method,
            accepted,
            targetOrigin: destination?.origin,
            targetPath: destination?.pathname,
            targetWorkspaceSelector: destination?.searchParams.get("o"),
          });
          // Follow only the intended app destination so Chromium commits Set-Cookie.
          if (!accepted) {
            abort(
              new Error(
                "Databricks session creation redirected to authentication or an unexpected destination",
              ),
            );
            return;
          }
          phase = "workspace landing";
          request.followRedirect();
        });
        request.on("response", (response) => {
          const id =
            response.headers?.["x-databricks-request-id"] ?? response.headers?.["x-request-id"];
          if (typeof id === "string" && /^[a-zA-Z0-9._:-]{1,128}$/.test(id)) requestId = id;
          diagnostics.record("databricks_bridge", {
            origin,
            phase: phase === "session-create" ? "session_create" : "workspace_landing",
            status: response.statusCode,
            requestId,
          });
          console.log("[omnigent] databricks session: bridge response", {
            origin,
            phase,
            status: response.statusCode,
            requestId,
            cookieWritesObserved: writtenCookies.length,
          });
          response.on("data", (chunk) => {
            // Read only enough to extract a structured error code; never display the body.
            if (response.statusCode >= 400 && errorBody.length < 4096) {
              errorBody += String(chunk).slice(0, 4096 - errorBody.length);
            }
          });
          response.on("end", () => finish(null, response.statusCode));
          response.on("error", (error) => finish(error));
          response.on("aborted", () =>
            finish(transportError("Databricks session response aborted")),
          );
          response.on("close", () =>
            finish(transportError("Databricks session response closed before completion")),
          );
        });
        // ClientRequest's Writable closes after end(), before the response arrives.
        // Only response completion, explicit failures, or the deadline settle the request.
        request.on("error", (error) => finish(error));
        request.on("abort", () => finish(new Error("Databricks session request aborted")));
        signal?.addEventListener("abort", onAbort, { once: true });
        if (signal?.aborted) onAbort();
        else request.end();
      });
      if (status < 200 || status >= 300) {
        let errorCode: string | undefined;
        try {
          const body: unknown = JSON.parse(errorBody);
          const code = propertyOf(body, "error_code") ?? propertyOf(body, "error");
          if (typeof code === "string" && /^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(code)) {
            errorCode = code;
          }
        } catch {
          /* HTML and unstructured errors are not displayed. */
        }
        if (status === 403 && /is blocked by Databricks IP ACL/.test(errorBody)) {
          errorCode = IP_ACL_BLOCKED;
        }
        const details = [errorCode, requestId && `request ID ${requestId}`]
          .filter(Boolean)
          .join(", ");
        const source =
          phase === "session-create"
            ? `${SESSION_CREATE_PATH} (no redirect)`
            : "workspace landing after /auth/session/create redirect";
        const error = Object.assign(
          new Error(`${source} returned HTTP ${status}${details ? `: ${details}` : ""}`),
          { phase, status, requestId, errorCode },
        );
        console.warn("[omnigent] databricks session: bridge rejected", {
          origin,
          phase,
          status,
          errorCode,
          requestId,
        });
        throw error;
      }
      const jar = await ses.cookies.get({ url: target.href, name: "DBAUTH" });
      signal?.throwIfAborted();
      const minted = jar.some(
        (cookie) =>
          writtenCookies.some((c) => sameCookie(c, cookie)) ||
          !before.some((c) => sameCookie(c, cookie)),
      );
      console.log("[omnigent] databricks session: cookie confirmation", {
        origin,
        existingCookies: before.length,
        currentCookies: jar.length,
        cookieWritesObserved: writtenCookies.length,
        minted,
      });
      if (!minted) {
        throw new Error(`${SESSION_CREATE_PATH}: no new DBAUTH cookie stored`);
      }
      diagnostics.record("databricks_session", { origin, outcome: "signed_in" });
      console.log(`[omnigent] databricks session: signed in to ${origin}`);
    } finally {
      ses.cookies.removeListener("changed", onCookieChanged);
    }
  }

  return { ensureDatabricksSession, mintSessionCookie };
}
