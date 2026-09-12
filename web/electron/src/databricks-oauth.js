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
//   OMNIGENT_DATABRICKS_OAUTH_SCOPES    (default "all-apis offline_access")
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
// Bound on how long we wait for the human to finish logging in in the browser.
const AUTH_TIMEOUT_MS = 300_000;
// Renew a little before real expiry so a mint isn't racing the clock.
const EXPIRY_SKEW_SECONDS = 60;

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

// ── OAuth token endpoint ─────────────────────────────────────────────────────

async function postToken(origin, body) {
  const resp = await fetch(`${origin}/oidc/v1/token`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: body.toString(),
  });
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(`token endpoint ${resp.status}: ${text.slice(0, 300)}`);
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

async function exchangeCode(origin, code, verifier, redirectUri) {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: OAUTH_CLIENT_ID,
    code_verifier: verifier,
  });
  const tokens = await postToken(origin, body);
  saveTokens(origin, tokens);
  return tokens;
}

async function refreshTokens(origin, refreshToken) {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: OAUTH_CLIENT_ID,
  });
  const tokens = await postToken(origin, body);
  // A refresh response may omit a fresh refresh_token; keep the working one.
  if (!tokens.refresh_token) tokens.refresh_token = refreshToken;
  saveTokens(origin, tokens);
  return tokens;
}

// ── Interactive browser login (loopback redirect) ───────────────────────────

async function runInteractiveLogin(origin) {
  const { redirectBase, scopes } = config();
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
      void shell.openExternal(authorizeUrl);
      console.log("[omnigent] databricks oauth: opened system browser for sign-in");
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
  return { tokens, workspaceOrigin: issuerOrigin };
}

/**
 * Return a valid access token plus the origin it was issued by, minting or
 * refreshing as needed. The issuer can differ from the entered origin (an
 * account host issues an account-scoped token), so callers read the returned
 * ``workspaceOrigin``. With ``interactive: false`` (the session-expiry path) it
 * never opens a browser — it uses a stored/refreshable token or throws.
 *
 * @param {string} origin The entered origin (account host or workspace host).
 * @param {{ interactive?: boolean }} [opts]
 * @returns {Promise<{ accessToken: string, workspaceOrigin: string }>}
 */
async function getValidAccessToken(origin, { interactive = true } = {}) {
  const stored = loadTokens(origin);
  const now = Math.floor(Date.now() / 1000);
  if (
    stored &&
    typeof stored.access_token === "string" &&
    typeof stored.expires_at === "number" &&
    stored.expires_at > now
  ) {
    return { accessToken: stored.access_token, workspaceOrigin: origin };
  }
  if (stored && typeof stored.refresh_token === "string" && stored.refresh_token) {
    try {
      const t = await refreshTokens(origin, stored.refresh_token);
      return { accessToken: t.access_token, workspaceOrigin: origin };
    } catch (e) {
      console.warn("[omnigent] databricks token refresh failed:", e.message);
      if (!interactive) throw e;
    }
  }
  if (!interactive) {
    throw new Error("no valid Databricks token and interactive login is disabled");
  }
  const { tokens, workspaceOrigin } = await runInteractiveLogin(origin);
  return { accessToken: tokens.access_token, workspaceOrigin };
}

module.exports = {
  databricksOAuthConfigured,
  getValidAccessToken,
  runInteractiveLogin,
  refreshTokens,
  loadTokens,
};
