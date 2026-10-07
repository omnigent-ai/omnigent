// Stand-in for an SSO-gated app front door (Databricks Apps style) and its IdP:
// unauthenticated requests go 302 → authorize → a WebAuthn "Verify with Security
// Key or Biometric Authenticator" step. The stand-in system browser
// (fakeSystemBrowser.cjs) completes that prompt so a sign-in that leaves the app
// window can finish.

"use strict";

const crypto = require("node:crypto");
const http = require("node:http");

const SESSION_COOKIE = "fd_session";
const FAKE_BROWSER_UA = "fake-system-browser";
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );

function listen(server) {
  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, () => resolve(server.address().port));
  });
}

function closeServer(server) {
  return new Promise((resolve) => {
    server.closeAllConnections?.();
    server.close(() => resolve());
  });
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => resolve(body));
  });
}

/** The IdP's security-key / biometric step, modelled on Okta's page text. */
function verifyPage({ email, completeUrl }) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign In</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #203138; font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  .card { width: 400px; background: #fff; border-radius: 4px; padding: 40px 42px; color: #1d1d21; }
  .logo { width: 48px; height: 48px; border-radius: 50%; background: #203138; margin: 0 auto 28px; }
  h1 { font-size: 20px; font-weight: 600; text-align: center; margin: 0 0 12px; line-height: 1.3; }
  .who { text-align: center; color: #6e6e78; font-size: 14px; margin-bottom: 36px; }
  p { font-size: 14px; line-height: 1.5; margin: 0 0 24px; }
  .status { font-size: 13px; color: #6e6e78; border-top: 1px solid #ddd; padding-top: 16px; min-height: 36px; }
  a { color: #0074b3; font-size: 14px; display: block; margin-top: 10px; text-decoration: none; }
</style>
</head>
<body>
  <div class="card">
    <div class="logo"></div>
    <h1>Verify with Security Key or Biometric Authenticator</h1>
    <div class="who">${escapeHtml(email)}</div>
    <p>You will be prompted to use a security key or biometric verification (Windows Hello,
       Touch ID, etc.). Follow the instructions to complete verification.</p>
    <div class="status" id="status">Starting verification…</div>
    <a href="#">Can't verify?</a>
    <a href="#">Back to sign in</a>
  </div>
<script>
(async () => {
  const completeUrl = ${JSON.stringify(completeUrl)};
  const status = document.getElementById("status");
  const state = (window.webauthnCeremony = {
    uvpaa: null, state: "starting", error: null, startedAt: Date.now(), settledAt: null,
  });
  const report = () =>
    fetch("/webauthn-result", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...state, userAgent: navigator.userAgent }),
    }).catch(() => {});
  try {
    state.uvpaa = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch (error) {
    state.uvpaa = "error:" + error.name;
  }
  state.state = "waiting";
  report();
  const ticker = setInterval(() => {
    const seconds = Math.round((Date.now() - state.startedAt) / 1000);
    status.textContent = "Waiting for your security key or biometric prompt… " + seconds + " s" +
      (state.uvpaa === true ? "" : " (no biometric authenticator available to this page)");
  }, 1000);
  try {
    await navigator.credentials.get({
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rpId: "localhost",
        timeout: 10000,
        userVerification: "required",
        allowCredentials: [{ type: "public-key", id: new Uint8Array(16), transports: ["usb", "internal"] }],
      },
    });
    state.state = "resolved";
    state.settledAt = Date.now();
    clearInterval(ticker);
    status.textContent = "Verified. Redirecting…";
    await report();
    location.assign(completeUrl);
  } catch (error) {
    state.state = "rejected";
    state.error = error.name + ": " + error.message;
    state.settledAt = Date.now();
    clearInterval(ticker);
    status.textContent = "Verification failed: " + state.error;
    report();
  }
})();
</script>
</body>
</html>`;
}

/**
 * @param {{ email?: string }} [options]
 * @returns {Promise<{ issuer: string, email: string,
 *   verifyRequests: Array<{ userAgent: string, url: string }>,
 *   ceremonyEvents: Array<Record<string, unknown>>,
 *   consumeCode: (code: string) => boolean, close: () => Promise<void> }>}
 */
async function startFakeIdp({ email = "desktop-user@example.test" } = {}) {
  const codes = new Set();
  const verifyRequests = [];
  const ceremonyEvents = [];
  let issuer;

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, issuer);
    if (req.method === "GET" && url.pathname === "/authorize") {
      const userAgent = req.headers["user-agent"] ?? "";
      verifyRequests.push({ userAgent, url: url.toString() });
      const code = crypto.randomBytes(16).toString("hex");
      codes.add(code);
      const back = new URL(url.searchParams.get("redirect_uri"));
      back.searchParams.set("code", code);
      back.searchParams.set("state", url.searchParams.get("state") ?? "");
      if (userAgent === FAKE_BROWSER_UA) {
        // A real browser shows its WebAuthn prompt; the stand-in stands for a person completing it.
        res.writeHead(302, { Location: back.toString() });
        res.end();
        return;
      }
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(verifyPage({ email, completeUrl: back.toString() }));
      return;
    }
    if (req.method === "POST" && url.pathname === "/webauthn-result") {
      const body = await readBody(req);
      try {
        ceremonyEvents.push({ ...JSON.parse(body), receivedAt: Date.now() });
      } catch {
        ceremonyEvents.push({ raw: body, receivedAt: Date.now() });
      }
      res.writeHead(204);
      res.end();
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const port = await listen(server);
  // WebAuthn needs a secure context with a domain RP ID: http://localhost qualifies, an IP does not.
  issuer = `http://localhost:${port}`;
  return {
    issuer,
    email,
    verifyRequests,
    ceremonyEvents,
    consumeCode: (code) => codes.delete(code),
    close: () => closeServer(server),
  };
}

