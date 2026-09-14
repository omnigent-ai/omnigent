// Databricks OAuth (PKCE + loopback) for the desktop shell.
//
// Runs the RFC 8252 native-app authorization-code flow against a Databricks
// workspace's OIDC endpoints in the user's SYSTEM browser (never an embedded
// webview), and holds the resulting access/refresh tokens. databricks-session.js
// exchanges the access token for a DBAUTH web-session cookie. This lets the shell
// stop driving Databricks login inside its own BrowserWindow, which SSO providers
// and Databricks itself are locking down.
//
// The OAuth client is a public, first-party Databricks app (client_id "omnigent",
// PKCE, no secret) registered as a published connector. Optional env:
//   OMNIGENT_DATABRICKS_OAUTH_REDIRECT  (default http://localhost; must match the app's registered
//                                        loopback redirect. Databricks ignores the port per RFC 8252,
//                                        so an ephemeral free port is bound unless one is pinned.)
//   OMNIGENT_DATABRICKS_OAUTH_CLIENT_ID      (default "omnigent"; override for a custom test app)
//   OMNIGENT_DATABRICKS_OAUTH_CLIENT_SECRET  (only for a CONFIDENTIAL test app; unset for the public
//                                        "omnigent" client, which authenticates with PKCE alone)
//   OMNIGENT_DATABRICKS_OAUTH_SCOPES    (default "all-apis offline_access")
//   OMNIGENT_DATABRICKS_OAUTH_FORCE_REFRESH=1  (testing: treat the stored access token as always
//                                        expired, so the refresh path runs on every connect/reload)
//
// The authorize request goes directly to the entered origin's /oidc/v1/authorize.
// A workspace host yields a workspace-scoped token; an account/SPOG host yields an
// account-scoped token, and the workspace is then chosen from the account
// workspaces API (see databricks-session.js). Account-first entry via the SISU
// login host (target=ACCOUNT, for generic multi-account login) is a follow-up.

"use strict";

const http = require("node:http");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { shell, safeStorage } = require("electron");

const DEFAULT_REDIRECT_BASE = "http://localhost";
const DEFAULT_SCOPES = "all-apis offline_access";
// Public first-party OAuth client (PKCE, no secret), registered as a published
// connector. Overridable via env so a custom app integration can be used for
// testing before the published "omnigent" connector exists.
const OAUTH_CLIENT_ID = (process.env.OMNIGENT_DATABRICKS_OAUTH_CLIENT_ID || "omnigent").trim();
// The shipped "omnigent" client is public (PKCE, no secret). A confidential
// custom app used for testing needs its secret sent on token/refresh; unset for
// a public client, so this is omitted and PKCE alone authenticates.
const OAUTH_CLIENT_SECRET = (process.env.OMNIGENT_DATABRICKS_OAUTH_CLIENT_SECRET || "").trim();
// Bound on how long we wait for the human to finish logging in in the browser.
const AUTH_TIMEOUT_MS = 300_000;
// Per-request network timeout for the token endpoint (and other back-channel
// calls) so a stalled socket can't hang the awaited connect flow.
const NETWORK_TIMEOUT_MS = 20_000;
// Treat the access token as expired this long before its real expiry, so a
// re-mint never races the clock (and a slightly-early refresh is harmless).
const EXPIRY_SKEW_SECONDS = 300;

/** True for an http(s) URL bound to a loopback host (localhost / 127.0.0.1 / ::1). */
function isLoopbackUrl(rawUrl) {
  try {
    const { protocol, hostname } = new URL(rawUrl);
    const host = hostname.replace(/^\[|\]$/g, "");
    return protocol === "http:" && (host === "localhost" || host === "127.0.0.1" || host === "::1");
  } catch {
    return false;
  }
}

// Hostname suffixes that mark a trusted Databricks origin. The leading dot stops
// look-alikes (evil-databricks.com, databricks.com.attacker.net) from matching.
// Covers workspace hosts (…cloud.databricks.com, …gcp.databricks.com) and account
// hosts (accounts.…databricks.com) since all end in one of these.
const TRUSTED_HOST_SUFFIXES = [".databricks.com", ".azuredatabricks.net"];

/** Read the optional OAuth config from the environment. */
function config() {
  return {
    redirectBase: (process.env.OMNIGENT_DATABRICKS_OAUTH_REDIRECT ?? DEFAULT_REDIRECT_BASE).trim(),
    scopes: (process.env.OMNIGENT_DATABRICKS_OAUTH_SCOPES ?? DEFAULT_SCOPES).trim(),
  };
}

/**
 * True when `url` is an https URL on a trusted Databricks host. The OAuth issuer
 * (`iss`) is validated with this before we POST the code+verifier to it or adopt
 * it as the workspace origin, so a spoofed redirect can't steer token traffic to
 * an attacker host.
 */
