// Stand-in Databricks workspace for desktop e2e runs: browser OAuth, the
// /auth/session/create cookie bridge, and a DBAUTH-gated proxy of /omnigent to a
// local server. `setVpn(false)` answers like a workspace IP access list off the VPN.
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const IP_ACL_BODY = JSON.stringify({
  error_code: "403",
  message: "Source IP address: 203.0.113.7 is blocked by Databricks IP ACL for workspace: 1",
});

function selfSignedCertificate(host) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fake-dbx-tls-"));
  const key = path.join(dir, "key.pem");
  const cert = path.join(dir, "cert.pem");
  execFileSync(
    "openssl",
    [
      "req",
      "-x509",
      "-newkey",
      "rsa:2048",
      "-nodes",
      "-keyout",
      key,
      "-out",
      cert,
      "-days",
      "1",
      "-subj",
      `/CN=${host}`,
      "-addext",
      `subjectAltName=DNS:${host}`,
    ],
    { stdio: "ignore", timeout: 30_000 },
  );
  const pair = { key: fs.readFileSync(key), cert: fs.readFileSync(cert) };
  fs.rmSync(dir, { recursive: true, force: true });
  return pair;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function parseCookies(header = "") {
  return Object.fromEntries(
    header
      .split(";")
      .map((part) => part.trim().split("="))
      .filter(([name]) => name)
      .map(([name, ...rest]) => [name, rest.join("=")]),
  );
}

/**
 * @param {{ host: string, upstreamUrl: string, accessTokenTtlSeconds?: number,
 *   cookieMaxAgeSeconds?: number, tokenEndpointBlockedOffVpn?: boolean,
 *   logPath?: string }} options
 */
