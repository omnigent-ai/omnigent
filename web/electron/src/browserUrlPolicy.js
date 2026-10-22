// Allowlist for AGENT-driven browser navigation (the URL-bar path
// stays permissive — it's a user gesture). An unguarded model-issued loadURL
// could point the view at file:// / cloud-metadata / loopback / private hosts
// and read the bytes back via screenshot (SSRF + local-file read + exfil).
// Runs in the main process (gate holds regardless of caller); Electron-free,
// with DNS injectable, so `node --test` can exercise it without Electron.

"use strict";

const dns = require("node:dns");

// Schemes the agent may navigate to; everything else (file:, chrome:, data:,
// javascript:, ...) is a privileged surface or code channel and is rejected.
const ALLOWED_SCHEMES = new Set(["http:", "https:"]);

// The exact loopback hosts an eligible Arca agent context may open. Other
// loopback spellings and DNS names that resolve to loopback stay blocked.
const ARCA_LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

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
 * True if the octets are outside public unicast space — the ranges an SSRF
 * payload targets (metadata 169.254.169.254, loopback, internal hosts). Mirrors
 * `omnigent/util/ssrf.py`, but deliberately stricter on blocks that are never
 * web destinations: all of 192.0.0.0/24 (Python exempts the .9/.10 anycast
 * services), and on the IPv6 side ::/96, fec0::/10, 2001::/23 (including
 * Python's global exceptions), and 3fff::/20.
 *
 * @param {[number, number, number, number]} octets
 */
function isBlockedIpv4(octets) {
  const [a, b, c] = octets;
  if (a === 0 || a === 10 || a === 127) return true; // "this host", RFC 1918, loopback
  if (a === 100 && b >= 64 && b <= 127) return true; // 100.64.0.0/10 shared (CGNAT)
  if (a === 169 && b === 254) return true; // link-local (incl. 169.254.169.254 metadata)
  if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12 private
  if (a === 192 && b === 168) return true; // 192.168.0.0/16 private
  if (a === 192 && b === 0 && (c === 0 || c === 2)) return true; // protocol assignments, TEST-NET-1
  if (a === 198 && (b === 18 || b === 19)) return true; // 198.18.0.0/15 benchmarking
  if (a === 198 && b === 51 && c === 100) return true; // TEST-NET-2
  if (a === 203 && b === 0 && c === 113) return true; // TEST-NET-3
  return a >= 224; // multicast, reserved, broadcast
}

/**
 * Parse an IPv6 address — bare or bracketed, with an optional dotted-quad tail
 * or zone ID — into eight 16-bit groups, or null if it is not one.
 *
 * @param {string} host
 * @returns {number[] | null}
 */
function parseIpv6(host) {
  const bare = host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
  const text = bare.split("%")[0];
  const halves = text.split("::");
  if (!text.includes(":") || halves.length > 2) return null;
  const parseGroups = (part, allowIpv4Tail) => {
    if (part === "") return [];
    const groups = [];
    const pieces = part.split(":");
    for (const [i, piece] of pieces.entries()) {
      const ipv4 = allowIpv4Tail && i === pieces.length - 1 ? parseIpv4(piece) : null;
      if (ipv4) groups.push((ipv4[0] << 8) | ipv4[1], (ipv4[2] << 8) | ipv4[3]);
      else if (/^[0-9a-f]{1,4}$/i.test(piece)) groups.push(parseInt(piece, 16));
      else return null;
    }
    return groups;
  };
  const head = parseGroups(halves[0], halves.length === 1);
  const tail = halves.length === 2 ? parseGroups(halves[1], true) : [];
  if (!head || !tail) return null;
  if (halves.length === 1) return head.length === 8 ? head : null;
  const zeros = 8 - head.length - tail.length;
  return zeros >= 1 ? [...head, ...new Array(zeros).fill(0), ...tail] : null;
}

/**
 * True if the IPv6 groups are outside public unicast space. Transition forms
 * that carry an IPv4 destination (IPv4-mapped, NAT64) are judged by that IPv4,
 * since the URL parser serializes them in hex (`[::ffff:7f00:1]`).
 *
 * @param {number[]} groups
 */
function isBlockedIpv6(groups) {
  const [g0, g1, g2, g3, g4, g5, g6, g7] = groups;
  const embeddedIpv4 = () => isBlockedIpv4([g6 >> 8, g6 & 0xff, g7 >> 8, g7 & 0xff]);
  const zeroThrough4 = g1 === 0 && g2 === 0 && g3 === 0 && g4 === 0;
  if (g0 === 0 && zeroThrough4 && g5 === 0xffff) return embeddedIpv4(); // ::ffff:0:0/96 mapped
  if (g0 === 0x64 && g1 === 0xff9b && g2 === 0 && g3 === 0 && g4 === 0 && g5 === 0) {
    return embeddedIpv4(); // 64:ff9b::/96 NAT64
  }
  // Only 2000::/3 is global unicast. Everything else is internal or reserved:
  // ::, ::1, IPv4-compatible ::/96, fc00::/7 unique-local, fe80::/10
  // link-local, fec0::/10 site-local, ff00::/8 multicast, ...
  if ((g0 & 0xe000) !== 0x2000) return true;
  if (g0 === 0x2001 && g1 < 0x200) return true; // 2001::/23 protocol assignments, incl. Teredo
  if (g0 === 0x2001 && g1 === 0xdb8) return true; // 2001:db8::/32 documentation
  if (g0 === 0x3fff && g1 < 0x1000) return true; // 3fff::/20 documentation
  return g0 === 0x2002; // 6to4, which can embed any IPv4 (non-global in ssrf.py too)
}

/**
 * Canonical text for an IP address (IPv4-mapped IPv6 collapses to its IPv4),
 * or null if `address` is not one, so differently written forms compare equal.
 *
 * @param {string} address
 */
function canonicalAddress(address) {
  const ipv4 = parseIpv4(address);
  if (ipv4) return ipv4.join(".");
  const groups = parseIpv6(address);
  if (!groups) return null;
  if (groups.slice(0, 5).every((g) => g === 0) && groups[5] === 0xffff) {
    return [groups[6] >> 8, groups[6] & 0xff, groups[7] >> 8, groups[7] & 0xff].join(".");
  }
  return groups.map((g) => g.toString(16)).join(":");
}

/** True if `host` is a dotted-quad IPv4 or an IPv6 literal. */
function isIpLiteral(host) {
  return parseIpv4(host) !== null || parseIpv6(host) !== null;
}

/**
 * True if an IP address is not publicly routable. Anything that does not
 * parse as an IP counts as internal, so a malformed resolver or connection
 * address fails closed.
 *
 * @param {string} address
 */
function isInternalAddress(address) {
  const ipv4 = parseIpv4(address);
  if (ipv4) return isBlockedIpv4(ipv4);
  const ipv6 = parseIpv6(address);
  return ipv6 ? isBlockedIpv6(ipv6) : true;
}

/**
 * True if the hostname is a loopback/internal name or IP literal we block.
 * `hostname` from the URL parser is lowercased, IPv4-canonicalized, and (for
 * IPv6) bracketed.
 *
 * @param {string} hostname
 */
function isBlockedHostname(hostname) {
  const host = hostname.toLowerCase().replace(/\.$/, "");
  if (host === "localhost" || host === "") return true;
  if (host.endsWith(".localhost")) return true; // *.localhost → loopback per spec
  return isIpLiteral(host) && isInternalAddress(host);
}

/**
 * Decide whether an AGENT-issued navigation to `url` is allowed: `{ ok: true }`
 * for an http(s) URL to a non-internal host (or eligible Arca loopback), else
 * `{ ok: false, error }`. Never throws — an unparseable URL is a rejection.
 * Judges the URL alone; `checkAgentNavigation` adds DNS resolution.
 *
 * @param {string} url
 * @returns {{ ok: true } | { ok: false, error: string }}
 */
function isAgentNavigationAllowed(url, { allowLocalhost = false } = {}) {
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
  if (allowLocalhost && ARCA_LOOPBACK_HOSTS.has(hostname)) {
    return { ok: true };
  }
  if (isBlockedHostname(hostname)) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a loopback/internal host`,
    };
  }
  return { ok: true };
}

/**
 * Resolve a hostname with the OS resolver. The desktop shell injects the
 * view's Chromium resolver instead, so the check sees what Chromium will use.
 *
 * @param {string} hostname
 * @returns {Promise<string[]>}
 */
async function lookupHostAddresses(hostname) {
  const records = await dns.promises.lookup(hostname, { all: true });
  return records.map((record) => record.address);
}

/**
 * `isAgentNavigationAllowed` plus DNS: a hostname must resolve, and every
 * address it resolves to must be public. Resolution failure blocks.
 *
 * @param {string} url
 * @param {{ allowLocalhost?: boolean, resolveHost?: (hostname: string) => Promise<string[]> }} [options]
 * @returns {Promise<{ ok: true } | { ok: false, error: string }>}
 */
async function checkAgentNavigation(
  url,
  { allowLocalhost = false, resolveHost = lookupHostAddresses } = {},
) {
  const verdict = isAgentNavigationAllowed(url, { allowLocalhost });
  if (!verdict.ok) return verdict;
  const { hostname } = new URL(url);
  if ((allowLocalhost && ARCA_LOOPBACK_HOSTS.has(hostname)) || isIpLiteral(hostname)) {
    return verdict;
  }
  let addresses = [];
  try {
    addresses = await resolveHost(hostname);
  } catch {
    // Unresolvable here means no public endpoint to allow.
  }
  if (addresses.length === 0 || addresses.some(isInternalAddress)) {
    // One message for both cases so the agent can't probe internal DNS.
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" does not resolve to a public address`,
    };
  }
  return verdict;
}