function isTrustedDatabricksOrigin(url) {
  try {
    const { protocol, hostname } = new URL(url);
    return protocol === "https:" && TRUSTED_HOST_SUFFIXES.some((s) => hostname.endsWith(s));
  } catch {
    return false;
  }
}

/** Whether the OAuth flow should be attempted. The client_id is compiled in, so
 * this is always on for managed Databricks workspaces (the caller gates on that);
 * kept as the single seam where a future enable gate would live. */
function databricksOAuthConfigured() {
  return true;
}

// ── PKCE (S256) ────────────────────────────────────────────────────────────

function base64url(buf) {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function makePkce() {
  const verifier = base64url(crypto.randomBytes(64));
  const challenge = base64url(crypto.createHash("sha256").update(verifier).digest());
  return { verifier, challenge };
}

// ── Token store (~/.omnigent, encrypted at rest via safeStorage) ─────────────
//
// A dedicated file, NOT the CLI's auth_tokens.json: the shapes differ and mixing
// them would confuse omnigent_cli.js's readers. Keyed by the trailing-slash-
// stripped workspace origin, mirroring that store's keying.

function tokenStorePath() {
  return path.join(os.homedir(), ".omnigent", "databricks_oauth_tokens.json");
}

function storeKey(origin) {
  return String(origin).replace(/\/+$/, "");
}

function readStore() {
  try {
    return JSON.parse(fs.readFileSync(tokenStorePath(), "utf8"));
  } catch {
    return {};
  }
}

function writeStore(store) {
  const p = tokenStorePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(store, null, 2), { mode: 0o600 });
  try {
    fs.chmodSync(p, 0o600);
  } catch {
    // Non-POSIX filesystem — the write-time mode is best effort.
  }
}

function saveTokens(origin, tokens) {
  const store = readStore();
  if (safeStorage.isEncryptionAvailable()) {
    store[storeKey(origin)] = {
      enc: safeStorage.encryptString(JSON.stringify(tokens)).toString("base64"),
    };
  } else {
    console.warn(
      "[omnigent] safeStorage unavailable; storing Databricks tokens unencrypted (0600)",
    );
    store[storeKey(origin)] = { plain: tokens };
  }
  writeStore(store);
}

function loadTokens(origin) {
  const entry = readStore()[storeKey(origin)];
  if (!entry || typeof entry !== "object") return null;
  if (typeof entry.enc === "string") {
    try {
      return JSON.parse(safeStorage.decryptString(Buffer.from(entry.enc, "base64")));
    } catch {
      return null;
    }
  }
  if (entry.plain && typeof entry.plain === "object") return entry.plain;
  return null;
}

function deleteStoredToken(origin) {
  const key = storeKey(origin);
  const store = readStore();
  if (store[key]) {
    Reflect.deleteProperty(store, key);
    writeStore(store);
  }
}

/**
 * Persist a token keyed by the WORKSPACE origin it is used against. For an
 * account-first (SPOG) login the token is account-scoped; ``account`` records
 * the account origin+id so a later silent refresh hits the account token
 * endpoint. Keying by the workspace origin (not the issuer) is what lets the
 * session-expiry path — which only knows the workspace — find and refresh it.
 *
 * @param {string} workspaceOrigin
 * @param {{ access_token: string, refresh_token?: string, expires_at: number }} tokens
 * @param {{ origin: string, id: string } | null} account
 */
function saveWorkspaceToken(workspaceOrigin, tokens, account) {
  saveTokens(workspaceOrigin, {
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    expires_at: tokens.expires_at,
    account: account ?? undefined,
  });
}

// ── OAuth token endpoint ─────────────────────────────────────────────────────

async function postToken(tokenUrl, body) {
  const resp = await fetch(tokenUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: body.toString(),
    signal: AbortSignal.timeout(NETWORK_TIMEOUT_MS),
  });
  const text = await resp.text();
  if (!resp.ok) {
    // Carry the status so refresh can detect a dead grant (400/401 invalid_grant)
    // and clear it instead of retrying a consumed single-use refresh token.
    const err = new Error(`token endpoint ${resp.status}: ${text.slice(0, 300)}`);
    err.status = resp.status;
    throw err;
  }
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    throw new Error("token endpoint returned a non-JSON response");
  }
  const accessToken = json.access_token;
  if (typeof accessToken !== "string" || accessToken === "") {
    throw new Error("token endpoint returned no access_token");
  }
  const expiresIn = typeof json.expires_in === "number" ? json.expires_in : 3600;
  return {
    access_token: accessToken,
    refresh_token: typeof json.refresh_token === "string" ? json.refresh_token : undefined,
    expires_at: Math.floor(Date.now() / 1000) + Math.max(0, expiresIn - EXPIRY_SKEW_SECONDS),
  };
}