/**
 * @param {{ idp: Awaited<ReturnType<typeof startFakeIdp>>, upstreamUrl: string }} options
 * @returns {Promise<{ url: string, requests: Array<{ method: string, path: string, authed: boolean }>,
 *   close: () => Promise<void> }>}
 */
async function startFakeFrontDoor({ idp, upstreamUrl }) {
  const sessions = new Set();
  const requests = [];
  let origin;

  const sessionOf = (req) => {
    const header = req.headers.cookie ?? "";
    for (const pair of header.split(";")) {
      const [name, ...rest] = pair.trim().split("=");
      if (name === SESSION_COOKIE && sessions.has(rest.join("="))) return rest.join("=");
    }
    return null;
  };

  const proxy = (req, res) => {
    const base = new URL(upstreamUrl);
    const incoming = new URL(req.url, base);
    const headers = {};
    for (const [name, value] of Object.entries(req.headers)) {
      if (!HOP_BY_HOP.has(name)) headers[name] = value;
    }
    headers.host = base.host;
    // Pin the destination to the upstream host; the request only supplies the path.
    const options = {
      protocol: base.protocol,
      hostname: base.hostname,
      port: base.port,
      path: `${incoming.pathname}${incoming.search}`,
      method: req.method,
      headers,
    };
    const upstream = http.request(options, (upstreamRes) => {
      const out = {};
      for (const [name, value] of Object.entries(upstreamRes.headers)) {
        if (!HOP_BY_HOP.has(name)) out[name] = value;
      }
      res.writeHead(upstreamRes.statusCode ?? 502, out);
      upstreamRes.pipe(res);
    });
    upstream.on("error", () => {
      if (!res.headersSent) res.writeHead(502);
      res.end();
    });
    req.pipe(upstream);
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, origin);
    const authed = sessionOf(req) !== null;
    requests.push({ method: req.method, path: url.pathname, authed });
    if (url.pathname === "/.auth/callback") {
      if (!idp.consumeCode(url.searchParams.get("code") ?? "")) {
        res.writeHead(400, { "Content-Type": "text/plain" });
        res.end("invalid code");
        return;
      }
      const token = crypto.randomBytes(16).toString("hex");
      sessions.add(token);
      let next = "/";
      try {
        next = new URL(url.searchParams.get("state") ?? "/", origin).pathname;
      } catch {
        // keep "/"
      }
      res.writeHead(302, {
        "Set-Cookie": `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax`,
        Location: next,
      });
      res.end();
      return;
    }
    if (authed) {
      proxy(req, res);
      return;
    }
    if (url.pathname === "/oidc/oauth2/v2.0/authorize") {
      const authorize = new URL("/authorize", idp.issuer);
      authorize.searchParams.set("client_id", "omnigent-app");
      authorize.searchParams.set("response_type", "code");
      authorize.searchParams.set("scope", "openid email");
      authorize.searchParams.set("redirect_uri", `${origin}/.auth/callback`);
      authorize.searchParams.set("state", url.searchParams.get("next") ?? "/");
      res.writeHead(302, { Location: authorize.toString() });
      res.end();
      return;
    }
    // The front door gates every path, the manifest included, like the Apps proxy.
    const login = new URL("/oidc/oauth2/v2.0/authorize", origin);
    login.searchParams.set("next", url.pathname);
    res.writeHead(302, { Location: login.toString() });
    res.end();
  });
  const port = await listen(server);
  origin = `http://localhost:${port}`;
  return { url: origin, requests, close: () => closeServer(server) };
}

module.exports = { startFakeIdp, startFakeFrontDoor, SESSION_COOKIE, FAKE_BROWSER_UA };