/**
 * Judge the address Chromium actually connected to for an agent request, which
 * catches a DNS answer that changed after `checkAgentNavigation`. A missing
 * `ip` blocks: agent documents are always fetched from the network, so a
 * response without a connected address is not one we can vouch for.
 *
 * @param {string} url
 * @param {string | undefined} ip
 * @param {{ allowLocalhost?: boolean }} [options]
 * @returns {{ ok: true } | { ok: false, error: string }}
 */
function isAgentConnectionAllowed(url, ip, { allowLocalhost = false } = {}) {
  let hostname = "";
  try {
    hostname = new URL(url).hostname;
  } catch {
    // Judge the address alone.
  }
  if (allowLocalhost && ARCA_LOOPBACK_HOSTS.has(hostname)) return { ok: true };
  if (!ip) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" reported no connected address`,
    };
  }
  if (isInternalAddress(ip)) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" connected to a non-public address`,
    };
  }
  return { ok: true };
}

/**
 * Proxy hosts listed in a PAC-style `resolveProxy` result, e.g.
 * "PROXY a.example:3128;SOCKS5 [::1]:1080;DIRECT" → ["a.example", "::1"].
 *
 * @param {string} pacResult
 * @returns {string[]}
 */
function proxyHosts(pacResult) {
  const hosts = [];
  for (const entry of String(pacResult ?? "").split(";")) {
    const match = /^\s*(?:PROXY|HTTPS|SOCKS|SOCKS4|SOCKS5|QUIC)\s+(\S+)\s*$/i.exec(entry);
    if (!match) continue; // DIRECT or malformed
    const hostPort = match[1];
    const bracketed = /^\[([^\]]+)\](?::\d+)?$/.exec(hostPort);
    hosts.push(bracketed ? bracketed[1] : hostPort.replace(/:\d+$/, ""));
  }
  return hosts;
}