// The code exchange targets the issuer that produced the code; caller persists.
async function exchangeCode(issuerOrigin, code, verifier, redirectUri) {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: OAUTH_CLIENT_ID,
    code_verifier: verifier,
  });
  if (OAUTH_CLIENT_SECRET) body.set("client_secret", OAUTH_CLIENT_SECRET);
  return postToken(`${issuerOrigin}/oidc/v1/token`, body);
}

/**
 * Token endpoint for a refresh. A workspace-scoped token refreshes at the
 * workspace's own endpoint; an account-scoped (SPOG) token must refresh at the
 * account endpoint WITH the account id in the path — the accounts host serves
 * every account, and a refresh carries no code for the server to resolve it
 * from (mirrors genie-one-desktop's account OIDC discovery).
 */
function refreshEndpoint(workspaceOrigin, account) {
  return account
    ? `${account.origin}/oidc/accounts/${account.id}/v1/token`
    : `${workspaceOrigin}/oidc/v1/token`;
}

// One in-flight refresh per store key. Single-use (rotating) refresh tokens
// can't be spent twice: without this, concurrent windows on one origin would
// each POST the same token, and all but one would get invalid_grant — revoking
// the whole grant. Sharing one promise also makes the persist single-writer.
const inflightRefresh = new Map();

async function doRefresh(workspaceOrigin, entry) {
  const endpoint = refreshEndpoint(workspaceOrigin, entry.account);
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: entry.refresh_token,
    client_id: OAUTH_CLIENT_ID,
  });
  if (OAUTH_CLIENT_SECRET) body.set("client_secret", OAUTH_CLIENT_SECRET);
  console.log(
    `[omnigent] databricks oauth: refreshing token for ${workspaceOrigin} at ${endpoint}`,
  );
  let minted;
  try {
    minted = await postToken(endpoint, body);
  } catch (e) {
    // A dead/consumed grant (reuse detection, or past max lifetime) — clear it so
    // we fall to a fresh login instead of looping on a token the server rejects.
    if (e.status === 400 || e.status === 401) {
      deleteStoredToken(workspaceOrigin);
      console.warn(
        `[omnigent] databricks oauth: grant dead (HTTP ${e.status}) for ${workspaceOrigin}; ` +
          "cleared stored token, will require a fresh login",
      );
    }
    throw e;
  }
  const rotated = typeof minted.refresh_token === "string" && minted.refresh_token !== "";
  const next = {
    access_token: minted.access_token,
    // Rotating servers return a fresh refresh_token (the old one is now spent);
    // non-rotating servers omit it, so keep the working one.
    refresh_token: minted.refresh_token ?? entry.refresh_token,
    expires_at: minted.expires_at,
    account: entry.account,
  };
  saveTokens(workspaceOrigin, next);
  console.log(
    `[omnigent] databricks oauth: refreshed ${workspaceOrigin} ` +
      `(refresh_token rotated=${rotated}, valid ~${Math.max(0, next.expires_at - Math.floor(Date.now() / 1000))}s)`,
  );
  return next;
}

function refreshStoredToken(workspaceOrigin, entry) {
  const key = storeKey(workspaceOrigin);
  const existing = inflightRefresh.get(key);
  if (existing) return existing;
  const p = doRefresh(workspaceOrigin, entry).finally(() => inflightRefresh.delete(key));
  inflightRefresh.set(key, p);
  return p;
}

/**
 * A valid access token for a workspace origin from the store alone — no browser.
 * Returns the stored token if still valid, else refreshes (rotating the refresh
 * token when the server does). Throws if there is nothing stored, or the refresh
 * fails — the caller decides whether to fall back to an interactive login.
 *
 * @param {string} workspaceOrigin
 * @returns {Promise<string>} A bearer access token.
 */
async function getValidStoredToken(workspaceOrigin) {
  const entry = loadTokens(workspaceOrigin);
  if (!entry || typeof entry.access_token !== "string") {
    throw new Error(`no stored Databricks token for ${workspaceOrigin}`);
  }
  const now = Math.floor(Date.now() / 1000);
  // Testing lever: OMNIGENT_DATABRICKS_OAUTH_FORCE_REFRESH=1 treats the stored
  // access token as always expired, so the refresh path runs on every connect
  // and every session-expiry reload — no waiting for real expiry to exercise it.
  const forceRefresh = process.env.OMNIGENT_DATABRICKS_OAUTH_FORCE_REFRESH === "1";
  if (!forceRefresh && typeof entry.expires_at === "number" && entry.expires_at > now) {
    console.log(
      `[omnigent] databricks oauth: using cached token for ${workspaceOrigin} ` +
        `(valid ~${entry.expires_at - now}s)`,
    );
    return entry.access_token;
  }
  if (typeof entry.refresh_token === "string" && entry.refresh_token) {
    console.log(
      `[omnigent] databricks oauth: ${forceRefresh ? "FORCE_REFRESH" : "token expired"} for ` +
        `${workspaceOrigin} → refreshing`,
    );
    return (await refreshStoredToken(workspaceOrigin, entry)).access_token;
  }
  throw new Error(
    `stored Databricks token for ${workspaceOrigin} is expired with no refresh token`,
  );
}

