// Accept conversation handoffs from trusted browser origins over loopback.

"use strict";

const http = require("node:http");

const { parseOmnigentDeepLink } = require("./deepLink");

/**
 * Ports tried in order; the web page probes the same list. More than one so a
 * taken port, or a development build beside the release build, still gets one.
 */
const DESKTOP_OPEN_PORTS = [28417, 28418, 28419];
const OPEN_PATH = "/open-conversation";
/** Lets a connected server's page learn the app is running before it offers a hand-off. */
const STATUS_PATH = "/status";
const MAX_BODY_BYTES = 1024;
/** Shortest gap between accepted requests, so a page can't keep raising the app. */
const MIN_OPEN_INTERVAL_MS = 1000;
const CONVERSATION_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
/** Marks an answer as this listener's, so the page can tell it from another program on the port. */
const LISTENER_HEADER = "X-Omnigent-Desktop";

/**
 * The deep link for a conversation on the server a page was loaded from.
 *
 * The server comes from the browser-set `Origin`, never from the request body,
 * so a page can only name conversations on its own server.
 *
 * @param {unknown} origin e.g. ``"https://omnigent.example.com"``.
 * @param {unknown} conversationId e.g. ``"e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"``.
 * @returns {string | null} e.g. ``"omnigent://omnigent.example.com/c/e4f5…"``;
 *   null for a malformed id, or an origin a deep link can't express (a remote
 *   server over plain http).
 */
function conversationDeepLink(origin, conversationId) {
  if (typeof origin !== "string" || typeof conversationId !== "string") return null;
  if (!CONVERSATION_ID_RE.test(conversationId)) return null;
  let host;
  try {
    host = new URL(origin).host;
  } catch {
    return null;
  }
  const link = `omnigent://${host}/c/${conversationId}`;
  return parseOmnigentDeepLink(link)?.origin === origin ? link : null;
}

/**
 * Whether a URL addresses the handoff listener on any port it may hold. The
 * shell's embedded browser panes are refused these, whatever page they show.
 *
 * @param {string} url e.g. ``"http://127.0.0.1:28417/open-conversation"``.
 * @returns {boolean}
 */
function isDesktopOpenRequest(url) {
  if (!URL.canParse(url)) return false;
  const { hostname, port } = new URL(url);
  return hostname === "127.0.0.1" && DESKTOP_OPEN_PORTS.includes(Number(port));
}

/**
 * @param {import("node:http").IncomingMessage} request
 * @returns {Promise<string | null>} The body, or null when it is over the limit.
 */
function readBody(request) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size <= MAX_BODY_BYTES) chunks.push(chunk);
    });
    request.on("end", () =>
      resolve(size <= MAX_BODY_BYTES ? Buffer.concat(chunks).toString("utf8") : null),
    );
    request.on("error", () => resolve(null));
  });
}

/**
 * @param {string} body
 * @returns {unknown} The request's `conversationId`, or undefined.
 */
function conversationIdFrom(body) {
  try {
    return JSON.parse(body)?.conversationId;
  } catch {
    return undefined;
  }
}

/**
 * Start the listener on the first free port.
 *
 * @param {{
 *   isKnownOrigin: (origin: string) => boolean,
 *   openLink: (link: string) => boolean | Promise<boolean>,
 *   ports?: number[],
 *   now?: () => number,
 * }} options `isKnownOrigin` says whether the user has connected the app to
 *   that server; every other origin is refused. `openLink` receives the
 *   `omnigent://` link and says whether the app ended up showing it.
 * @returns {Promise<{ port: number, close: () => Promise<void> } | null>} null
 *   when every port is taken.
 */
async function startDesktopOpenServer({
  isKnownOrigin,
  openLink,
  ports = DESKTOP_OPEN_PORTS,
  now = Date.now,
}) {
  let lastOpenAt = Number.NEGATIVE_INFINITY;
  let port = 0;

  const handle = async (request, response) => {
    const reply = (status, headers = {}) => {
      // One request per connection: a refusal is sent before the body is read.
      response.writeHead(status, { "Cache-Control": "no-store", Connection: "close", ...headers });
      response.end();
    };
    // A page on another site can reach this port by a name it controls (DNS rebinding).
    if (request.headers.host !== `127.0.0.1:${port}`) return reply(403);
    const origin = request.headers.origin;
    // Refusals carry no CORS headers, so the asking page learns nothing from them.
    if (typeof origin !== "string" || !isKnownOrigin(origin)) return reply(403);
    // A server no deep link can name (a remote one over plain http) is never offered the app.
    if (conversationDeepLink(origin, "probe") === null) return reply(403);
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Expose-Headers": LISTENER_HEADER,
      [LISTENER_HEADER]: "1",
      Vary: "Origin",
    };
    if (request.url !== OPEN_PATH && request.url !== STATUS_PATH) return reply(404, cors);
    if (request.method === "OPTIONS") {
      return reply(204, {
        ...cors,
        "Access-Control-Allow-Methods": "GET, POST",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Private-Network": "true",
        "Access-Control-Max-Age": "600",
      });
    }
    if (request.url === STATUS_PATH) return reply(request.method === "GET" ? 204 : 405, cors);
    if (request.method !== "POST") return reply(405, cors);
    const body = await readBody(request);
    if (body === null) return reply(413, cors);
    const link = conversationDeepLink(origin, conversationIdFrom(body));
    if (!link) return reply(400, cors);
    if (now() - lastOpenAt < MIN_OPEN_INTERVAL_MS) return reply(429, cors);
    lastOpenAt = now();
    // Accepted only once the app shows the conversation; otherwise the page keeps it.
    return reply((await openLink(link)) ? 204 : 504, cors);
  };

  for (const candidate of ports) {
    const server = http.createServer((request, response) => {
      void handle(request, response).catch(() => response.destroy());
    });
    // oxlint-disable-next-line no-await-in-loop -- each port is tried only after the one before it is refused
    const listening = await new Promise((resolve) => {
      server.once("error", () => resolve(false));
      server.listen({ port: candidate, host: "127.0.0.1", exclusive: true }, () => resolve(true));
    });
    if (!listening) continue;
    port = server.address().port;
    return {
      port,
      close: () =>
        new Promise((resolve) => {
          server.close(() => resolve());
          server.closeAllConnections();
        }),
    };
  }
  return null;
}

module.exports = {
  startDesktopOpenServer,
  conversationDeepLink,
  isDesktopOpenRequest,
  DESKTOP_OPEN_PORTS,
  MIN_OPEN_INTERVAL_MS,
};
