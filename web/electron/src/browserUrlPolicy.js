// Navigation URL policy for agent-driven browser navigation.
//
// The embedded WebContentsView is driven from two paths that share ONE main-
// process choke point (browserViewRegistry.openOrNavigate -> loadURL):
//   1. the URL bar — a user typing an address and pressing Enter. User-
//      initiated, so it keeps its permissive behavior (any scheme the
//      normalizer produced).
//   2. the agent relay — a `browser_navigate` action the model issued. This
//      is NOT a human gesture, so an unguarded loadURL lets the model point
//      the view at `file://`, cloud-metadata / loopback / private-range hosts,
//      then read the rendered bytes back via screenshot/snapshot (SSRF +
//      local-file read + exfil).
//
// `isAgentNavigationAllowed(url)` is the allowlist enforced on the AGENT path
// only. It runs in the main process so the gate holds regardless of caller.
// Kept as a pure, dependency-free function so it can be unit-tested with
// `node --test` without booting Electron.

"use strict";

// Schemes the agent may navigate to. Everything else (file:, chrome:,
// devtools:, data:, blob:, javascript:, about:, ...) is rejected — those are
// either local-resource / privileged surfaces or in-page code channels that
// have no business being reachable from a model-issued navigation.
const ALLOWED_SCHEMES = new Set(["http:", "https:"]);

/**
 * Parse the low byte-count of a dotted-decimal / integer IPv4 host into its
 * four octets, or return null if it isn't an IPv4 literal. The WHATWG URL
 * parser already canonicalizes obfuscated forms (`0x7f000001`, `2130706433`,
 * `0177.0.0.1`) into dotted-decimal in `hostname`, so by the time we see the
 * host here it is either dotted-quad or a non-IPv4 name.
 *
 * @param {string} host
 * @returns {[number, number, number, number] | null}
 */
function parseIpv4(host) {
  const parts = host.split(".");
  if (parts.length !== 4) return null;
  const octets = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const n = Number(part);
    if (n < 0 || n > 255) return null;
    octets.push(n);
  }
  return /** @type {[number,number,number,number]} */ (octets);
}

/**
 * True if the IPv4 octets fall in a link-local, loopback, or RFC-1918 private
 * range — the ranges an SSRF payload would target (cloud metadata at
 * 169.254.169.254, loopback services, internal network hosts).
 *
 * @param {[number, number, number, number]} octets
 */
function isBlockedIpv4(octets) {
  const [a, b] = octets;
  if (a === 127) return true; // 127.0.0.0/8   loopback
  if (a === 10) return true; // 10.0.0.0/8    private
  if (a === 169 && b === 254) return true; // 169.254.0.0/16 link-local (incl. 169.254.169.254 metadata)
  if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12 private
  if (a === 192 && b === 168) return true; // 192.168.0.0/16 private
  if (a === 0) return true; // 0.0.0.0/8     "this host"
  return false;
}

/**
 * True if the hostname is a loopback / internal name or IPv6 literal we block
 * by default. `hostname` from the WHATWG URL parser is already lowercased and,
 * for IPv6, wrapped in brackets.
 *
 * @param {string} hostname
 */
function isBlockedHostname(hostname) {
  const host = hostname.toLowerCase();
  if (host === "localhost" || host === "" ) return true;
  // Any `*.localhost` name resolves to loopback per the spec.
  if (host.endsWith(".localhost")) return true;
  // IPv6 loopback / unspecified / link-local (fe80::/10). Bracketed by URL.
  if (host === "[::1]" || host === "[::]") return true;
  if (host.startsWith("[fe80:") || host.startsWith("[fe80::")) return true;
  // IPv6 mapped/embedded IPv4 loopback etc. — cheap belt-and-suspenders: any
  // bracketed literal containing an embedded 127./169.254. dotted quad.
  if (host.startsWith("[") && (host.includes("127.") || host.includes("169.254."))) return true;
  return false;
}

/**
 * Decide whether an AGENT-issued navigation to `url` is allowed.
 *
 * Returns `{ ok: true }` when the URL is an http(s) URL to a non-internal
 * host, or `{ ok: false, error }` with a clean, structured reason otherwise.
 * Never throws — an unparseable URL is a rejection, not a crash.
 *
 * @param {string} url
 * @returns {{ ok: true } | { ok: false, error: string }}
 */
function isAgentNavigationAllowed(url) {
  if (typeof url !== "string" || url.trim() === "") {
    return { ok: false, error: "navigation blocked: empty url" };
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, error: `navigation blocked: not a valid absolute URL: ${url}` };
  }
  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    return {
      ok: false,
      error: `navigation blocked: scheme "${parsed.protocol}" is not allowed for agent navigation (only http/https)`,
    };
  }
  const hostname = parsed.hostname;
  if (isBlockedHostname(hostname)) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a loopback/internal host`,
    };
  }
  const ipv4 = parseIpv4(hostname);
  if (ipv4 && isBlockedIpv4(ipv4)) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a link-local/loopback/private-range address`,
    };
  }
  return { ok: true };
}

module.exports = {
  isAgentNavigationAllowed,
  // Exported for focused unit tests.
  parseIpv4,
  isBlockedIpv4,
  isBlockedHostname,
  ALLOWED_SCHEMES,
};
