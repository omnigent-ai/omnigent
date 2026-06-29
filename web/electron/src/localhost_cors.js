// Let trusted pages talk to localhost services.
//
// Why: authentication flows (and local-runner integrations) need pages served
// from a remote server origin to fetch() endpoints on the user's own machine
// (http://localhost:<port>). Those localhost services typically don't speak
// CORS — they were written for same-origin or CLI use — so Chromium blocks
// the response and the page sees an opaque "Failed to fetch". Chromium's
// newer Local Network Access permission is NOT the blocker in Electron
// (verified inert in Electron 42, even with LocalNetworkAccessChecks
// force-enabled); ordinary CORS is.
//
// What: webRequest hooks on the shell session that inject CORS response
// headers — and answer CORS preflights — for requests TO localhost FROM
// pages on a trusted origin. The localhost service needs no changes. The
// shell only ever widens responses for (trusted page → localhost) pairs;
// responses that already carry Access-Control-Allow-Origin are left alone
// so services with real CORS policies keep enforcing them.
//
// Trust scoping mirrors the privileged-IPC gate in main.js: the requesting
// page's Origin must be trusted (pinned server origin or explicit
// allowlist), and when frame information is available the TOP-LEVEL page
// must be trusted too, so a trusted-origin iframe embedded in a hostile
// page confers nothing.

"use strict";

/**
 * Hostnames treated as "localhost" targets. Deliberately the loopback
 * literals only — NOT private-range IPs (192.168.*, 10.*) or mDNS names,
 * which reach other machines on the user's network.
 */
const LOCAL_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * webRequest URL filter matching the localhost targets. Chromium match
 * patterns ignore ports, so these cover every port.
 */
const LOCALHOST_URL_FILTER = {
  urls: [
    "http://localhost/*",
    "https://localhost/*",
    "http://127.0.0.1/*",
    "https://127.0.0.1/*",
    "http://[::1]/*",
    "https://[::1]/*",
  ],
};

/**
 * Parse a URL string into its origin, or null when it isn't a valid URL.
 * (Duplicated from main.js rather than imported to keep this module
 * dependency-free and separately testable.)
 *
 * @param {string} url
 * @returns {string | null}
 */
function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * True when a URL points at a loopback host over http(s).
 *
 * @param {string} url
 * @returns {boolean}
 */
function isLocalhostUrl(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return false;
  return LOCAL_HOSTNAMES.has(u.hostname);
}

/**
 * Case-insensitive single-header lookup in a webRequest headers object.
 *
 * @param {Record<string, string | string[]> | undefined} headers
 * @param {string} name Lower-case header name to find.
 * @returns {string | null} The header value (first value when repeated).
 */
function headerValue(headers, name) {
  if (!headers) return null;
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === name) {
      return Array.isArray(value) ? (value[0] ?? null) : value;
    }
  }
  return null;
}

/**
 * Wire the localhost-CORS hooks onto a session.
 *
 * NOTE: Electron allows only ONE listener per webRequest event per session,
 * so this module must stay the sole owner of onBeforeSendHeaders /
 * onHeadersReceived / onCompleted / onErrorOccurred on the shell session.
 *
 * @param {Electron.Session} ses The session to hook (the shell's default
 *   session).
 * @param {(origin: string) => boolean} isTrustedPageOrigin Decides which
 *   page origins may reach localhost. Called per localhost-bound request
 *   with the requesting page's origin (e.g. ``"https://my-server.example"``).
 */
function registerLocalhostCors(ses, isTrustedPageOrigin) {
  /**
   * In-flight localhost requests from trusted pages, keyed by webRequest
   * id: what to echo back in the injected CORS headers. Entries are
   * removed on completion/error (NOT in onHeadersReceived — a redirect
   * re-fires it for the same id).
   *
   * @type {Map<number, {origin: string, acrMethod: string | null,
   *   acrHeaders: string | null}>}
   */
  const inflight = new Map();

  ses.webRequest.onBeforeSendHeaders(LOCALHOST_URL_FILTER, (details, callback) => {
    const pageOrigin = headerValue(details.requestHeaders, "origin");
    // Same-origin localhost→localhost needs no CORS help; requests with no
    // Origin header (top-level navigations, same-origin GETs) don't either.
    if (
      pageOrigin &&
      pageOrigin !== "null" &&
      pageOrigin !== originOf(details.url) &&
      isTrustedPageOrigin(pageOrigin)
    ) {
      // When Chromium attributes the request to a frame (CORS preflights
      // are issued by the network service and may carry none), require the
      // top-level page to be trusted as well — a trusted-origin iframe
      // inside a hostile page must not open localhost to it.
      const topOrigin = details.frame?.top?.url ? originOf(details.frame.top.url) : null;
      if (topOrigin === null || topOrigin === pageOrigin || isTrustedPageOrigin(topOrigin)) {
        inflight.set(details.id, {
          origin: pageOrigin,
          acrMethod: headerValue(details.requestHeaders, "access-control-request-method"),
          acrHeaders: headerValue(details.requestHeaders, "access-control-request-headers"),
        });
      }
    }
    callback({});
  });

  ses.webRequest.onHeadersReceived(LOCALHOST_URL_FILTER, (details, callback) => {
    const info = inflight.get(details.id);
    if (!info) {
      callback({});
      return;
    }
    // The service speaks CORS itself → its policy wins, unmodified.
    if (headerValue(details.responseHeaders, "access-control-allow-origin") !== null) {
      callback({});
      return;
    }
    const responseHeaders = { ...details.responseHeaders };
    // Echo the exact page origin (never "*") so credentialed requests —
    // which auth flows typically are — pass Chromium's CORS checks.
    responseHeaders["Access-Control-Allow-Origin"] = [info.origin];
    responseHeaders["Access-Control-Allow-Credentials"] = ["true"];
    responseHeaders["Vary"] = ["Origin"];
    if (details.method === "OPTIONS" && info.acrMethod) {
      // CORS preflight. The localhost service likely answered 404/405 with
      // no CORS headers; rewrite it into a passing preflight response.
      responseHeaders["Access-Control-Allow-Methods"] = [info.acrMethod];
      if (info.acrHeaders) {
        responseHeaders["Access-Control-Allow-Headers"] = [info.acrHeaders];
      }
      // Pre-approve Chromium's Private Network Access preflight extension,
      // future-proofing against PNA/LNA enforcement arriving in a later
      // Electron upgrade.
      responseHeaders["Access-Control-Allow-Private-Network"] = ["true"];
      callback({ responseHeaders, statusLine: "HTTP/1.1 204 No Content" });
      return;
    }
    callback({ responseHeaders });
  });

  ses.webRequest.onCompleted(LOCALHOST_URL_FILTER, (details) => {
    inflight.delete(details.id);
  });
  ses.webRequest.onErrorOccurred(LOCALHOST_URL_FILTER, (details) => {
    inflight.delete(details.id);
  });
}

module.exports = { registerLocalhostCors, isLocalhostUrl, LOCAL_HOSTNAMES };
