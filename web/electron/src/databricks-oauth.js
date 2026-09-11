// Databricks OAuth (PKCE + loopback) for the desktop shell.
//
// Runs the RFC 8252 native-app authorization-code flow against a Databricks
// workspace's OIDC endpoints in the user's SYSTEM browser (never an embedded
// webview), and holds the resulting access/refresh tokens. databricks-session.js
// exchanges the access token for a DBAUTH web-session cookie. This lets the shell
// stop driving Databricks login inside its own BrowserWindow, which SSO providers
// and Databricks itself are locking down.
//
// Config comes from the environment (a first-party OAuth app the operator
// registers on the workspace/account):
//   OMNIGENT_DATABRICKS_OAUTH_CLIENT_ID      (required)
//   OMNIGENT_DATABRICKS_OAUTH_CLIENT_SECRET  (confidential clients; omit for PKCE-only)
//   OMNIGENT_DATABRICKS_OAUTH_REDIRECT       (default http://localhost; must match the app's
//                                             registered loopback redirect. The port is bound
//                                             ephemerally — Databricks ignores it per RFC 8252 —
//                                             unless you pin one, e.g. http://localhost:8020)
//   OMNIGENT_DATABRICKS_OAUTH_SCOPES         (default "all-apis offline_access")
//   OMNIGENT_DATABRICKS_OAUTH_SPOG=1         (single-pane-of-glass entry: route authorize through
//                                             the login host so the user picks an account+workspace
//                                             and the token comes back WORKSPACE-scoped. Off = the
//                                             entered URL must already be a specific workspace.)
//   OMNIGENT_DATABRICKS_LOGIN_URL            (SISU login host for SPOG account-first login; default
//                                             https://login.databricks.com. Set to a staging SISU
//                                             host when testing SPOG on staging.)

"use strict";

const http = require("node:http");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { app, shell, safeStorage } = require("electron");

const DEFAULT_REDIRECT_BASE = "http://localhost";
const DEFAULT_SCOPES = "all-apis offline_access";
// SISU login host for the SPOG account-first picker (mirrors DB One). Prod; set
// OMNIGENT_DATABRICKS_LOGIN_URL to a staging SISU host when testing on staging.
const DEFAULT_LOGIN_URL = "https://login.databricks.com";
// Bound on how long we wait for the human to finish logging in in the browser.
const AUTH_TIMEOUT_MS = 300_000;
// Renew a little before real expiry so a mint isn't racing the clock.
const EXPIRY_SKEW_SECONDS = 60;

// Hostname suffixes that mark a trusted Databricks origin. The leading dot stops
// look-alikes (evil-databricks.com, databricks.com.attacker.net) from matching.
// Covers workspace hosts (…cloud.databricks.com, …gcp.databricks.com) and account
// hosts (accounts.…databricks.com) since all end in one of these.
const TRUSTED_HOST_SUFFIXES = [".databricks.com", ".azuredatabricks.net"];

/** Read the OAuth config from the environment. */
function config() {
  return {
    clientId: (process.env.OMNIGENT_DATABRICKS_OAUTH_CLIENT_ID ?? "").trim(),
    clientSecret: (process.env.OMNIGENT_DATABRICKS_OAUTH_CLIENT_SECRET ?? "").trim(),
    redirectBase: (process.env.OMNIGENT_DATABRICKS_OAUTH_REDIRECT ?? DEFAULT_REDIRECT_BASE).trim(),
    scopes: (process.env.OMNIGENT_DATABRICKS_OAUTH_SCOPES ?? DEFAULT_SCOPES).trim(),
    // Empty unless explicitly set — SPOG then defaults the login host to the entered origin.
    loginUrl: (process.env.OMNIGENT_DATABRICKS_LOGIN_URL ?? "").trim().replace(/\/+$/, ""),
    spog: process.env.OMNIGENT_DATABRICKS_OAUTH_SPOG === "1",
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

/** Whether OAuth is configured enough to attempt (a client_id is the minimum). */
function databricksOAuthConfigured() {
  return config().clientId !== "";
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
    console.warn("[omnigent] safeStorage unavailable; storing Databricks tokens unencrypted (0600)");
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

/**
 * Decode and log a JWT access token's scope/audience (payload only — never the
 * token itself). Diagnostic: /auth/session/create's route-level check requires
 * the `all-apis` scope, so this shows whether the minted token actually has it.
 */
function logTokenScopes(accessToken) {
  try {
    const parts = String(accessToken).split(".");
    if (parts.length !== 3) {
      console.log("[omnigent] databricks oauth: token is opaque (not a JWT); cannot read scopes");
      return;
    }
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
    const scope = payload.scope ?? payload.scp ?? "(no scope claim)";
    console.log(`[omnigent] databricks oauth: token scope="${scope}" aud="${payload.aud ?? ""}"`);
  } catch (e) {
    console.warn("[omnigent] databricks oauth: could not decode token scopes:", e.message);
  }
}

async function exchangeCode(origin, code, verifier, redirectUri) {
  const { clientId, clientSecret } = config();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: clientId,
    code_verifier: verifier,
  });
  if (clientSecret) body.set("client_secret", clientSecret);
  const tokens = await postToken(origin, body);
  saveTokens(origin, tokens);
  console.log("[omnigent] databricks oauth: access token minted");
  logTokenScopes(tokens.access_token);
  return tokens;
}

async function refreshTokens(origin, refreshToken) {
  const { clientId, clientSecret } = config();
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: clientId,
  });
  if (clientSecret) body.set("client_secret", clientSecret);
  const tokens = await postToken(origin, body);
  // A refresh response may omit a fresh refresh_token; keep the working one.
  if (!tokens.refresh_token) tokens.refresh_token = refreshToken;
  saveTokens(origin, tokens);
  console.log("[omnigent] databricks oauth: access token refreshed");
  return tokens;
}

