// Stand-in Databricks Apps front door: sends signed-out visitors to an Okta-style
// WebAuthn factor page, then proxies signed-in traffic to `upstreamUrl`.

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SESSION_COOKIE = "fake_apps_session";
const CREDENTIAL_ID = Buffer.from("desktop-e2e-security-key").toString("base64url");

const FACTOR_PAGE = `<!doctype html>
<html><head><meta charset="utf-8"><title>Sign In</title>
<style>
body{font-family:sans-serif;background:#f4f4f4;margin:0;display:flex;justify-content:center}
main{background:#fff;margin-top:120px;padding:40px 48px;width:420px;border-radius:6px;box-shadow:0 2px 8px #0002}
h1{font-size:22px;text-align:center}p{line-height:1.5;color:#333}a{color:#1662dd;display:block;margin-top:12px}
small{display:block;margin-top:28px;color:#888}
</style></head>
<body><main>
<h1>Verify with Security Key or Biometric Authenticator</h1>
<p>You will be prompted to use a security key or biometric verification (Windows Hello, Touch ID, etc.). Follow the instructions to complete verification.</p>
<p id="status">Waiting for your security key or biometric prompt…</p>
<a href="#">Can't verify?</a><a href="#">Back to sign in</a>
<small>Local stand-in identity provider for an automated test.</small>
</main>
<script>
const report = (event, detail = {}) =>
  fetch("/events", { method: "POST", body: JSON.stringify({ event, ...detail }) });
const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
(async () => {
  await report("get_started");
  try {
    await navigator.credentials.get({ publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId: location.hostname,
      allowCredentials: [{ type: "public-key", id: b64(${JSON.stringify(CREDENTIAL_ID)}),
        transports: ["usb", "nfc", "internal", "hybrid"] }],
      userVerification: "preferred",
      timeout: 120000,
    } });
    await report("get_resolved");
    document.getElementById("status").textContent = "Verified.";
    location.href = "/signin/done" + location.search;
  } catch (error) {
    await report("get_rejected", { name: error.name, message: error.message });
    document.getElementById("status").textContent = "Verification failed: " + error.name;
  }
})();
</script></body></html>`;

function generateCertificate(dir, hostname) {
  const keyPath = path.join(dir, "front-door-key.pem");
  const certPath = path.join(dir, "front-door-cert.pem");
  const result = spawnSync(
    "openssl",
    [
      "req",
      "-x509",
      "-newkey",
      "ec",
      "-pkeyopt",
      "ec_paramgen_curve:prime256v1",
      "-nodes",
      "-days",
      "1",
      "-subj",
      `/CN=${hostname}`,
      "-addext",
      `subjectAltName=DNS:${hostname}`,
      "-keyout",
      keyPath,
      "-out",
      certPath,
    ],
    { encoding: "utf8" },
  );
  if (result.status !== 0) {
    throw new Error(`openssl could not create the front-door certificate: ${result.stderr}`);
  }
  return { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) };
}

const hasSession = (req) =>
  (req.headers.cookie ?? "").split(";").some((part) => part.trim() === `${SESSION_COOKIE}=ok`);

/**
 * @param {{ upstreamUrl: string, scratchDir: string }} options
 * @returns {Promise<{ serverUrl: string, idpOrigin: string, credentialId: string,
 *   events: Array<{ event: string, at: number }>, preloadPath: string,
 *   launchEnv: Record<string, string>, holdSignIn: () => void, releaseSignIn: () => void,
 *   close: () => Promise<void> }>}
 */
