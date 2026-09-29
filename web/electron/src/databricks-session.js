// Bridge a Databricks OAuth access token into a DBAUTH web-session cookie.
//
// Calls the login service's /auth/session/create (the OAuth→session bridge from
// the Databricks One mobile design) with a bearer token; that endpoint mints an
// SMv2 session and returns it as a 302 -> next_url with Set-Cookie: DBAUTH. We
// let net.request FOLLOW that redirect so Electron commits the cookie into the
// target window's session jar, then confirm it landed, so the /omnigent SPA
// opens already authenticated. databricks-oauth.js supplies/refreshes the token;
// main.js calls ensureDatabricksSession at the pre-load and session-expiry seams.

"use strict";

const { net } = require("electron");
const {
  runInteractiveLogin,
  getValidStoredToken,
  saveWorkspaceToken,
  isTrustedDatabricksOrigin,
} = require("./databricks-oauth");
const { parseAccountFromToken, listRunningWorkspaces } = require("./databricks-account");
const { isDatabricksOAuthServerUrl } = require("./url");
const { cookieMatchesOrigin } = require("./databricks-auth");

const SESSION_CREATE_PATH = "/auth/session/create";
// Bound on the session-create request so a stalled socket can't hang connect.
const NETWORK_TIMEOUT_MS = 20_000;

/**
 * Ensure ``ses`` holds a live DBAUTH cookie for a workspace, and return that
 * workspace origin. Two entry points with deliberately different behavior:
 *
 * - Explicit connect/login (``interactive: true``): ALWAYS authenticate fresh —
 *   never silently reuse a stored token. So a new window connecting to a SPOG
 *   URL re-runs the account flow + picker (choosing the workspace for THIS
 *   window) instead of dropping into another window's workspace. A workspace URL
 *   re-authenticates directly (usually a silent browser SSO round-trip). The
 *   result is persisted keyed by the resolved workspace origin.
 * - Restore/renewal (``interactive: false``): reuse the stored token for this
 *   (already-resolved) workspace, refreshing if needed, and re-mint the cookie
 *   against the SAME workspace — no browser, no picker. This is the ONLY path
 *   that reads the cache, including relaunch and additional windows.
 *
 * @param {Electron.Session} ses The session whose cookie jar to seed.
 * @param {string} origin The entered/pinned origin (account or workspace host).
 * @param {{ interactive?: boolean, nextPath?: string, workspaceId?: string, signal?: AbortSignal,
 *   pickWorkspace?: (workspaces: Array<{workspaceId: string, name: string, fqdn: string}>)
 *     => Promise<{fqdn: string, name: string} | null> }} [opts]
 *   ``workspaceId`` (from a ``?o=`` hint) auto-selects that workspace for an
 *   account-scoped login, skipping the picker.
 * @returns {Promise<string>} The workspace origin the session was created for.
 */
async function ensureDatabricksSession(
  ses,
  origin,
  { interactive = true, nextPath = "/omnigent", pickWorkspace, workspaceId, signal } = {},
) {
  signal?.throwIfAborted();
  if (!isDatabricksOAuthServerUrl(origin)) {
    throw new Error("Browser OAuth requires an HTTPS Databricks workspace/account URL");
  }
  console.log("[omnigent] databricks session: prepare", {
    origin,
    interactive,
    workspaceHint: workspaceId ?? null,
  });
  let bridgeOrigin;
  let accessToken;

  if (!interactive) {
    // Shared refreshes must persist rotated credentials even if this caller cancels.
    // The cancellation check below stops this caller before cookie minting.
    accessToken = await getValidStoredToken(origin);
    bridgeOrigin = origin;
  } else {
    // Explicit login: authenticate fresh, never reusing the cache.
    const { tokens, issuerOrigin } = await runInteractiveLogin(origin, { signal });
    signal?.throwIfAborted();
    const account = parseAccountFromToken(tokens.access_token);
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
      const workspaces = await listRunningWorkspaces(account, tokens.access_token, { signal });
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
      let picked = workspaceId
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
      saveWorkspaceToken(bridgeOrigin, tokens, {
        origin: account.accountOrigin,
        id: account.accountId,
      });
      console.log(`[omnigent] databricks session: bridging to workspace ${bridgeOrigin}`);
    } else {
      // Workspace-scoped: the token targets the issuer (the entered workspace).
      bridgeOrigin = issuerOrigin;
      saveWorkspaceToken(bridgeOrigin, tokens, null);
    }
    accessToken = tokens.access_token;
  }

  signal?.throwIfAborted();
  // Never send the bearer to a non-Databricks host.
  if (!isTrustedDatabricksOrigin(bridgeOrigin)) {
    throw new Error(`refusing to send credentials to untrusted workspace origin: ${bridgeOrigin}`);
  }

  await mintSessionCookie(ses, bridgeOrigin, accessToken, nextPath, { signal });
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
  ses,
  origin,
  accessToken,
  nextPath,
  { signal, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout } = {},
) {
  signal?.throwIfAborted();
  const target = new URL(nextPath, origin);
  if (!isDatabricksOAuthServerUrl(origin) || target.origin !== origin) {
    throw new Error("Session creation requires a same-origin Databricks destination");
  }
  const writtenCookies = [];
  const sameCookie = (a, b) =>
    a.domain === b.domain &&
    a.path === b.path &&
    a.value === b.value &&
    a.expirationDate === b.expirationDate;
  const onCookieChanged = (_event, cookie, _cause, removed) => {
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
  let phase = "session-create";
  let errorBody = "";
  let requestId;
  try {
    const status = await new Promise((resolve, reject) => {
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
      const finish = (error, code) => {
        if (settled) return;
        settled = true;
        clearTimeoutFn(timer);
        signal?.removeEventListener("abort", onAbort);
        if (error) {
          console.warn("[omnigent] databricks session: bridge transport failed", { origin, phase });
          reject(error);
        } else resolve(code);
      };
      const abort = (message) => {
        finish(new Error(message));
        request.abort();
      };
      const onAbort = () => {
        finish(signal.reason);
        request.abort();
      };
      const timer = setTimeoutFn(
        () => abort("Databricks session creation timed out"),
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
            "Databricks session creation redirected to authentication or an unexpected destination",
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
        response.on("aborted", () => finish(new Error("Databricks session response aborted")));
        response.on("close", () =>
          finish(new Error("Databricks session response closed before completion")),
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
      let errorCode;
      try {
        const body = JSON.parse(errorBody);
        const code = body?.error_code ?? body?.error;
        if (typeof code === "string" && /^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(code)) errorCode = code;
      } catch {
        /* HTML and unstructured errors are not displayed. */
      }
      const details = [errorCode, requestId && `request ID ${requestId}`]
        .filter(Boolean)
        .join(", ");
      const source =
        phase === "session-create"
          ? `${SESSION_CREATE_PATH} (no redirect)`
          : "workspace landing after /auth/session/create redirect";
      const error = new Error(`${source} returned HTTP ${status}${details ? `: ${details}` : ""}`);
      error.phase = phase;
      error.status = status;
      error.requestId = requestId;
      error.errorCode = errorCode;
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
    console.log(`[omnigent] databricks session: signed in to ${origin}`);
  } finally {
    ses.cookies.removeListener("changed", onCookieChanged);
  }
}

module.exports = { ensureDatabricksSession, mintSessionCookie };