// ── Interactive browser login (loopback redirect) ───────────────────────────

async function runInteractiveLogin(origin) {
  const { redirectBase, scopes } = config();
  // The redirect (and thus the local listener) must be loopback-only — never a
  // wildcard/public bind like http://0.0.0.0, and never a non-http scheme the
  // server can't serve. RFC 8252 §7.3.
  if (!isLoopbackUrl(redirectBase)) {
    throw new Error(
      `OMNIGENT_DATABRICKS_OAUTH_REDIRECT must be an http loopback URL, got: ${redirectBase}`,
    );
  }
  const { verifier, challenge } = makePkce();
  const state = base64url(crypto.randomBytes(24));
  const base = new URL(redirectBase);
  // RFC 8252 loopback: Databricks matches the registered redirect ignoring the
  // port, so bind an ephemeral free port (no fixed number to reserve, no
  // cross-app collision). Only scheme+host+path must match the registration; an
  // explicit port in the config is honored for setups that need a fixed one.
  const fixedPort = base.port ? Number(base.port) : 0;
  const pathPart = base.pathname && base.pathname !== "/" ? base.pathname : "";
  let redirectUri;

  const callback = await new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      let reqUrl;
      try {
        reqUrl = new URL(req.url, base.origin);
      } catch {
        reqUrl = null;
      }
      if (!reqUrl || reqUrl.pathname !== base.pathname) {
        res.writeHead(404);
        res.end();
        return;
      }
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(
        '<html><body style="font-family:system-ui;text-align:center;padding:60px">' +
          "<h2>Signed in to Databricks</h2>" +
          "<p>You can close this tab and return to Omnigent.</p></body></html>",
      );
      const params = reqUrl.searchParams;
      cleanup();
      const err = params.get("error");
      if (err) {
        const desc = params.get("error_description");
        reject(new Error(`authorization error: ${err}${desc ? ` - ${desc}` : ""}`));
        return;
      }
      if (params.get("state") !== state) {
        reject(new Error("state mismatch (possible CSRF)"));
        return;
      }
      const c = params.get("code");
      if (!c) {
        reject(new Error("no code in callback"));
        return;
      }
      resolve({ code: c, iss: params.get("iss") });
    });

    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("timed out waiting for browser login"));
    }, AUTH_TIMEOUT_MS);

    function cleanup() {
      clearTimeout(timer);
      server.close();
    }

    server.on("error", (e) => {
      cleanup();
      reject(e);
    });

    server.listen(fixedPort, base.hostname, () => {
      const port = server.address().port;
      redirectUri = `${base.protocol}//${base.hostname}:${port}${pathPart}`;
      const authQuery = new URLSearchParams({
        response_type: "code",
        client_id: OAUTH_CLIENT_ID,
        redirect_uri: redirectUri,
        scope: scopes,
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
      }).toString();
      // Authorize directly against the entered origin. A workspace host issues a
      // workspace-scoped token; an account/SPOG host issues an account-scoped one
      // (the workspace is chosen afterward from the account workspaces API).
      const authorizeUrl = `${origin}/oidc/v1/authorize?${authQuery}`;
      shell.openExternal(authorizeUrl).then(
        () => console.log("[omnigent] databricks oauth: opened system browser for sign-in"),
        (e) => {
          // Can't hand off to the browser — fail fast instead of waiting out the
          // auth timeout with a window the user can't complete.
          cleanup();
          reject(new Error(`could not open the system browser: ${e.message}`));
        },
      );
    });
  });

  // The origin the token was issued by comes from the issuer (iss, RFC 9207) when
  // present — an account host for an account-scoped token, the workspace host for
  // a workspace-scoped one. Fall back to the entered origin when absent.
  let issuerOrigin = origin;
  if (callback.iss) {
    if (!isTrustedDatabricksOrigin(callback.iss)) {
      throw new Error(`authorization issuer is not a trusted Databricks origin: ${callback.iss}`);
    }
    issuerOrigin = new URL(callback.iss).origin;
  }
  const tokens = await exchangeCode(issuerOrigin, callback.code, verifier, redirectUri);
  return { tokens, issuerOrigin };
}

module.exports = {
  databricksOAuthConfigured,
  runInteractiveLogin,
  getValidStoredToken,
  saveWorkspaceToken,
  loadTokens,
  isTrustedDatabricksOrigin,
};
