// Loopback stand-in for a host-sharded ingress (deploy/kubernetes/multi_replica).
//
// The real NGINX picks a server replica from `X-Databricks-Omnigent-Slice-Key`
// (or `?omnigent_slice_key=` for browser WebSockets). A request that reaches
// a replica without the host's runner tunnel cannot be served through that
// tunnel. This stand-in has one backend, so it models the wrong-replica outcome
// directly: tunnel-dependent routes that arrive without the key are answered
// the way routes_resources.py answers when no runner client is reachable
// (HTTP 502, "no runner available for resource access"); everything else is
// forwarded unchanged. Every request is logged with its routing/identity
// headers so a journey can compare the app's own requests with a bypass.

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");

const SLICE_KEY_HEADER = "x-databricks-omnigent-slice-key";
const SLICE_KEY_QUERY = "omnigent_slice_key";
const IDENTITY_HEADER = "x-forwarded-email";

/** Routes a replica can only serve through the host's/runner's live tunnel. */
const TUNNEL_ROUTES = [
  /^\/v1\/sessions\/[^/]+\/resources(\/|$)/,
  /^\/v1\/hosts\/[^/]+\/(filesystem|plugins|skills|runners)(\/|$)/,
];

const HOP_BY_HOP = new Set(["connection", "keep-alive", "transfer-encoding", "proxy-connection"]);

function sliceKeyOf(req, url) {
  const header = req.headers[SLICE_KEY_HEADER];
  if (typeof header === "string" && header) return header;
  return url.searchParams.get(SLICE_KEY_QUERY);
}

function forwardHeaders(headers, backend) {
  const out = {};
  for (const [name, value] of Object.entries(headers)) {
    if (HOP_BY_HOP.has(name.toLowerCase())) continue;
    out[name] = value;
  }
  out.host = backend.host;
  return out;
}

/**
 * Start the stand-in in front of `backendUrl`.
 *
 * @param {string} backendUrl e.g. `http://127.0.0.1:43123`
 * @param {{ logPath?: string }} [options] Append one JSON line per request.
 * @returns {Promise<{ url: string, requests: object[], close: () => Promise<void> }>}
 */
async function startRoutingIngress(backendUrl, { logPath } = {}) {
  const backend = new URL(backendUrl);
  const requests = [];
  const record = (entry) => {
    requests.push(entry);
    if (logPath) fs.appendFileSync(logPath, `${JSON.stringify(entry)}\n`);
  };
  const entryFor = (req, url, upgrade) => ({
    ts: new Date().toISOString(),
    method: req.method,
    path: url.pathname + url.search,
    upgrade,
    sliceKey: sliceKeyOf(req, url),
    forwardedEmail: req.headers[IDENTITY_HEADER] ?? null,
    secFetchMode: req.headers["sec-fetch-mode"] ?? null,
    secFetchDest: req.headers["sec-fetch-dest"] ?? null,
    tunnelRoute: TUNNEL_ROUTES.some((re) => re.test(url.pathname)),
    decision: null,
    status: null,
  });

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://ingress.invalid");
    const entry = entryFor(req, url, false);
    if (entry.tunnelRoute && !entry.sliceKey) {
      entry.decision = "wrong-replica";
      entry.status = 502;
      record(entry);
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ detail: "no runner available for resource access" }));
      return;
    }
    entry.decision = "forward";
    const upstream = http.request(
      {
        host: backend.hostname,
        port: backend.port,
        method: req.method,
        path: req.url,
        headers: forwardHeaders(req.headers, backend),
      },
      (upstreamRes) => {
        entry.status = upstreamRes.statusCode;
        record(entry);
        const headers = {};
        for (const [name, value] of Object.entries(upstreamRes.headers)) {
          if (!HOP_BY_HOP.has(name.toLowerCase())) headers[name] = value;
        }
        res.writeHead(upstreamRes.statusCode, headers);
        upstreamRes.pipe(res);
      },
    );
    upstream.on("error", (err) => {
      entry.status = 502;
      entry.decision = `forward-error: ${err.code ?? err.message}`;
      record(entry);
      if (!res.headersSent) res.writeHead(502, { "content-type": "text/plain" });
      res.end("ingress stand-in: backend unreachable");
    });
    req.pipe(upstream);
  });

  server.on("upgrade", (req, socket, head) => {
    const url = new URL(req.url, "http://ingress.invalid");
    const entry = entryFor(req, url, true);
    if (entry.tunnelRoute && !entry.sliceKey) {
      entry.decision = "wrong-replica";
      entry.status = 502;
      record(entry);
      socket.end(
        "HTTP/1.1 502 Bad Gateway\r\ncontent-type: application/json\r\nconnection: close\r\n\r\n" +
          JSON.stringify({ detail: "no runner available for resource access" }),
      );
      return;
    }
    entry.decision = "forward";
    entry.status = 101;
    record(entry);
    const upstream = net.connect(Number(backend.port), backend.hostname, () => {
      const lines = [`${req.method} ${req.url} HTTP/1.1`];
      for (let i = 0; i < req.rawHeaders.length; i += 2) {
        const name = req.rawHeaders[i];
        const value = name.toLowerCase() === "host" ? backend.host : req.rawHeaders[i + 1];
        lines.push(`${name}: ${value}`);
      }
      upstream.write(`${lines.join("\r\n")}\r\n\r\n`);
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    const drop = () => {
      socket.destroy();
      upstream.destroy();
    };
    upstream.on("error", drop);
    socket.on("error", drop);
  });

  await new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    requests,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}

module.exports = { startRoutingIngress, SLICE_KEY_HEADER, IDENTITY_HEADER, TUNNEL_ROUTES };