async function startFakeDatabricksWorkspace(options) {
  const { host, upstreamUrl } = options;
  const origin = `https://${host}`;
  const upstream = new URL(upstreamUrl);
  const state = {
    vpn: true,
    accessTokenTtlSeconds: options.accessTokenTtlSeconds ?? 3600,
    cookieMaxAgeSeconds: options.cookieMaxAgeSeconds ?? 60,
    tokenEndpointBlockedOffVpn: options.tokenEndpointBlockedOffVpn ?? false,
  };
  const codes = new Set();
  const accessTokens = new Map();
  const refreshTokens = new Set();
  const sessionCookies = new Map();
  const requests = [];
  const liveSockets = new Set();
  const counts = { authorize: 0, tokenCode: 0, tokenRefresh: 0, sessionCreate: 0 };

  const record = (entry) => {
    const line = { at: new Date().toISOString(), vpn: state.vpn, ...entry };
    requests.push(line);
    if (options.logPath) fs.appendFileSync(options.logPath, `${JSON.stringify(line)}\n`);
  };
  const reply = (res, status, headers, body = "") => {
    res.writeHead(status, headers);
    res.end(body);
    return status;
  };
  const ipAclBlocked = (res) =>
    reply(res, 403, { "content-type": "application/json" }, IP_ACL_BODY);
  const random = () => crypto.randomBytes(18).toString("base64url");
  const issueTokens = () => {
    const access = `dapi-standin-${random()}`;
    const refresh = `refresh-standin-${random()}`;
    accessTokens.set(access, Date.now() + state.accessTokenTtlSeconds * 1000);
    refreshTokens.add(refresh);
    return {
      access_token: access,
      refresh_token: refresh,
      token_type: "Bearer",
      expires_in: state.accessTokenTtlSeconds,
      scope: "all-apis offline_access",
    };
  };
  const validCookie = (req) => {
    const value = parseCookies(req.headers.cookie).DBAUTH;
    const expiresAt = value && sessionCookies.get(value);
    return Boolean(expiresAt && expiresAt > Date.now());
  };

  async function handle(req, res) {
    const url = new URL(req.url, origin);
    const route = url.pathname;
    let status;
    let detail;
    if (route === "/oidc/v1/authorize") {
      if (!state.vpn && state.tokenEndpointBlockedOffVpn) {
        status = ipAclBlocked(res);
      } else if (url.searchParams.get("state") === "preflight") {
        status = reply(res, 302, { location: `${origin}/login?next_url=%2Foidc` });
        detail = "preflight";
      } else {
        counts.authorize += 1;
        const code = random();
        codes.add(code);
        const target = new URL(url.searchParams.get("redirect_uri"));
        target.searchParams.set("code", code);
        target.searchParams.set("state", url.searchParams.get("state") ?? "");
        target.searchParams.set("iss", origin);
        status = reply(res, 302, { location: target.toString() });
        detail = "browser sign-in";
      }
    } else if (route === "/oidc/v1/token" && req.method === "POST") {
      const form = new URLSearchParams(await readBody(req));
      const grant = form.get("grant_type");
      detail = grant;
      if (grant === "refresh_token") counts.tokenRefresh += 1;
      else counts.tokenCode += 1;
      if (!state.vpn && state.tokenEndpointBlockedOffVpn) {
        status = ipAclBlocked(res);
      } else if (grant === "authorization_code" && codes.delete(form.get("code"))) {
        status = reply(
          res,
          200,
          { "content-type": "application/json" },
          JSON.stringify(issueTokens()),
        );
      } else if (grant === "refresh_token" && refreshTokens.has(form.get("refresh_token"))) {
        status = reply(
          res,
          200,
          { "content-type": "application/json" },
          JSON.stringify(issueTokens()),
        );
      } else {
        status = reply(
          res,
          400,
          { "content-type": "application/json" },
          '{"error":"invalid_grant"}',
        );
      }
    } else if (route === "/auth/session/create") {
      counts.sessionCreate += 1;
      const bearer = (req.headers.authorization ?? "").replace(/^Bearer /, "");
      const nextUrl = url.searchParams.get("next_url") ?? "/omnigent";
      if (!state.vpn) {
        status = ipAclBlocked(res);
      } else if (!(accessTokens.get(bearer) > Date.now())) {
        status = reply(
          res,
          401,
          { "content-type": "application/json" },
          '{"error_code":"UNAUTHENTICATED"}',
        );
      } else {
        const value = random();
        sessionCookies.set(value, Date.now() + (state.cookieMaxAgeSeconds + 5) * 1000);
        status = reply(res, 302, {
          location: new URL(nextUrl, origin).toString(),
          "set-cookie": `DBAUTH=${value}; Path=/; Max-Age=${state.cookieMaxAgeSeconds}; Secure; HttpOnly; SameSite=Lax`,
        });
      }
    } else if (route === "/login" || route.startsWith("/login/")) {
      status = reply(res, 200, { "content-type": "text/html" }, "<h1>Databricks sign-in</h1>");
    } else if (route === "/" && req.method === "HEAD") {
      status = reply(res, 200, { server: "databricks" });
    } else if (route === "/omnigent" || route.startsWith("/omnigent/")) {
      if (!state.vpn) {
        status = ipAclBlocked(res);
      } else if (!validCookie(req)) {
        status = reply(res, 302, {
          location: `${origin}/login?next_url=${encodeURIComponent(url.pathname)}`,
        });
      } else {
        status = await proxy(req, res);
      }
    } else {
      status = reply(res, 404, { "content-type": "text/plain" }, "not found");
    }
    if (!route.startsWith("/omnigent/assets/")) {
      record({ method: req.method, path: route, status, detail });
    }
  }

  function proxy(req, res) {
    return new Promise((resolve) => {
      const upstreamReq = http.request(
        {
          host: upstream.hostname,
          port: upstream.port,
          method: req.method,
          path: req.url,
          headers: { ...req.headers, host: upstream.host },
        },
        (upstreamRes) => {
          res.writeHead(upstreamRes.statusCode, upstreamRes.headers);
          upstreamRes.pipe(res);
          resolve(upstreamRes.statusCode);
        },
      );
      upstreamReq.on("error", () => {
        if (!res.headersSent) res.writeHead(502);
        res.end();
        resolve(502);
      });
      req.pipe(upstreamReq);
    });
  }

  function proxyUpgrade(req, socket, head) {
    if (!state.vpn || !validCookie(req)) {
      record({ method: "UPGRADE", path: new URL(req.url, origin).pathname, status: 403 });
      socket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
      return;
    }
    const upstreamSocket = net.connect(Number(upstream.port), upstream.hostname, () => {
      const headers = Object.entries({ ...req.headers, host: upstream.host })
        .map(([name, value]) => `${name}: ${value}`)
        .join("\r\n");
      upstreamSocket.write(`${req.method} ${req.url} HTTP/1.1\r\n${headers}\r\n\r\n`);
      upstreamSocket.write(head);
      socket.pipe(upstreamSocket).pipe(socket);
    });
    for (const s of [socket, upstreamSocket]) {
      liveSockets.add(s);
      s.on("close", () => liveSockets.delete(s));
      s.on("error", () => s.destroy());
    }
  }

  const onRequest = (req, res) => {
    handle(req, res).catch(() => {
      if (!res.headersSent) res.writeHead(500);
      res.end();
    });
  };
  const tls = https.createServer(selfSignedCertificate(host), onRequest);
  tls.on("upgrade", proxyUpgrade);
  tls.on("connection", (socket) => {
    liveSockets.add(socket);
    socket.on("close", () => liveSockets.delete(socket));
  });

  // Plain listener: the main process's Node fetch (rewritten by the preload) and
  // Chromium's CONNECT tunnels to the TLS listener both arrive here.
  const plain = http.createServer(onRequest);
  plain.on("connect", (req, clientSocket, head) => {
    const [targetHost] = req.url.split(":");
    if (targetHost !== host || !tls.address()) {
      clientSocket.end("HTTP/1.1 502 Bad Gateway\r\n\r\n");
      return;
    }
    const tunnel = net.connect(tls.address().port, "127.0.0.1", () => {
      clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      tunnel.write(head);
      tunnel.pipe(clientSocket).pipe(tunnel);
    });
    for (const s of [clientSocket, tunnel]) {
      liveSockets.add(s);
      s.on("close", () => liveSockets.delete(s));
      s.on("error", () => s.destroy());
    }
  });

  const listen = (server) =>
    new Promise((resolve) => {
      server.listen(0, "127.0.0.1", () => resolve(server.address().port));
    });
  const tlsPort = await listen(tls);
  const plainPort = await listen(plain);

  return {
    origin,
    host,
    tlsPort,
    proxyUrl: `http://127.0.0.1:${plainPort}`,
    plainUrl: `http://127.0.0.1:${plainPort}`,
    state,
    counts,
    requests,
    /** Join or leave the "VPN"; leaving drops every open connection, as a network change does. */
    setVpn(on) {
      state.vpn = on;
      record({ method: "VPN", path: on ? "joined" : "left", status: 0 });
      if (!on) for (const socket of liveSockets) socket.destroy();
    },
    async close() {
      for (const socket of liveSockets) socket.destroy();
      await Promise.all(
        [tls, plain].map(
          (listener) =>
            new Promise((resolve) => {
              listener.close(resolve);
            }),
        ),
      );
    },
  };
}

module.exports = { IP_ACL_BODY, startFakeDatabricksWorkspace };
