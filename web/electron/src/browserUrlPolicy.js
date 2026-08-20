// Allowlist for AGENT-driven browser navigation (the URL-bar path
// stays permissive — it's a user gesture). An unguarded model-issued loadURL
// could point the view at file:// / cloud-metadata / loopback / private hosts
// and read the bytes back via screenshot (SSRF + local-file read + exfil).
// Runs in the main process (gate holds regardless of caller); pure + dep-free
// so `node --test` can exercise it without Electron.

"use strict";

// Schemes the agent may navigate to; everything else (file:, chrome:, data:,
// javascript:, ...) is a privileged surface or code channel and is rejected.
const ALLOWED_SCHEMES = new Set(["http:", "https:"]);

/**
 * Parse a dotted-quad IPv4 host into four octets, or null if not IPv4. The
 * WHATWG URL parser already canonicalizes obfuscated forms (`0x7f000001`,
 * `2130706433`) to dotted-decimal, so `host` here is dotted-quad or a name.
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
 * True if the octets are loopback / link-local / RFC-1918 private — the ranges
 * an SSRF payload targets (metadata 169.254.169.254, loopback, internal hosts).
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
 * True if the hostname is a loopback/internal name or IPv6 literal we block.
 * `hostname` from the URL parser is lowercased and (for IPv6) bracketed.
 *
 * @param {string} hostname
 */
function isBlockedHostname(hostname) {
  const host = hostname.toLowerCase();
  if (host === "localhost" || host === "") return true;
  if (host.endsWith(".localhost")) return true; // *.localhost → loopback per spec
  // IPv6 loopback / unspecified / link-local (fe80::/10), bracketed by the URL parser.
  if (host === "[::1]" || host === "[::]") return true;
  if (host.startsWith("[fe80:") || host.startsWith("[fe80::")) return true;
  // Belt-and-suspenders: bracketed literal embedding a 127./169.254. quad.
  if (host.startsWith("[") && (host.includes("127.") || host.includes("169.254."))) return true;
  return false;
}

/**
 * Decide whether an AGENT-issued navigation to `url` is allowed: `{ ok: true }`
 * for an http(s) URL to a non-internal host, else `{ ok: false, error }`. Never
 * throws — an unparseable URL is a rejection.
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