/**
 * True if `ip` is an address of a proxy in `pacResult`. Configuration alone
 * proves nothing about a connection — "PROXY a:3128; DIRECT" may fall back to
 * a direct connection — so the connected address itself must be a proxy's.
 * Known limit: webRequest exposes neither the port nor whether a proxy was
 * used, so if an internal proxy with a DIRECT fallback is down and the
 * destination rebinds to that proxy's IP, a direct connection to another port
 * there is accepted. Failing closed would block agent browsing via such proxies.
 *
 * @param {string | undefined} ip
 * @param {string} pacResult
 * @param {(hostname: string) => Promise<string[]>} resolveHost
 */
async function isProxyAddress(ip, pacResult, resolveHost) {
  const target = ip ? canonicalAddress(ip) : null;
  if (target === null) return false;
  const resolved = await Promise.all(
    proxyHosts(pacResult).map((host) =>
      isIpLiteral(host) ? [host] : resolveHost(host).catch(() => []),
    ),
  );
  return resolved.flat().some((address) => canonicalAddress(address) === target);
}

module.exports = {
  isAgentNavigationAllowed,
  checkAgentNavigation,
  isAgentConnectionAllowed,
  isProxyAddress,
  lookupHostAddresses,
  // Exported for focused unit tests.
  proxyHosts,
  parseIpv4,
  parseIpv6,
  isBlockedIpv4,
  isBlockedIpv6,
  isInternalAddress,
  isBlockedHostname,
  ALLOWED_SCHEMES,
};
