// Minimal real-HTTP OIDC IdP for desktop-shell recordings (the desktop analog
// of tests/e2e_ui/auth/_fake_idp.py). Its /authorize page is a passkey-only
// stage issuing the discoverable WebAuthn request a self-hosted IdP's
// authenticator-validation step does, and shows whether it ever settles.

"use strict";

const crypto = require("node:crypto");
const http = require("node:http");
const { URL } = require("node:url");

const FAKE_IDP_EMAIL = "e2e-user@example.test";
const KID = "desktop-e2e-fake-idp-key";

function base64url(input) {
  return Buffer.from(input).toString("base64url");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function signIdToken(privateKey, claims) {
  const header = base64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid: KID }));
  const payload = base64url(JSON.stringify(claims));
  const signature = crypto.sign("sha256", Buffer.from(`${header}.${payload}`), privateKey);
  return `${header}.${payload}.${base64url(signature)}`;
}

function passkeyStagePage({ email, continueUrl, passkeyTimeoutMs }) {
  const safeEmail = escapeHtml(email);
  const safeContinue = escapeHtml(continueUrl);
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Fake IdP Sign-in</title>
  <style>
    body { font-family: sans-serif; padding: 2rem; max-width: 36rem; }
    #webauthn-status { font-size: 1.25rem; padding: 1rem; border: 1px solid #999; }
    #webauthn-status[data-state="pending"] { background: #fff6d5; }
    #webauthn-status[data-state="resolved"] { background: #d8f5d8; }
    #webauthn-status[data-state="rejected"] { background: #f8d7da; }
    #webauthn-elapsed { color: #555; }
  </style>
</head>
<body>
  <h1 id="idp-heading">Fake IdP</h1>
  <h2 id="idp-stage">Passkey sign-in</h2>
  <p>Use your passkey to sign in as <b>${safeEmail}</b>.</p>
  <p id="webauthn-status" data-state="pending">Waiting for your passkey…</p>
  <p id="webauthn-elapsed">Waiting for 0s (passkey request timeout: ${passkeyTimeoutMs} ms)</p>
  <a id="fake-idp-continue" href="${safeContinue}" hidden>Continue as ${safeEmail}</a>
  <script>
    (function () {
      var status = document.getElementById("webauthn-status");
      var elapsed = document.getElementById("webauthn-elapsed");
      var started = Date.now();
      var state = {
        state: "pending", startedAt: started, settledAt: null,
        error: null, uvpaa: null, userAgent: navigator.userAgent,
      };
      window.passkeyCeremony = state;
      setInterval(function () {
        if (state.state !== "pending") return;
        var seconds = Math.round((Date.now() - started) / 1000);
        elapsed.textContent = "Waiting for " + seconds + "s (passkey request timeout: ${passkeyTimeoutMs} ms)";
      }, 1000);
      function settle(kind, text) {
        state.state = kind;
        state.settledAt = Date.now();
        status.dataset.state = kind;
        status.textContent = text;
      }
      (async function () {
        try {
          state.uvpaa = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
        } catch (err) {
          state.uvpaa = String(err);
        }
        var challenge = new Uint8Array(32);
        crypto.getRandomValues(challenge);
        try {
          await navigator.credentials.get({
            publicKey: {
              challenge: challenge,
              timeout: ${passkeyTimeoutMs},
              rpId: location.hostname,
              userVerification: "preferred",
              allowCredentials: [],
            },
          });
          settle("resolved", "Passkey accepted — continuing…");
          document.getElementById("fake-idp-continue").hidden = false;
        } catch (err) {
          state.error = { name: err && err.name, message: err && err.message };
          settle("rejected", "Passkey sign-in failed: " + (err && err.name) + " — " + (err && err.message));
        }
      })();
    })();
  </script>
</body>
</html>`;
}

/**
 * Start the fake IdP on a loopback port.
 *
 * @param {object} [opts]
 * @param {string} [opts.clientId]
 * @param {string} [opts.clientSecret]
 * @param {number} [opts.passkeyTimeoutMs] The RP timeout the /authorize page
 *   passes to `navigator.credentials.get`.
 * @returns {Promise<{ issuer: string, clientId: string, clientSecret: string,
 *   email: string,
 *   requests: Array<{ method: string, path: string, at: number, userAgent: string }>,
 *   close: () => Promise<void> }>}
 */
async function startFakeIdp(opts = {}) {
  const clientId = opts.clientId ?? "desktop-e2e-client";
  const clientSecret = opts.clientSecret ?? "desktop-e2e-secret";
  const passkeyTimeoutMs = opts.passkeyTimeoutMs ?? 1500;
  const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: KID, use: "sig", alg: "RS256" };
  const requests = [];
  let issuer = "";

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, issuer);
    requests.push({
      method: req.method,
      path: url.pathname,
      at: Date.now(),
      userAgent: req.headers["user-agent"] ?? "",
    });
    const json = (body, status = 200) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    };
    if (url.pathname === "/.well-known/openid-configuration") {
      json({
        issuer,
        authorization_endpoint: `${issuer}/authorize`,
        token_endpoint: `${issuer}/token`,
        jwks_uri: `${issuer}/jwks`,
        userinfo_endpoint: `${issuer}/userinfo`,
        response_types_supported: ["code"],
        subject_types_supported: ["public"],
        id_token_signing_alg_values_supported: ["RS256"],
      });
      return;
    }
    if (url.pathname === "/jwks") {
      json({ keys: [jwk] });
      return;
    }
    if (url.pathname === "/authorize") {
      const redirectUri = url.searchParams.get("redirect_uri") ?? "";
      const state = url.searchParams.get("state") ?? "";
      const continueUrl = `${redirectUri}?code=fake-auth-code&state=${encodeURIComponent(state)}`;
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(passkeyStagePage({ email: FAKE_IDP_EMAIL, continueUrl, passkeyTimeoutMs }));
      return;
    }
    if (url.pathname === "/token" && req.method === "POST") {
      req.resume();
      req.on("end", () => {
        const now = Math.floor(Date.now() / 1000);
        const idToken = signIdToken(privateKey, {
          iss: issuer,
          aud: clientId,
          sub: "fake-idp-subject",
          email: FAKE_IDP_EMAIL,
          email_verified: true,
          iat: now,
          auth_time: now,
          exp: now + 300,
        });
        json({
          access_token: "fake-access-token",
          token_type: "Bearer",
          expires_in: 300,
          id_token: idToken,
        });
      });
      return;
    }
    json({ error: "not found" }, 404);
  });

  await new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  // WebAuthn needs a registrable-domain origin; an IP literal is rejected.
  issuer = `http://localhost:${server.address().port}`;

  return {
    issuer,
    clientId,
    clientSecret,
    email: FAKE_IDP_EMAIL,
    requests,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}

/**
 * Env that puts a harness-spawned `omnigent server` into stock OIDC mode
 * against `idp`, mirroring tests/e2e_ui/auth/_oidc_server.py.
 *
 * @param {{ issuer: string, clientId: string, clientSecret: string }} idp
 * @param {string} serverUrl The server's own origin (for the redirect URI).
 * @returns {Record<string, string>}
 */
function oidcServerEnv(idp, serverUrl) {
  return {
    OMNIGENT_AUTH_PROVIDER: "oidc",
    OMNIGENT_AUTH_ENABLED: "1",
    OMNIGENT_LOCAL_SINGLE_USER: "",
    OMNIGENT_OIDC_ISSUER: idp.issuer,
    OMNIGENT_OIDC_CLIENT_ID: idp.clientId,
    OMNIGENT_OIDC_CLIENT_SECRET: idp.clientSecret,
    OMNIGENT_OIDC_REDIRECT_URI: `${serverUrl}/auth/callback`,
    OMNIGENT_OIDC_COOKIE_SECRET: crypto.randomBytes(32).toString("hex"),
  };
}

module.exports = { FAKE_IDP_EMAIL, startFakeIdp, oidcServerEnv };