// ── Interactive browser login (loopback redirect) ───────────────────────────

async function runInteractiveLogin(origin) {
  const { clientId, redirectBase, scopes, loginUrl, spog } = config();
  if (!clientId) throw new Error("OMNIGENT_DATABRICKS_OAUTH_CLIENT_ID is not set");
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
        "<html><body style=\"font-family:system-ui;text-align:center;padding:60px\">" +
          "<h2>Signed in to Databricks</h2>" +
          "<p>You can close this tab and return to Omnigent.</p></body></html>",
      );
      const params = reqUrl.searchParams;
      cleanup();
      // Full callback shape (code redacted) — the single most useful debug line:
      // shows whether iss came back (SPOG) and whether the picker round-tripped.
      console.log(
        `[omnigent] databricks oauth: callback hasCode=${params.has("code")} ` +
          `state=${params.get("state") === state ? "ok" : "MISMATCH"} ` +
          `iss=${params.get("iss") ?? "(none)"} error=${params.get("error") ?? "(none)"}`,
      );
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
      // Standard PKCE authorize request as a RELATIVE path — in SPOG mode this
      // rides inside the login host's destination_url so the picker resolves a
      // workspace before this authorize runs (yielding a workspace-scoped token).
      const authQuery = new URLSearchParams({
        response_type: "code",
        client_id: clientId,
        redirect_uri: redirectUri,
        scope: scopes,
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
      }).toString();
      const authPath = `/oidc/v1/authorize?${authQuery}`;
      // SPOG (account-first, mirrors genie-one-desktop accountSelector): enter at
      // the SISU login host with target=ACCOUNT. That flag is what makes SISU
      // resolve the account's cloud host and redirect the code back to our
      // loopback — WITHOUT it SISU can't resolve the relative destination_url and
      // never redirects (it just lands on the account console). The token comes
      // back ACCOUNT-scoped (issuer = account host); the workspace is chosen
      // afterward from the account workspaces API. NO isMobile — that routes
      // through the /mobile-redirect bounce page; desktop wants a direct loopback
      // redirect. Login host is login.databricks.com unless
      // OMNIGENT_DATABRICKS_LOGIN_URL overrides it (e.g. a staging SISU host).
      // Non-SPOG authorizes straight against the entered origin.
      const loginHost = loginUrl || DEFAULT_LOGIN_URL;
      const authorizeUrl = spog
        ? `${loginHost}/?destination_url=${encodeURIComponent(authPath)}` +
          `&target=ACCOUNT&l=${encodeURIComponent(app.getLocale())}`
        : `${origin}${authPath}`;
      console.log(
        `[omnigent] databricks oauth: mode=${spog ? `SPOG(login-host=${loginHost})` : "workspace-direct"} ` +
          `entered=${origin} redirect_uri=${redirectUri}`,
      );
      console.log(`[omnigent] databricks oauth: authorize URL = ${authorizeUrl}`);
      void shell.openExternal(authorizeUrl);
      console.log("[omnigent] databricks oauth: opened system browser; waiting for login…");
    });
  });

  // The workspace the token is scoped to comes from the issuer (iss, RFC 9207)
  // when present — that's how SPOG conveys which workspace the user picked.
  // Fall back to the entered origin for the workspace-direct flow.
  let workspaceOrigin = origin;
  if (callback.iss) {
    if (!isTrustedDatabricksOrigin(callback.iss)) {
      throw new Error(`authorization issuer is not a trusted Databricks origin: ${callback.iss}`);
    }
    workspaceOrigin = new URL(callback.iss).origin;
    console.log(`[omnigent] databricks oauth: issuer=${callback.iss} -> workspace origin ${workspaceOrigin}`);
  } else if (spog) {
    console.warn(
      `[omnigent] databricks oauth: SPOG mode but callback carried no iss; ` +
        `falling back to entered origin ${origin} (token may be account-scoped)`,
    );
  }

  console.log(`[omnigent] databricks oauth: exchanging code at ${workspaceOrigin}/oidc/v1/token`);
  const tokens = await exchangeCode(workspaceOrigin, callback.code, verifier, redirectUri);
  return { tokens, workspaceOrigin };
}

/**
 * Return a valid access token plus the workspace origin it is scoped to, minting
 * or refreshing as needed. In SPOG mode an interactive login can resolve a
 * DIFFERENT workspace origin than the one entered (the user picks it), so callers
 * must use the returned ``workspaceOrigin`` for the session-create call and the
 * window load. With ``interactive: false`` (the session-expiry path) it never
 * opens a browser — it uses a stored/refreshable token or throws.
 *
 * @param {string} origin The entered origin (SPOG/account host or workspace host).
 * @param {{ interactive?: boolean, forceLogin?: boolean }} [opts]
 * @returns {Promise<{ accessToken: string, workspaceOrigin: string }>}
 */
async function getValidAccessToken(origin, { interactive = true, forceLogin = false } = {}) {
  if (forceLogin) {
    // Testing: skip any cached/refreshable token and run the full browser flow.
    const { tokens, workspaceOrigin } = await runInteractiveLogin(origin);
    return { accessToken: tokens.access_token, workspaceOrigin };
  }
  const stored = loadTokens(origin);
  const now = Math.floor(Date.now() / 1000);
  if (
    stored &&
    typeof stored.access_token === "string" &&
    typeof stored.expires_at === "number" &&
    stored.expires_at > now
  ) {
    console.log(`[omnigent] databricks oauth: using cached token for ${origin}`);
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