async function startFakeAppsFrontDoor({ upstreamUrl, scratchDir }) {
  const hostname = "omnigent-repro.aws.databricksapps.com";
  const upstream = new URL(upstreamUrl);
  const serverUrl = `https://${hostname}`;
  const events = [];
  const tls = generateCertificate(scratchDir, hostname);
  let idpOrigin;
  // While set, the sign-in page waits after its key answers instead of navigating.
  let heldSignIns = null;

  const idp = http.createServer((req, res) => {
    const url = new URL(req.url, idpOrigin);
    if (req.method === "GET" && url.pathname === "/signin/verify/webauthn") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(FACTOR_PAGE);
      return;
    }
    if (req.method === "POST" && url.pathname === "/events") {
      let body = "";
      req.on("data", (chunk) => {
        body += chunk;
      });
      req.on("end", () => {
        const report = JSON.parse(body || "{}");
        events.push({ ...report, at: Date.now() });
        if (heldSignIns && report.event === "get_resolved") heldSignIns.push(res);
        else res.writeHead(204).end();
      });
      return;
    }
    if (req.method === "GET" && url.pathname === "/signin/done") {
      const back = new URL("/.auth/callback", serverUrl);
      back.searchParams.set("return", url.searchParams.get("return") ?? "/");
      res.writeHead(302, { Location: back.toString() });
      res.end();
      return;
    }
    res.writeHead(404).end();
  });
  await new Promise((resolve) => {
    idp.listen(0, "127.0.0.1", resolve);
  });
  idpOrigin = `http://localhost:${idp.address().port}`;

  const frontDoor = https.createServer({ key: tls.key, cert: tls.cert }, (req, res) => {
    const url = new URL(req.url, serverUrl);
    if (url.pathname === "/.auth/callback") {
      const back = url.searchParams.get("return") ?? "/";
      res.writeHead(302, {
        Location: /^\/(?![/\\])/.test(back) ? back : "/",
        "Set-Cookie": `${SESSION_COOKIE}=ok; Path=/; Secure; HttpOnly; SameSite=Lax`,
      });
      res.end();
      return;
    }
    if (!hasSession(req)) {
      const signIn = new URL("/signin/verify/webauthn", idpOrigin);
      signIn.searchParams.set("return", req.url);
      res.writeHead(302, { Location: signIn.toString() });
      res.end();
      return;
    }
    const proxied = http.request(
      {
        host: upstream.hostname,
        port: upstream.port,
        method: req.method,
        path: req.url,
        headers: { ...req.headers, "x-forwarded-proto": "https" },
      },
      (upstreamRes) => {
        res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
        upstreamRes.pipe(res);
      },
    );
    proxied.on("error", () => res.destroy());
    req.pipe(proxied);
  });
  frontDoor.on("upgrade", (req, socket, head) => {
    if (!hasSession(req)) {
      socket.end("HTTP/1.1 401 Unauthorized\r\n\r\n");
      return;
    }
    const target = net.connect(Number(upstream.port), upstream.hostname, () => {
      const headerLines = [];
      for (let i = 0; i < req.rawHeaders.length; i += 2) {
        headerLines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`);
      }
      target.write(`${req.method} ${req.url} HTTP/1.1\r\n${headerLines.join("\r\n")}\r\n\r\n`);
      target.write(head);
      socket.pipe(target).pipe(socket);
    });
    target.on("error", () => socket.destroy());
    socket.on("error", () => target.destroy());
  });
  await new Promise((resolve) => {
    frontDoor.listen(0, "127.0.0.1", resolve);
  });
  const frontDoorPort = frontDoor.address().port;

  // Main-process preload: route the Apps hostname to the local front door and
  // trust only its throwaway certificate.
  const preloadPath = path.join(scratchDir, "fake-apps-host.cjs");
  const fingerprint = new crypto.X509Certificate(tls.cert).fingerprint256;
  fs.writeFileSync(
    preloadPath,
    `const { app, session } = require("electron");\n` +
      `app.commandLine.appendSwitch("host-resolver-rules", ${JSON.stringify(
        `MAP ${hostname} 127.0.0.1:${frontDoorPort}`,
      )});\n` +
      `app.whenReady().then(() => session.defaultSession.setCertificateVerifyProc((request, callback) => {\n` +
      `  const trusted = request.hostname === ${JSON.stringify(hostname)} &&\n` +
      `    request.certificate.fingerprint === "sha256/" + Buffer.from(${JSON.stringify(
        fingerprint.replace(/:/g, ""),
      )}, "hex").toString("base64");\n` +
      `  callback(trusted ? 0 : -3);\n` +
      `}));\n`,
  );

  // Keep the mapped hostname off any ambient egress proxy.
  const noProxy = [process.env.no_proxy ?? process.env.NO_PROXY, hostname]
    .filter(Boolean)
    .join(",");

  return {
    serverUrl,
    launchEnv: { no_proxy: noProxy, NO_PROXY: noProxy },
    idpOrigin,
    credentialId: CREDENTIAL_ID,
    events,
    preloadPath,
    holdSignIn: () => {
      heldSignIns = [];
    },
    releaseSignIn: () => {
      for (const res of heldSignIns ?? []) res.writeHead(204).end();
      heldSignIns = null;
    },
    close: async () => {
      for (const server of [frontDoor, idp]) {
        server.closeAllConnections?.();
        // oxlint-disable-next-line no-await-in-loop -- close each server in turn.
        await new Promise((resolve) => {
          server.close(() => resolve());
        });
      }
    },
  };
}

module.exports = { startFakeAppsFrontDoor };
